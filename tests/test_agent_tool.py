import json

from harness.loop import run_turn
from harness.permissions import PermissionPolicy
from harness.tools.agent import agent_tool
from harness.tools.base import Tool
from tests.fake_llm import FakeLLM


def noop_tool(read_only: bool = True) -> Tool:
    return Tool(
        name="noop",
        description="A tool that does nothing, for tests.",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "ok",
        read_only=read_only,
    )


def test_subagent_returns_only_the_final_answer():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "the answer"},
        ]
    )
    tool = agent_tool(llm, tools={"noop": noop_tool()}, policy=None, asker=None)
    assert tool.execute(task="do the thing") == "the answer"


def test_subagent_starts_from_an_empty_context():
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tool = agent_tool(llm, tools={}, policy=None, asker=None, system="SUB PROMPT")
    tool.execute(task="explore the repo")
    # the sub's first model call: just the task on a fresh transcript,
    # under the subagent's own system prompt
    assert llm.turns[0]["messages"] == [{"role": "user", "content": "explore the repo"}]
    assert llm.turns[0]["system"] == "SUB PROMPT"


def test_parent_transcript_never_sees_the_subagents_work():
    llm = FakeLLM(
        [
            # parent delegates, sub works (one tool call), sub answers,
            # parent wraps up — one shared FakeLLM serves both loops
            {"type": "tool_calls", "calls": [{"name": "agent", "arguments": {"task": "explore"}}]},
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "sub answer"},
            {"type": "text", "content": "parent answer"},
        ]
    )
    tools = {"noop": noop_tool()}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None, asker=None)
    messages = []
    reply = run_turn(messages, "go", llm, tools=tools)
    assert reply["content"] == "parent answer"
    # exactly: user, the agent call, its one-string result, the final reply
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[2]["content"] == "sub answer"
    assert "noop" not in json.dumps(messages)


def test_subagent_cannot_spawn_subagents():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "agent", "arguments": {"task": "explore"}}]},
            {"type": "text", "content": "sub answer"},
            {"type": "text", "content": "parent answer"},
        ]
    )
    tools = {"noop": noop_tool()}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None, asker=None)
    run_turn([], "go", llm, tools=tools)
    # the sub's model call was offered noop but never the agent tool —
    # recursion is a capability the inner loop simply doesn't have
    names = [d["function"]["name"] for d in llm.turns[1]["tools"]]
    assert "noop" in names
    assert "agent" not in names


def test_subagent_failure_becomes_an_error_result():
    class ExplodesOnSecondCall:
        def __init__(self, script):
            self.inner = FakeLLM(script)
            self.calls = 0

        def complete(self, messages, tools=None, system=None):
            self.calls += 1
            if self.calls == 2:  # the sub's one and only model call
                raise RuntimeError("backend down")
            return self.inner.complete(messages, tools=tools, system=system)

    llm = ExplodesOnSecondCall(
        [
            {"type": "tool_calls", "calls": [{"name": "agent", "arguments": {"task": "x"}}]},
            {"type": "text", "content": "parent answer"},
        ]
    )
    tools = {}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None, asker=None)
    messages = []
    reply = run_turn(messages, "go", llm, tools=tools)
    # the sub blowing up is information for the parent, not a crash
    assert messages[2]["content"] == "Error: RuntimeError: backend down"
    assert reply["content"] == "parent answer"


def test_always_grant_in_the_sub_carries_back_to_the_policy():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    policy = PermissionPolicy("default")
    asks = []

    def asker(name: str, args: dict) -> str:
        asks.append(name)
        return "always"

    tool = agent_tool(
        llm, tools={"noop": noop_tool(read_only=False)}, policy=policy, asker=asker
    )
    tool.execute(task="do it")
    # same policy object: the sub asked once, and the grant persists
    assert asks == ["noop"]
    assert "noop" in policy.session_allowlist


def test_sub_tool_calls_are_observable():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    seen = []
    tool = agent_tool(
        llm,
        tools={"noop": noop_tool()},
        policy=None,
        asker=None,
        on_tool_call=lambda name, args: seen.append(name),
    )
    tool.execute(task="x")
    assert seen == ["noop"]


def test_delegation_itself_is_read_only():
    # the agent tool performs no side effect of its own — every side
    # effect inside the sub passes the same permission gate individually,
    # so the gate belongs on the actions, not on the delegation
    tool = agent_tool(FakeLLM([]), tools={}, policy=None, asker=None)
    assert tool.read_only is True


def test_an_exhausted_subagent_is_an_error_not_an_answer():
    # a sub that hits max_iterations returns the harness abort marker;
    # the wrapper must convert it to an error string, never relay it as
    # the subagent's "final answer"
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
        ]
    )
    tool = agent_tool(
        llm, tools={"noop": noop_tool()}, policy=None, asker=None, max_iterations=2
    )
    result = tool.execute(task="x")
    assert result.startswith("Error:")
    assert "no final answer" in result


def test_recursion_guard_is_by_identity_not_registry_key():
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {}
    tool = agent_tool(llm, tools=tools, policy=None, asker=None)
    tools["delegate"] = tool  # any key: the filter is on the object
    tool.execute(task="x")
    # the sub was offered no tools at all — its own wrapper included
    assert llm.turns[0]["tools"] is None


def test_subagent_system_prompt_callable_is_evaluated_per_delegation():
    llm = FakeLLM(
        [
            {"type": "text", "content": "one"},
            {"type": "text", "content": "two"},
        ]
    )
    prompts = iter(["FIRST", "SECOND"])
    tool = agent_tool(
        llm, tools={}, policy=None, asker=None, system=lambda: next(prompts)
    )
    tool.execute(task="a")
    tool.execute(task="b")
    assert llm.turns[0]["system"] == "FIRST"
    assert llm.turns[1]["system"] == "SECOND"
