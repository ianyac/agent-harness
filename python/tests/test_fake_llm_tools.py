import json

import pytest

from tests.fake_llm import FakeLLM


def test_text_entry_becomes_a_plain_reply():
    llm = FakeLLM([{"type": "text", "content": "hi"}])
    reply = llm.complete([{"role": "user", "content": "x"}])
    assert reply == {"role": "assistant", "content": "hi"}


def test_single_call_entry_becomes_a_tool_call_message():
    llm = FakeLLM(
        [{"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 2, "b": 3}}]}]
    )
    reply = llm.complete([{"role": "user", "content": "sum these"}])
    assert reply["role"] == "assistant"
    assert reply["content"] is None
    (call,) = reply["tool_calls"]
    assert call["id"] == "call_0"
    assert call["type"] == "function"
    assert call["function"]["name"] == "add"
    assert json.loads(call["function"]["arguments"]) == {"a": 2, "b": 3}


def test_call_ids_stay_unique_across_turns():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 1, "b": 1}}]},
            {"type": "tool_calls", "calls": [{"name": "add", "arguments": {"a": 2, "b": 2}}]},
        ]
    )
    first = llm.complete([])["tool_calls"][0]["id"]
    second = llm.complete([])["tool_calls"][0]["id"]
    assert first != second


def test_multiple_calls_in_one_entry_share_the_message():
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {"name": "add", "arguments": {"a": 1, "b": 1}},
                    {"name": "add", "arguments": {"a": 2, "b": 2}},
                ],
            }
        ]
    )
    reply = llm.complete([])
    assert len(reply["tool_calls"]) == 2
    ids = {c["id"] for c in reply["tool_calls"]}
    assert len(ids) == 2


def test_offered_tools_are_recorded():
    llm = FakeLLM([{"type": "text", "content": "ok"}])
    defs = [{"type": "function", "function": {"name": "add"}}]
    llm.complete([{"role": "user", "content": "x"}], tools=defs)
    assert llm.turns[0]["tools"] == defs


def test_unplayed_turns_are_visibly_unplayed():
    llm = FakeLLM(
        [{"type": "text", "content": "a"}, {"type": "text", "content": "b"}]
    )
    llm.complete([{"role": "user", "content": "x"}])
    assert llm.turns[0]["messages"] is not None
    assert llm.turns[1]["messages"] is None


def test_unknown_entry_type_raises():
    llm = FakeLLM([{"type": "poem"}])
    with pytest.raises(ValueError, match="poem"):
        llm.complete([])
