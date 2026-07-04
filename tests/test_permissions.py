from harness.loop import run_turn
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool
from tests.fake_llm import FakeLLM


def reader_tool() -> Tool:
    return Tool(
        name="peek",
        description="Read something without changing anything.",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "peeked",
        read_only=True,
    )


def writer_tool() -> Tool:
    return Tool(
        name="scribble",
        description="Mutate something.",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "scribbled",
    )


def script(*names: str) -> FakeLLM:
    """One tool call per iteration for each name, then a text answer."""
    entries = [
        {"type": "tool_calls", "calls": [{"name": n, "arguments": {}}]} for n in names
    ]
    entries.append({"type": "text", "content": "done"})
    return FakeLLM(entries)


def recording_asker(answers: list[str], log: list):
    def ask(name: str, args: dict) -> str:
        log.append((name, args))
        return answers.pop(0)

    return ask


def test_read_only_tool_runs_without_asking_in_default_mode():
    asked = []
    messages = []
    run_turn(
        messages,
        "peek",
        script("peek"),
        tools={"peek": reader_tool()},
        policy=PermissionPolicy("default"),
        asker=recording_asker([], asked),
    )
    assert asked == []
    assert messages[2]["content"] == "peeked"


def test_ask_yes_runs_the_tool_once():
    asked = []
    messages = []
    run_turn(
        messages,
        "scribble",
        script("scribble"),
        tools={"scribble": writer_tool()},
        policy=PermissionPolicy("default"),
        asker=recording_asker(["yes"], asked),
    )
    assert asked == [("scribble", {})]
    assert messages[2]["content"] == "scribbled"


def test_ask_no_becomes_a_denial_result_the_model_sees():
    llm = script("scribble")
    messages = []
    run_turn(
        messages,
        "scribble",
        llm,
        tools={"scribble": writer_tool()},
        policy=PermissionPolicy("default"),
        asker=recording_asker(["no"], []),
    )
    assert messages[2]["role"] == "tool"
    assert messages[2]["content"].startswith("Permission denied")
    # the model's next call was shown the denial
    assert llm.turns[1]["messages"][-1]["content"].startswith("Permission denied")
    # and the turn still finished with a plain answer
    assert messages[-1] == {"role": "assistant", "content": "done"}


def test_always_skips_the_second_ask():
    asked = []
    policy = PermissionPolicy("default")
    messages = []
    run_turn(
        messages,
        "scribble twice",
        script("scribble", "scribble"),
        tools={"scribble": writer_tool()},
        policy=policy,
        asker=recording_asker(["always"], asked),
    )
    assert len(asked) == 1
    executed = [m for m in messages if m["role"] == "tool"]
    assert [m["content"] for m in executed] == ["scribbled", "scribbled"]
    assert "scribble" in policy.session_allowlist


def test_read_only_mode_denies_writes_without_asking():
    asked = []
    messages = []
    run_turn(
        messages,
        "scribble",
        script("scribble"),
        tools={"scribble": writer_tool()},
        policy=PermissionPolicy("readOnly"),
        asker=recording_asker([], asked),
    )
    assert asked == []
    assert messages[2]["content"].startswith("Permission denied")


def test_accept_all_runs_everything_without_asking():
    asked = []
    messages = []
    run_turn(
        messages,
        "scribble",
        script("scribble"),
        tools={"scribble": writer_tool()},
        policy=PermissionPolicy("acceptAll"),
        asker=recording_asker([], asked),
    )
    assert asked == []
    assert messages[2]["content"] == "scribbled"


def test_no_policy_keeps_stage_two_behavior():
    messages = []
    run_turn(
        messages,
        "scribble",
        script("scribble"),
        tools={"scribble": writer_tool()},
    )
    assert messages[2]["content"] == "scribbled"


def test_ask_with_no_asker_degrades_to_denial():
    messages = []
    run_turn(
        messages,
        "scribble",
        script("scribble"),
        tools={"scribble": writer_tool()},
        policy=PermissionPolicy("default"),
    )
    assert messages[2]["content"].startswith("Permission denied")
