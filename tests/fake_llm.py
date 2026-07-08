import json
from copy import deepcopy
from typing import Callable


class FakeLLM:
    """Scripted model. Script entries (explicit format, no shorthands):

    {"type": "text", "content": "hi"}
    {"type": "tool_calls", "calls": [{"name": ..., "arguments": {...}}, ...]}

    Each entry is wrapped into a turn record — a full I/O trace of one
    exchange once played:  {"output": <scripted directive>,
    "messages": <what was shown>, "tools": <what was offered>,
    "system": <the system prompt received>}
    """

    def __init__(self, script: list[dict]):
        self.turns = [
            {"output": entry, "messages": None, "tools": None, "system": None}
            for entry in script
        ]
        self.current_line = 0
        self._call_counter = 0

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict:
        turn = self.turns[self.current_line]
        self.current_line += 1
        turn["messages"] = deepcopy(messages)
        turn["tools"] = deepcopy(tools)
        turn["system"] = system
        entry = turn["output"]
        match entry["type"]:
            case "text":
                self._stream(entry["content"], on_text_delta)
                return {"role": "assistant", "content": entry["content"]}
            case "tool_calls":
                # optional "content": a reply may narrate before its calls,
                # and that narration streams like any other text
                self._stream(entry.get("content"), on_text_delta)
                return {
                    "role": "assistant",
                    "content": entry.get("content"),
                    "tool_calls": [
                        # raw_arguments scripts a model emitting broken JSON
                        self._tool_call(
                            c["name"],
                            c["arguments"],
                            raw=c.get("raw_arguments"),
                        )
                        for c in entry["calls"]
                    ],
                }
            case unknown:
                raise ValueError(f"unknown FakeLLM script entry type {unknown!r}")

    def _stream(self, content, on_text_delta) -> None:
        if not content or on_text_delta is None:
            return
        # emit in small slices, like a real stream — chunks must join to
        # exactly the content
        for i in range(0, len(content), 5):
            on_text_delta(content[i : i + 5])

    def _tool_call(self, name: str, arguments: dict, raw: str | None = None) -> dict:
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
