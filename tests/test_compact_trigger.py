from harness.compaction import DEFAULT_SUMMARY_INSTRUCTION
from harness.loop import run_turn
from harness.tools.base import Tool
from tests.fake_llm import FakeLLM


def history(n_exchanges: int) -> list[dict]:
    messages = []
    for i in range(n_exchanges):
        messages.append({"role": "user", "content": f"question {i} " + "detail " * 30})
        messages.append({"role": "assistant", "content": f"answer {i} " + "detail " * 30})
    return messages


def test_turn_compacts_history_over_the_threshold():
    messages = history(6)
    llm = FakeLLM(
        [
            {"type": "text", "content": "SUMMARY"},
            {"type": "text", "content": "final answer"},
        ]
    )
    compactions = []
    reply = run_turn(
        messages,
        "next question",
        llm,
        compact_threshold=50,
        keep_recent=2,
        on_compact=compactions.append,
    )
    # first model call is the summarizer, second is the turn itself
    assert llm.turns[0]["messages"][-1]["content"] == DEFAULT_SUMMARY_INSTRUCTION
    assert llm.turns[1]["messages"][0]["content"] == "SUMMARY"
    # 13 messages, keep_recent=2 snapped to the last plain assistant → 10 summarized
    assert compactions == [10]
    assert messages[0]["content"] == "SUMMARY"
    assert {"role": "user", "content": "next question"} in messages
    assert reply["content"] == "final answer"


def test_turn_skips_compaction_under_the_threshold():
    messages = history(2)
    # a single scripted reply: a summarizer call would crash the script
    llm = FakeLLM([{"type": "text", "content": "reply"}])
    run_turn(messages, "hi", llm, compact_threshold=100_000, keep_recent=2)
    assert messages[0]["content"].startswith("question 0")


def test_compaction_is_off_by_default():
    messages = history(6)
    llm = FakeLLM([{"type": "text", "content": "reply"}])
    run_turn(messages, "hi", llm)
    assert messages[0]["content"].startswith("question 0")


def test_no_compaction_when_history_is_all_tail():
    # over the threshold but nothing old enough to summarize: the turn must
    # proceed, on_compact must not fire, and no summarizer call is made
    # (the single-entry script would crash on a second complete())
    messages = [
        {"role": "user", "content": "q1 " + "detail " * 100},
        {"role": "assistant", "content": "a1 " + "detail " * 100},
    ]
    llm = FakeLLM([{"type": "text", "content": "reply"}])
    compactions = []
    run_turn(
        messages,
        "next",
        llm,
        compact_threshold=50,
        keep_recent=8,
        on_compact=compactions.append,
    )
    assert compactions == []
    assert messages[0]["content"].startswith("q1")


def test_mid_turn_tool_growth_triggers_compaction():
    # the turn starts under the threshold; a huge tool result pushes it
    # over between iterations, and the re-check compacts before the next
    # model call — keeping the in-flight tool exchange intact
    messages = []
    for i in range(3):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    dump = Tool(
        name="dump",
        description="Returns a huge payload, for tests.",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "x " * 3000,
    )
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {"type": "text", "content": "SUMMARY"},
            {"type": "text", "content": "done"},
        ]
    )
    compactions = []
    reply = run_turn(
        messages,
        "go",
        llm,
        tools={"dump": dump},
        compact_threshold=2_000,
        keep_recent=2,
        on_compact=compactions.append,
    )
    assert compactions == [6]
    assert llm.turns[1]["messages"][-1]["content"] == DEFAULT_SUMMARY_INSTRUCTION
    assert reply["content"] == "done"
    # the in-flight exchange survived the mid-turn cut as a pair
    assert messages[2].get("tool_calls") and messages[3]["role"] == "tool"


def test_breadcrumbs_reach_the_summary_message():
    messages = history(6)
    llm = FakeLLM(
        [
            {"type": "text", "content": "SUMMARY"},
            {"type": "text", "content": "ok"},
        ]
    )
    run_turn(
        messages,
        "next question",
        llm,
        compact_threshold=50,
        keep_recent=2,
        breadcrumbs="Action log: .agent/actions.jsonl (7 entries)",
    )
    assert "[Auto-generated — not summarized]" in messages[0]["content"]
    assert "Action log: .agent/actions.jsonl (7 entries)" in messages[0]["content"]
