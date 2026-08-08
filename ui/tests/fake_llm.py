import json
from copy import deepcopy
from typing import Callable


class FakeLLM:
    """Small scripted LLM used by the standalone UI service tests."""

    context_window = 128_000

    def __init__(self, script: list[dict]):
        self.script = deepcopy(script)
        self.turns: list[dict] = []
        self._call_counter = 0

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        projection_hash: str | None = None,
        on_stream_reset: Callable[[], None] | None = None,
    ) -> dict:
        while self.script:
            entry = self.script.pop(0)
            self.turns.append(
                {
                    "output": deepcopy(entry),
                    "messages": deepcopy(messages),
                    "tools": deepcopy(tools),
                    "system": system,
                    "projection_hash": projection_hash,
                }
            )
            match entry["type"]:
                case "stream_reset":
                    if on_stream_reset is not None:
                        on_stream_reset()
                case "text":
                    self._stream(entry["content"], on_text_delta)
                    return {"role": "assistant", "content": entry["content"]}
                case "tool_calls":
                    self._stream(entry.get("content"), on_text_delta)
                    return {
                        "role": "assistant",
                        "content": entry.get("content"),
                        "tool_calls": [
                            self._tool_call(
                                call["name"],
                                call["arguments"],
                                raw=call.get("raw_arguments"),
                            )
                            for call in entry["calls"]
                        ],
                    }
                case unknown:
                    raise ValueError(f"unknown FakeLLM script entry type {unknown!r}")
        raise AssertionError("FakeLLM script exhausted")

    @staticmethod
    def _stream(content: str | None, callback: Callable[[str], None] | None) -> None:
        if not content or callback is None:
            return
        for offset in range(0, len(content), 5):
            callback(content[offset : offset + 5])

    def _tool_call(self, name: str, arguments: dict, *, raw: str | None) -> dict:
        call = {
            "id": f"call_{self._call_counter}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments) if raw is None else raw,
            },
        }
        self._call_counter += 1
        return call
