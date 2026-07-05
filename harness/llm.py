import json
import pathlib
import time
import uuid
from typing import Callable, Protocol

import httpx


class LLMClient(Protocol):
    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> dict: ...


CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"

RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


class RetryableHTTPError(Exception):
    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"retryable HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def with_retries(
    attempt: Callable[[], dict],
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
):
    """Run attempt(), retrying transient failures with capped backoff.

    Retryable: network-level errors and 429/5xx statuses. Anything else
    (auth failures, malformed requests) raises immediately — retrying a
    401 four times just delays the real error message.
    """
    for tries_left in range(max_retries, -1, -1):
        try:
            return attempt()
        except (httpx.TransportError, RetryableHTTPError) as error:
            if tries_left == 0:
                raise
            delay = 0.5 * 2 ** (max_retries - tries_left)
            retry_after = getattr(error, "retry_after", None)
            if retry_after is not None:
                delay = max(delay, retry_after)
            sleep(min(delay, 30.0))


def normalize(raw: dict) -> dict:
    """Reduce a raw /responses payload to a plain assistant message dict.

    Output items: reasoning is dropped, message text becomes content,
    function_call items become tool_calls. Nothing else crosses into the
    conversation.
    """
    content = None
    tool_calls = []
    for item in raw["output"]:
        if item.get("type") == "message":
            content = "".join(
                part["text"]
                for part in item["content"]
                if part.get("type") == "output_text"
            )
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"],  # stays a JSON string
                    },
                }
            )
    if content is None and not tool_calls:
        raise ValueError("codex response output has no message or function_call")
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def to_wire_tools(tools: list[dict]) -> list[dict]:
    """Chat-format tool definitions -> Responses API flat format."""
    return [
        {
            "type": "function",
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": t["function"]["parameters"],
        }
        for t in tools
    ]


def to_wire_input(messages: list[dict]) -> list[dict]:
    """Internal message dicts -> Responses API input items."""
    items = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m["tool_call_id"],
                    "output": m["content"],
                }
            )
            continue
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"unsupported role {role!r}")
        if m.get("content") is not None:
            items.append(
                {
                    "type": "message",
                    "role": role,
                    # assistant history replays as the model's own prior
                    # output; everything else (user, system) is input
                    "content": [
                        {
                            "type": "output_text"
                            if role == "assistant"
                            else "input_text",
                            "text": m["content"],
                        }
                    ],
                }
            )
        for call in m.get("tool_calls", []):
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            )
    return items


def build_request_body(
    model: str,
    instructions: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
) -> dict:
    """Pure request assembly, split out so the offline suite can pin it."""
    body = {
        "model": model,
        # per-call system prompt wins outright, even when empty; the
        # constructor default applies only when none was provided
        "instructions": system if system is not None else instructions,
        "input": to_wire_input(messages),
        "store": False,
        "stream": True,  # the codex endpoint rejects non-streaming requests
    }
    if tools:
        body["tools"] = to_wire_tools(tools)
    return body


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
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> dict:
        body = build_request_body(
            self.model, self.instructions, messages, tools, system
        )
        # store=False makes the request idempotent, so a stream that dies
        # mid-way is safely retried from scratch (partials discarded)
        return with_retries(lambda: self._attempt(body))

    def _attempt(self, body: dict) -> dict:
        output_items: list[dict] = []
        final = None
        with httpx.Client(timeout=120) as client:
            with client.stream(
                "POST",
                CODEX_URL,
                headers={**self._headers, "session_id": str(uuid.uuid4())},
                json=body,
            ) as resp:
                if resp.status_code in RETRYABLE_STATUSES:
                    retry_after = resp.headers.get("retry-after")
                    raise RetryableHTTPError(
                        resp.status_code,
                        retry_after=float(retry_after) if retry_after else None,
                    )
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
