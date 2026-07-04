from harness.loop import run_turn
from harness.tools.base import Tool
from tests.fake_llm import FakeLLM


def tool_named(name: str, execute) -> Tool:
    return Tool(
        name=name,
        description="Test tool.",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )


def one_call(name: str, arguments: dict | None = None, raw: str | None = None) -> dict:
    call: dict = {"name": name, "arguments": arguments or {}}
    if raw is not None:
        call["raw_arguments"] = raw
    return {"type": "tool_calls", "calls": [call]}


def boom():
    raise ValueError("boom")


def test_tool_exception_becomes_an_error_result_and_the_turn_completes():
    llm = FakeLLM([one_call("fragile"), {"type": "text", "content": "sorry"}])
    messages = []
    reply = run_turn(messages, "go", llm, tools={"fragile": tool_named("fragile", boom)})
    assert messages[2]["role"] == "tool"
    assert "Error" in messages[2]["content"] and "boom" in messages[2]["content"]
    # the model's second call was shown the error text
    assert "boom" in llm.turns[1]["messages"][-1]["content"]
    assert reply == {"role": "assistant", "content": "sorry"}


def test_error_then_success_within_one_turn():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return "worked"

    llm = FakeLLM(
        [one_call("flaky"), one_call("flaky"), {"type": "text", "content": "done"}]
    )
    messages = []
    run_turn(messages, "try", llm, tools={"flaky": tool_named("flaky", flaky)})
    results = [m["content"] for m in messages if m["role"] == "tool"]
    assert "transient" in results[0]
    assert results[1] == "worked"


def test_unknown_tool_name_becomes_an_error_result_listing_available_tools():
    llm = FakeLLM([one_call("teleport"), {"type": "text", "content": "oops"}])
    messages = []
    run_turn(messages, "go", llm, tools={"add": tool_named("add", lambda: "0")})
    assert messages[2]["role"] == "tool"
    assert "teleport" in messages[2]["content"]
    assert "add" in messages[2]["content"]  # tells the model what exists


def test_malformed_arguments_json_becomes_an_error_result():
    llm = FakeLLM(
        [one_call("add", raw='{"a": 1, '), {"type": "text", "content": "oops"}]
    )
    messages = []
    run_turn(messages, "go", llm, tools={"add": tool_named("add", lambda: "0")})
    assert messages[2]["role"] == "tool"
    assert "Error" in messages[2]["content"]


def test_iteration_cap_returns_a_graceful_message_instead_of_raising():
    endless = one_call("add")
    llm = FakeLLM([endless, endless, endless])
    messages = []
    reply = run_turn(
        messages,
        "loop",
        llm,
        tools={"add": tool_named("add", lambda: "0")},
        max_iterations=3,
    )
    assert reply["role"] == "assistant"
    assert "3 iterations" in reply["content"]
    assert messages[-1] == reply  # the transcript stays complete
