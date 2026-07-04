import json
import pathlib
import uuid
from typing import Protocol

import httpx


class LLMClient(Protocol):
    def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict: ...


CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"


def normalize(raw: dict) -> dict[str, str]:
    """Reduce a raw /responses payload to a plain assistant message dict.

    The output list holds reasoning items alongside the message; only the
    message's text crosses into the conversation.
    """
    for item in raw["output"]:
        if item.get("type") == "message":
            text = "".join(
                part["text"]
                for part in item["content"]
                if part.get("type") == "output_text"
            )
            return {"role": "assistant", "content": text}
    raise ValueError("no message item in codex response output")


class CodexAdapter:
    def __init__(
        self,
        model: str = "gpt-5.5",
        instructions: str = "You are a helpful assistant.",
    ):
        self.model = model
        self.instructions = instructions
        auth = pathlib.Path.home() / ".codex" / "auth.json"
        try:
            tokens = json.loads(auth.read_text())["tokens"]
        except FileNotFoundError:
            raise RuntimeError(
                f"no codex credentials at {auth} — run `codex login` first"
            ) from None
        self._headers = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "chatgpt-account-id": tokens["account_id"],
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "Accept": "text/event-stream",
        }

    def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        # tools= is accepted but not yet sent; wire translation lands in
        # task 3.3, and passing tools before then changes nothing
        for m in messages:
            if m["role"] not in ("user", "assistant", "system"):
                raise ValueError(
                    f"unsupported role {m['role']!r} — the adapter learns tool "
                    "messages in lesson 3"
                )
        body = {
            "model": self.model,
            "instructions": self.instructions,
            "input": [
                {
                    "type": "message",
                    "role": m["role"],
                    # assistant history replays as the model's own prior
                    # output; everything else (user, system) is input
                    "content": [
                        {
                            "type": "output_text"
                            if m["role"] == "assistant"
                            else "input_text",
                            "text": m["content"],
                        }
                    ],
                }
                for m in messages
            ],
            "store": False,
            "stream": True,  # the codex endpoint rejects non-streaming requests
        }
        output_items: list[dict] = []
        final = None
        with httpx.Client(timeout=120) as client:
            with client.stream(
                "POST",
                CODEX_URL,
                headers={**self._headers, "session_id": str(uuid.uuid4())},
                json=body,
            ) as resp:
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", "replace")
                    raise RuntimeError(f"codex HTTP {resp.status_code}: {detail[:500]}")
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if event.get("type") == "response.output_item.done":
                        output_items.append(event["item"])
                    elif event.get("type") == "response.completed":
                        final = event["response"]
        if final is None:
            raise RuntimeError("codex stream ended without response.completed")
        if not final.get("output"):
            final["output"] = output_items  # terminal event arrives with empty output
        return normalize(final)
