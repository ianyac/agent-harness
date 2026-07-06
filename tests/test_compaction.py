from copy import deepcopy

from harness.compaction import DEFAULT_SUMMARY_INSTRUCTION, compact, estimate_tokens
from tests.fake_llm import FakeLLM


def exchange(question: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


def tool_exchange(question: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": answer},
    ]


def test_compact_replaces_old_turns_with_a_summary():
    messages = exchange("q1", "a1") + exchange("q2", "a2") + exchange("q3", "a3")
    llm = FakeLLM([{"type": "text", "content": "SUMMARY"}])
    result = compact(messages, llm, keep_recent=2)
    assert result == [{"role": "assistant", "content": "SUMMARY"}] + messages[4:]


def test_summarizer_sees_the_old_turns_plus_the_instruction():
    messages = exchange("q1", "a1") + exchange("q2", "a2") + exchange("q3", "a3")
    llm = FakeLLM([{"type": "text", "content": "SUMMARY"}])
    compact(messages, llm, keep_recent=2)
    assert llm.turns[0]["messages"] == messages[:4] + [
        {"role": "user", "content": DEFAULT_SUMMARY_INSTRUCTION}
    ]


def test_compact_does_not_mutate_the_original_list():
    messages = exchange("q1", "a1") + exchange("q2", "a2") + exchange("q3", "a3")
    snapshot = deepcopy(messages)
    compact(messages, FakeLLM([{"type": "text", "content": "SUMMARY"}]), keep_recent=2)
    assert messages == snapshot


def test_cut_never_splits_a_tool_call_from_its_results():
    messages = exchange("q1", "a1") + tool_exchange("q2", "a2")
    llm = FakeLLM([{"type": "text", "content": "SUMMARY"}])
    # naive cut (len - 2 = 4) would orphan the tool result at index 4;
    # the boundary must widen back to just after the plain a1 reply
    result = compact(messages, llm, keep_recent=2)
    assert result == [{"role": "assistant", "content": "SUMMARY"}] + messages[2:]


def test_breadcrumb_note_is_appended_under_the_marker():
    messages = exchange("q1", "a1") + exchange("q2", "a2") + exchange("q3", "a3")
    llm = FakeLLM([{"type": "text", "content": "SUMMARY"}])
    result = compact(
        messages,
        llm,
        keep_recent=2,
        breadcrumbs="Action log: .agent/actions.jsonl (247 entries)",
    )
    # appended by code after the model reply — never part of what the
    # summarizer wrote, never subject to its judgment
    assert result[0]["content"] == (
        "SUMMARY\n\n[Auto-generated — not summarized]\n"
        "Action log: .agent/actions.jsonl (247 entries)"
    )


def test_no_breadcrumb_note_by_default():
    messages = exchange("q1", "a1") + exchange("q2", "a2") + exchange("q3", "a3")
    llm = FakeLLM([{"type": "text", "content": "SUMMARY"}])
    result = compact(messages, llm, keep_recent=2)
    assert result[0] == {"role": "assistant", "content": "SUMMARY"}


def test_short_conversation_is_returned_unchanged():
    messages = exchange("q1", "a1")
    # an empty-scripted FakeLLM would crash if the summarizer ran
    result = compact(messages, FakeLLM([]), keep_recent=4)
    assert result is messages


def test_no_safe_cut_means_no_compaction():
    messages = tool_exchange("q1", "a1")
    result = compact(messages, FakeLLM([]), keep_recent=2)
    assert result is messages


def test_token_estimate_grows_with_content():
    assert estimate_tokens([{"role": "user", "content": "hi " * 200}]) > estimate_tokens(
        [{"role": "user", "content": "hi"}]
    )


def test_token_estimate_counts_every_message():
    messages = exchange("what is 2+2?", "4")
    assert estimate_tokens(messages) > estimate_tokens(messages[:1])


def test_token_estimate_includes_the_system_prompt():
    messages = exchange("q1", "a1")
    prompt = "You are a coding agent with a long list of standing rules."
    assert estimate_tokens(messages, system=prompt) > estimate_tokens(messages)


def test_token_estimate_includes_tool_definitions():
    messages = exchange("q1", "a1")
    defs = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]
    assert estimate_tokens(messages, tools=defs) > estimate_tokens(messages)


def test_token_estimate_includes_tool_call_payloads():
    bare = {"role": "assistant", "content": None}
    with_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "run", "arguments": '{"cmd": "ls -la /tmp"}'},
            }
        ],
    }
    assert estimate_tokens([with_call]) > estimate_tokens([bare])
