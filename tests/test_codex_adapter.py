import json
from pathlib import Path

from harness.llm import normalize, to_wire_input, to_wire_tools


def test_normalize_extracts_a_plain_assistant_message():
    raw = json.loads(Path("tests/fixtures/codex_response.json").read_text())
    msg = normalize(raw)
    assert isinstance(msg["content"], str) and msg["content"]
    # exactly two keys: provider extras must not leak into the conversation
    assert msg == {"role": "assistant", "content": msg["content"]}


def test_normalize_parses_a_real_captured_tool_call():
    raw = json.loads(Path("tests/fixtures/codex_tool_call.json").read_text())
    msg = normalize(raw)
    assert msg["role"] == "assistant"
    (call,) = msg["tool_calls"]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert call["id"] == raw["output"][0]["call_id"]
    # arguments stays a JSON string end to end — the wart we accepted
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


def test_wire_tools_flatten_the_function_wrapper():
    defs = [
        {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two integers.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert to_wire_tools(defs) == [
        {
            "type": "function",
            "name": "add",
            "description": "Add two integers.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_wire_input_translates_a_full_tool_round_trip():
    messages = [
        {"role": "user", "content": "sum 1 and 2"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "3"},
        {"role": "assistant", "content": "The sum is 3."},
    ]
    assert to_wire_input(messages) == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "sum 1 and 2"}],
        },
        {
            "type": "function_call",
            "call_id": "call_0",
            "name": "add",
            "arguments": '{"a": 1, "b": 2}',
        },
        {"type": "function_call_output", "call_id": "call_0", "output": "3"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "The sum is 3."}],
        },
    ]
