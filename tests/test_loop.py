import json

from harness.loop import _run_one_call, run_tool, run_turn
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool
from tests.fake_llm import FakeLLM


def add_tool() -> Tool:
    return Tool(
        name="add",
        description="Add two integers and return the sum.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        execute=lambda a, b: str(a + b),
    )


def test_tool_call_then_answer_builds_the_full_sandwich():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 2, "b": 3}}]},
            {"type": "text", "content": "The sum is 5."},
        ]
    )
    messages = []
    reply = run_turn(messages, "add 2 and 3", llm, tools={"add": add_tool()})
    assert reply == {"role": "assistant", "content": "The sum is 5."}
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[2] == {"role": "tool", "tool_call_id": "call_0", "content": "5"}


def test_model_sees_the_tool_result_and_is_offered_tools_every_iteration():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 2, "b": 3}}]},
            {"type": "text", "content": "done"},
        ]
    )
    registry = {"add": add_tool()}
    run_turn([], "add", llm, tools=registry)
    second_call_saw = llm.turns[1]["messages"]
    assert second_call_saw[-1] == {
        "role": "tool",
        "tool_call_id": "call_0",
        "content": "5",
    }
    assert llm.turns[0]["tools"] == llm.turns[1]["tools"]
    assert llm.turns[0]["tools"] is not None


def test_two_sequential_tool_calls_before_answering():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 1, "b": 1}}]},
            {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 2, "b": 2}}]},
            {"type": "text", "content": "2 and 4"},
        ]
    )
    messages = []
    run_turn(messages, "two sums", llm, tools={"add": add_tool()})
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


def test_parallel_calls_in_one_message_each_get_a_result():
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {"name": "add", "arguments": {"a": 1, "b": 1}},
                    {"name": "add", "arguments": {"a": 2, "b": 2}},
                ],
            },
            {"type": "text", "content": "2 and 4"},
        ]
    )
    messages = []
    run_turn(messages, "two sums at once", llm, tools={"add": add_tool()})
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "assistant"]
    assert messages[2]["tool_call_id"] == "call_0"
    assert messages[3]["tool_call_id"] == "call_1"
    assert [m["content"] for m in messages[2:4]] == ["2", "4"]


def test_without_tools_behaves_exactly_like_stage_one():
    llm = FakeLLM([{"type": "text", "content": "hi"}])
    messages = []
    reply = run_turn(messages, "hello", llm)
    assert reply == {"role": "assistant", "content": "hi"}
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert llm.turns[0]["tools"] is None


def test_observer_sees_each_execution_in_order():
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {"name": "add", "arguments": {"a": 1, "b": 1}},
                    {"name": "add", "arguments": {"a": 2, "b": 2}},
                ],
            },
            {"type": "text", "content": "done"},
        ]
    )
    seen = []
    run_turn(
        [],
        "two sums",
        llm,
        tools={"add": add_tool()},
        on_tool_call=lambda name, args: seen.append((name, args)),
    )
    assert seen == [("add", {"a": 1, "b": 1}), ("add", {"a": 2, "b": 2})]


def test_iteration_cap_ends_the_turn_gracefully():
    # rewritten in lesson 8: the cap is a failure, and failure is information
    endless = {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 1, "b": 1}}]}
    llm = FakeLLM([endless, endless, endless])
    messages = []
    reply = run_turn(messages, "loop forever", llm, tools={"add": add_tool()}, max_iterations=3)
    assert reply["role"] == "assistant"
    assert "3 iterations" in reply["content"]
    assert messages[-1] == reply


def test_run_tool_denies_a_non_read_only_tool_under_read_only_mode():
    # run_tool is the governed dispatch a tool can delegate to (the skill
    # tool's embedded !`cmd`), so it must enforce the permission gate itself.
    policy = PermissionPolicy("readOnly")
    result = run_tool(
        "add", {"a": 1, "b": 1}, {"add": add_tool()}, policy, asker=None, on_tool_call=None
    )
    assert result.startswith("Permission denied")


def test_run_tool_invokes_on_tool_call_once_when_permitted():
    seen = []
    result = run_tool(
        "add",
        {"a": 2, "b": 3},
        {"add": add_tool()},
        policy=None,
        asker=None,
        on_tool_call=lambda name, args: seen.append((name, args)),
    )
    assert result == "5"
    assert seen == [("add", {"a": 2, "b": 3})]


def test_run_tool_unknown_tool_name_returns_error():
    result = run_tool("nope", {}, {}, policy=None, asker=None, on_tool_call=None)
    assert result.startswith("Error: unknown tool")


def test_run_one_call_still_parses_json_args_and_delegates_to_run_tool():
    call = {"function": {"name": "add", "arguments": json.dumps({"a": 4, "b": 5})}}
    result = _run_one_call(call, {"add": add_tool()}, policy=None, asker=None, on_tool_call=None)
    assert result == "9"
