import json

import pytest

from harness.loop import run_turn
from harness.permissions import PermissionPolicy
from harness.tools.agent import agent_tool, run_subagent
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool, simple_tool


def test_subagent_returns_only_the_final_answer():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "the answer"},
        ]
    )
    tool = agent_tool(llm, tools={"noop": noop_tool()}, policy=None)
    assert tool.execute(task="do the thing") == "the answer"


def test_subagent_starts_from_an_empty_context():
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tool = agent_tool(llm, tools={}, policy=None, system="SUB PROMPT")
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
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
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
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
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

        def complete(self, messages, tools=None, system=None, on_text_delta=None):
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
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
    messages = []
    reply = run_turn(messages, "go", llm, tools=tools)
    # the sub blowing up is information for the parent, not a crash
    assert messages[2]["content"] == "Error: RuntimeError: backend down"
    assert reply["content"] == "parent answer"


def test_parent_grants_flow_down_into_the_sub():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    policy = PermissionPolicy("default")
    policy.session_allowlist.add("noop")  # granted "always" at a parent prompt
    tool = agent_tool(llm, tools={"noop": noop_tool(read_only=False)}, policy=policy)
    assert tool.execute(task="do it") == "done"
    # the sub's tool result was a real execution, not a denial
    assert llm.turns[1]["messages"][2]["content"] == "ok"


def test_subagents_never_ask_ungranted_actions_are_denied():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    policy = PermissionPolicy("default")
    tool = agent_tool(llm, tools={"noop": noop_tool(read_only=False)}, policy=policy)
    tool.execute(task="do it")
    # background subs have no asker: an "ask" decision resolves to denial,
    # delivered to the sub as an ordinary tool result...
    assert llm.turns[1]["messages"][2]["content"].startswith("Permission denied")
    # ...and no grant can ever flow up to the parent's session
    assert "noop" not in policy.session_allowlist


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
        on_tool_call=lambda name, args: seen.append(name),
    )
    tool.execute(task="x")
    assert seen == ["noop"]


def test_delegation_itself_is_read_only():
    # the agent tool performs no side effect of its own — every side
    # effect inside the sub passes the same permission gate individually,
    # so the gate belongs on the actions, not on the delegation
    tool = agent_tool(FakeLLM([]), tools={}, policy=None)
    assert tool.read_only is True


def test_delegation_declares_its_consumed_task_brief_as_foldable():
    tool = agent_tool(FakeLLM([]), tools={}, policy=None)
    assert tool.foldable_inputs == ("task",)


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
        llm, tools={"noop": noop_tool()}, policy=None, max_iterations=2
    )
    result = tool.execute(task="x")
    assert result.startswith("Error:")
    assert "no final answer" in result


def test_recursion_guard_survives_registry_renaming():
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {}
    tool = agent_tool(llm, tools=tools, policy=None)
    tools["delegate"] = tool  # any key: the filter is on the tool's field
    tool.execute(task="x")
    # the sub was offered no tools at all — its own wrapper included
    assert llm.turns[0]["tools"] is None


def test_recursion_guard_survives_hook_wrapping():
    from harness.hooks import Hook, HookSet, with_hooks

    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
    # wrapping replaces the Tool object; the spawns_subagents field rides
    # along, so the guard holds where an identity check would break
    with_hooks(tools, HookSet(pre_tool_use=[Hook(command="exit 0")]))
    tools["agent"].execute(task="x")
    assert llm.turns[0]["tools"] is None


def test_hooks_fire_on_the_delegation_itself():
    from harness.hooks import Hook, HookSet, with_hooks

    llm = FakeLLM([])  # any model call would crash: the block precedes it
    tools = {}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
    with_hooks(tools, HookSet(pre_tool_use=[Hook(command="exit 2", matcher="agent")]))
    assert tools["agent"].execute(task="x").startswith("Blocked by hook")


def test_subagent_system_prompt_callable_is_evaluated_per_delegation():
    llm = FakeLLM(
        [
            {"type": "text", "content": "one"},
            {"type": "text", "content": "two"},
        ]
    )
    prompts = iter(["FIRST", "SECOND"])
    tool = agent_tool(
        llm, tools={}, policy=None, system=lambda: next(prompts)
    )
    tool.execute(task="a")
    tool.execute(task="b")
    assert llm.turns[0]["system"] == "FIRST"
    assert llm.turns[1]["system"] == "SECOND"


# run_subagent is the extracted function agent_tool.execute delegates to;
# the tests above already exercise it indirectly through agent_tool, so
# these call it directly to pin its own contract.


def test_run_subagent_returns_the_final_answer():
    llm = FakeLLM([{"type": "text", "content": "the answer"}])
    assert run_subagent("do it", llm, tools={}, policy=None) == "the answer"


def test_run_subagent_excludes_spawns_subagents_tools():
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"noop": noop_tool()}
    # agent_tool's own tool is the canonical spawns_subagents=True case
    tools["agent"] = agent_tool(FakeLLM([]), tools={}, policy=None)
    run_subagent("do it", llm, tools, policy=None)
    names = [d["function"]["name"] for d in llm.turns[0]["tools"]]
    assert "noop" in names
    assert "agent" not in names


def test_run_subagent_excludes_session_local_non_inheritable_tools():
    # Regression caught: a child using the parent's fold tool would mutate IDs
    # belonging to a transcript it cannot see.
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {
        "local": simple_tool(
            "local",
            lambda: "ok",
            description="parent-local control",
            read_only=True,
            inheritable=False,
        ),
        "noop": noop_tool(),
    }
    run_subagent("do it", llm, tools, policy=None)
    names = [definition["function"]["name"] for definition in llm.turns[0]["tools"]]
    assert names == ["noop"]


def test_substitution_restores_a_filtered_tool_in_a_safe_build():
    # the parent's skill tool is fork-capable (filtered out); the sub gets the
    # non-forking build in its place — filtering alone could only remove it
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"skill": noop_tool(name="skill", spawns_subagents=True)}
    variant = noop_tool(name="skill")
    run_subagent("do it", llm, tools, policy=None, substitutions={"skill": variant})
    names = [d["function"]["name"] for d in llm.turns[0]["tools"]]
    assert names == ["skill"]


def test_substitution_is_ignored_for_a_name_the_caller_did_not_offer():
    # a fork skill's allowed-tools passes an already-restricted registry;
    # substitution must not smuggle a tool back past that restriction
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"read_file": noop_tool(name="read_file")}   # "skill" deliberately absent
    run_subagent(
        "do it", llm, tools, policy=None,
        substitutions={"skill": noop_tool(name="skill")},
    )
    names = [d["function"]["name"] for d in llm.turns[0]["tools"]]
    assert names == ["read_file"]


def test_agent_tool_forwards_substitutions_to_the_subagent():
    # the model-driven delegation path: deleting the forward in agent_tool used
    # to leave the whole suite green while stripping skills from every subagent
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"skill": noop_tool(name="skill", spawns_subagents=True)}
    tool = agent_tool(
        llm, tools, policy=None,
        substitutions={"skill": noop_tool(name="skill")},
    )
    tool.execute(task="go")
    names = [d["function"]["name"] for d in llm.turns[0]["tools"]]
    assert names == ["skill"]


def test_substitutions_may_be_a_callable_given_the_filtered_registry():
    # the callable form lets a caller pick a build from what the sub will
    # actually hold — main.py only hands over an EXECUTING skill tool to a sub
    # that already has bash, so allowed-tools stays a real bound
    seen = []

    def choose(offered):
        seen.append(sorted(offered))
        return {"skill": noop_tool(name="skill")} if "bash" in offered else {}

    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"skill": noop_tool(name="skill", spawns_subagents=True),
             "read_file": noop_tool(name="read_file")}
    run_subagent("go", llm, tools, policy=None, substitutions=choose)
    assert seen == [["read_file"]]              # filtered registry, pre-substitution
    names = [d["function"]["name"] for d in llm.turns[0]["tools"]]
    assert names == ["read_file"]               # no bash → no skill tool


def test_a_delegating_substitution_is_refused():
    # the filter exists to keep delegating tools out of subagents; a
    # substitution must not be a way around it (unbounded recursion)
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"skill": noop_tool(name="skill", spawns_subagents=True)}
    with pytest.raises(ValueError, match="delegating"):
        run_subagent(
            "go", llm, tools, policy=None,
            substitutions={"skill": noop_tool(name="skill", spawns_subagents=True)},
        )


def test_a_substitution_whose_name_disagrees_with_its_key_is_refused():
    # definitions() advertises tool.name but the loop dispatches on the dict
    # key, so a divergence offers the sub a tool it can never call
    llm = FakeLLM([{"type": "text", "content": "done"}])
    tools = {"skill": noop_tool(name="skill", spawns_subagents=True)}
    with pytest.raises(ValueError, match="does not match tool name"):
        run_subagent(
            "go", llm, tools, policy=None,
            substitutions={"skill": noop_tool(name="view_skill")},
        )


def test_substitution_does_not_mutate_the_parent_registry():
    llm = FakeLLM([{"type": "text", "content": "done"}])
    parent = {"skill": noop_tool(name="skill", spawns_subagents=True)}
    run_subagent(
        "do it", llm, parent, policy=None,
        substitutions={"skill": noop_tool(name="skill")},
    )
    assert parent["skill"].spawns_subagents is True   # parent build untouched


def test_run_subagent_exhaustion_becomes_an_error_string():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
        ]
    )
    result = run_subagent(
        "do it", llm, tools={"noop": noop_tool()}, policy=None, max_iterations=2
    )
    assert result.startswith("Error:")
    assert "no final answer" in result
