import json

import pytest

from ui.server.tests.fake_llm import FakeLLM, text_reply, tool_reply


def test_replies_in_order_and_requests_recorded():
    llm = FakeLLM([text_reply("one"), text_reply("two")])
    first = llm.complete([{"role": "user", "content": "q"}], system="sys")
    assert first == {"role": "assistant", "content": "one"}
    assert llm.complete([]) == {"role": "assistant", "content": "two"}
    assert llm.requests[0]["system"] == "sys"
    assert llm.requests[0]["messages"] == [{"role": "user", "content": "q"}]


def test_tool_reply_shape_matches_harness_expectations():
    reply = tool_reply(("echo", {"x": 1}), ("bash", {"command": "ls"}))
    assert reply["role"] == "assistant" and reply["content"] is None
    calls = reply["tool_calls"]
    assert [c["id"] for c in calls] == ["call-1", "call-2"]
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "echo"
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}


def test_exhausted_replies_raise_a_clear_error():
    llm = FakeLLM([text_reply("only one")])
    llm.complete([])
    with pytest.raises(AssertionError, match="ran out of scripted replies"):
        llm.complete([])
