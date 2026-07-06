"""Scripted LLMClient double — the ui suite's model, like the harness
suite's fake but owned here (tests/ is the harness lane)."""

import json


class FakeLLM:
    def __init__(self, replies: list[dict]):
        self._replies = list(replies)
        self.requests: list[dict] = []

    def complete(self, messages, tools=None, system=None):
        self.requests.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "system": system}
        )
        if not self._replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        return self._replies.pop(0)


def text_reply(text: str) -> dict:
    return {"role": "assistant", "content": text}


def tool_reply(*calls: tuple[str, dict]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls, start=1)
        ],
    }
