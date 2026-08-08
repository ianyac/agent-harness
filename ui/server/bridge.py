"""Thread-safe primitives between synchronous harness turns and asyncio."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from harness.llm import LLMClient
from pydantic import TypeAdapter
from server.protocol import PermissionDecision, ServerEvent


class TurnCancelled(Exception):
    """Raised at a harness callback boundary after cancellation is requested."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise TurnCancelled()


class EventSink:
    """Build validated server events in a worker and enqueue them on its loop."""

    def __init__(
        self,
        session_id: str,
        generation: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.session_id = session_id
        self.generation = generation
        self.loop = loop
        self._queue: asyncio.Queue[ServerEvent] = asyncio.Queue()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._adapter = TypeAdapter(ServerEvent)

    def emit(self, event_type: str, **payload: object) -> None:
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        event = self._adapter.validate_python(
            {
                "type": event_type,
                "session_id": self.session_id,
                "generation": self.generation,
                "sequence": sequence,
                **payload,
            }
        )
        self.loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def next(self) -> ServerEvent:
        return await self._queue.get()


DecisionKind = Literal["permission", "plan"]
PermissionResult = PermissionDecision
PlanResult = tuple[bool, str]


@dataclass
class _PendingDecision:
    kind: DecisionKind
    request_id: str
    answers: queue.Queue[PermissionResult | PlanResult]
    answered: bool = False


class DecisionBroker:
    """Own the sole blocking permission or plan decision for one runtime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: _PendingDecision | None = None

    @property
    def pending_request_id(self) -> str | None:
        with self._lock:
            return self._pending.request_id if self._pending is not None else None

    def request_permission(
        self,
        request_id: str,
        on_pending: Callable[[], None] | None = None,
    ) -> PermissionResult:
        result = self._request("permission", request_id, on_pending)
        if not isinstance(result, str):
            raise RuntimeError("permission request received a plan answer")
        return result

    def request_plan(
        self,
        request_id: str,
        on_pending: Callable[[], None] | None = None,
    ) -> PlanResult:
        result = self._request("plan", request_id, on_pending)
        if not isinstance(result, tuple):
            raise RuntimeError("plan request received a permission answer")
        return result

    def _request(
        self,
        kind: DecisionKind,
        request_id: str,
        on_pending: Callable[[], None] | None,
    ) -> PermissionResult | PlanResult:
        pending = _PendingDecision(kind, request_id, queue.Queue(maxsize=1))
        with self._lock:
            if self._pending is not None:
                raise RuntimeError("a decision request is already pending")
            self._pending = pending
        try:
            if on_pending is not None:
                on_pending()
            return pending.answers.get()
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def answer_permission(
        self,
        request_id: str,
        answer: PermissionDecision,
    ) -> bool:
        if answer not in ("yes", "no", "always"):
            return False
        return self._answer("permission", request_id, answer)

    def answer_plan(
        self,
        request_id: str,
        approved: bool,
        feedback: str = "",
    ) -> bool:
        return self._answer("plan", request_id, (approved, feedback))

    def _answer(
        self,
        kind: DecisionKind,
        request_id: str,
        answer: PermissionResult | PlanResult,
    ) -> bool:
        with self._lock:
            pending = self._pending
            if (
                pending is None
                or pending.kind != kind
                or pending.request_id != request_id
                or pending.answered
            ):
                return False
            pending.answered = True
            pending.answers.put_nowait(answer)
            return True

    def disconnect(self) -> None:
        with self._lock:
            pending = self._pending
            if pending is None or pending.answered:
                return
            pending.answered = True
            fallback: PermissionResult | PlanResult = (
                "no" if pending.kind == "permission" else (False, "")
            )
            pending.answers.put_nowait(fallback)


class CancellableLLM:
    """Public LLM wrapper that checks cancellation at streaming boundaries."""

    def __init__(self, wrapped: LLMClient, token: CancellationToken) -> None:
        self._wrapped = wrapped
        self._token = token
        context_window = getattr(wrapped, "context_window", None)
        if context_window is not None:
            self.context_window = context_window

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        projection_hash: str | None = None,
        on_stream_reset: Callable[[], None] | None = None,
    ) -> dict:
        self._token.check()

        def text_delta(text: str) -> None:
            self._token.check()
            if on_text_delta is not None:
                on_text_delta(text)
            self._token.check()

        def stream_reset() -> None:
            self._token.check()
            if on_stream_reset is not None:
                on_stream_reset()
            self._token.check()

        reply = self._wrapped.complete(
            messages,
            tools=tools,
            system=system,
            on_text_delta=text_delta if on_text_delta is not None else None,
            projection_hash=projection_hash,
            on_stream_reset=(
                stream_reset if on_stream_reset is not None else None
            ),
        )
        self._token.check()
        return reply


def rollback_to_boundary(messages: list[dict]) -> int:
    dropped = 0
    while messages and not (
        messages[-1]["role"] == "assistant"
        and not messages[-1].get("tool_calls")
    ):
        messages.pop()
        dropped += 1
    return dropped
