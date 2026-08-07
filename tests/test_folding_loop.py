import json

import pytest

from harness.folding import FoldConfig, FoldingContext, ProjectionError
from harness.loop import run_turn
from harness.tools.base import Tool
from harness.tools.folding import fold_tool
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool
from tests.test_folding import rich_note, tool_exchange


def completed_history(result: str = "full evidence") -> list[dict]:
    return tool_exchange("read_file", {"path": "a.py"}, result) + [
        {"role": "assistant", "content": "read complete"}
    ]


def context_for(tmp_path, **config) -> FoldingContext:
    return FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0, **config),
    )


def test_loop_sends_projection_but_retains_full_shadow_history(tmp_path):
    # Regression caught: projection markers must never overwrite the caller's
    # durable transcript, or unfold/replay loses the original evidence.
    messages = completed_history()
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": noop_tool(name="read_file")})
    context.fold("m2.r0", "finished", rich_note())
    context.checkpoint()
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, context=context)

    assert llm.turns[0]["messages"][2]["content"].startswith("[folded m2.r0")
    assert messages[2]["content"] == "full evidence"


def test_loop_applies_pending_folds_at_the_next_turn_boundary(tmp_path):
    # Regression caught: a low-volume fold can wait within its current phase,
    # but it must not remain unapplied forever once a new user turn begins.
    messages = completed_history()
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": noop_tool(name="read_file")})
    context.fold("m2.r0", "finished", rich_note())
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, context=context)

    assert llm.turns[0]["messages"][2]["content"].startswith("[folded m2.r0")


def test_loop_refuses_folding_and_compaction_before_mutating_history(tmp_path):
    # Regression caught: two independent context managers produce a projection
    # neither ledger can reconstruct.
    messages = []
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_turn(
            messages,
            "hello",
            FakeLLM([]),
            context=context_for(tmp_path),
            compact_threshold=10,
        )
    assert messages == []


def test_loop_labels_new_tool_results_before_the_next_model_call(tmp_path):
    # Regression caught: IDs are copy handles for the agent; assigning them only
    # at session end makes fold calls impossible during the active turn.
    dump = Tool(
        name="dump",
        description="return evidence",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "fresh evidence",
    )
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    messages = []

    run_turn(messages, "inspect", llm, tools={"dump": dump}, context=context_for(tmp_path))

    assert llm.turns[1]["messages"][2]["content"].startswith("[m2.r0 · ~")
    assert messages[2]["content"] == "fresh evidence"


def test_marked_share_crossing_threshold_rebuilds_mid_turn(tmp_path):
    # Regression caught: waiting until the next user turn after a very large fold
    # wastes every model call in the remainder of the current phase.
    dump = Tool(
        name="dump",
        description="return evidence",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "evidence " * 2_000,
    )
    context = context_for(tmp_path, checkpoint_ratio=0.01)
    tools = {"dump": dump, "fold": fold_tool(context)}
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "fold",
                        "arguments": {
                            "span_id": "m2.r0",
                            "reason": "finished",
                            "note": rich_note(),
                        },
                    }
                ],
            },
            {"type": "text", "content": "done"},
        ]
    )

    run_turn([], "inspect", llm, tools=tools, context=context)

    assert llm.turns[2]["messages"][2]["content"].startswith("[folded m2.r0")


def test_small_mark_stays_visible_mid_turn_then_folds_on_next_turn(tmp_path):
    context = context_for(tmp_path, checkpoint_ratio=1.0)
    dump = Tool(
        name="dump",
        description="return evidence",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "small evidence",
    )
    tools = {"dump": dump, "fold": fold_tool(context)}
    first_llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "fold",
                        "arguments": {
                            "span_id": "m2.r0",
                            "reason": "finished",
                            "note": rich_note(),
                        },
                    }
                ],
            },
            {"type": "text", "content": "phase done"},
        ]
    )
    messages: list[dict] = []

    run_turn(messages, "inspect", first_llm, tools=tools, context=context)
    assert "small evidence" in first_llm.turns[2]["messages"][2]["content"]

    second_llm = FakeLLM([{"type": "text", "content": "done"}])
    run_turn(messages, "next phase", second_llm, tools=tools, context=context)
    assert second_llm.turns[0]["messages"][2]["content"].startswith("[folded m2.r0")


def test_following_turn_receives_auto_fold_notice_without_polluting_shadow(tmp_path):
    context = context_for(tmp_path)
    messages = completed_history("same")
    messages.extend(
        [
            {"role": "user", "content": "read again"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "same"},
            {"role": "assistant", "content": "read complete"},
        ]
    )
    tools = {"read_file": noop_tool(name="read_file")}
    context.sync(messages, tools)
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, tools=tools, context=context)

    projected_user = next(
        message for message in reversed(llm.turns[0]["messages"])
        if message["role"] == "user" and "next" in message["content"]
    )
    assert projected_user["content"].startswith("[auto-folded m6.r0")
    assert messages[-2] == {"role": "user", "content": "next"}


def test_scanner_redaction_reaches_the_model_and_shadow_immediately(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    leak = Tool(
        name="leak",
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: secret,
    )
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "leak", "arguments": {}}]},
            {"type": "text", "content": "handled"},
        ]
    )
    messages: list[dict] = []

    run_turn(messages, "inspect", llm, tools={"leak": leak}, context=context_for(tmp_path))

    assert secret not in json.dumps(llm.turns[1]["messages"])
    assert secret not in json.dumps(messages)
    assert messages[2]["content"] == "[redacted — credential detected in tool output]"
