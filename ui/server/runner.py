"""TurnRunner: one session's bridge from the blocking run_turn to an
event sink.

The caller owns the worker thread: call try_begin() (atomically claims
the turn slot), then run_turn_blocking() on a thread. answer_permission()
and cancel() are called from other threads. emit must be thread-safe."""

import inspect
import itertools
import threading
from concurrent.futures import Future
from dataclasses import replace
from typing import Callable

from harness.loop import run_turn
from harness.tools.base import Tool

from ui.server import events


class TurnCancelled(Exception):
    """Raised inside harness callbacks to abort the running turn."""


class TurnRunner:
    def __init__(
        self,
        llm,
        tools: dict[str, Tool],
        policy,
        system_prompt: Callable[[], str],
        messages: list[dict],
        emit: Callable[[dict], None],
        run_turn_fn=run_turn,
        subagent_system_prompt: Callable[[], str] | None = None,
        compact_threshold: int | None = None,
        keep_recent: int = 8,
    ):
        self._llm = llm
        self._policy = policy
        self._system_prompt = system_prompt  # re-evaluated per turn, like main.py
        self._emit = emit
        self._run_turn = run_turn_fn
        self._subagent_system_prompt = subagent_system_prompt
        self._compact_threshold = compact_threshold
        self._keep_recent = keep_recent
        # degraded mode until the streaming seam lands
        # (docs/streams/ui/2026-07-06-seam-token-streaming.md): probe the
        # signature instead of editing harness/
        self._streaming = (
            "on_text_delta" in inspect.signature(run_turn_fn).parameters
        )
        self.messages = messages
        self._tools = {name: self._wrap(tool) for name, tool in tools.items()}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.running = False
        self.pending_permission: dict | None = None
        self.streamed_text = ""
        self._permission_future: Future | None = None
        self._cancelled = False

    # -- called from the event-loop thread --------------------------------
    def try_begin(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self._cancelled = False
            return True

    # -- called from any thread --------------------------------------------
    def answer_permission(self, request_id: str, answer: str) -> None:
        with self._lock:
            pending, future = self.pending_permission, self._permission_future
            if pending is None or pending["id"] != request_id or future is None:
                return  # stale or unknown: ignore, never crash the socket
            self._permission_future = None
        future.set_result(answer)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            future, self._permission_future = self._permission_future, None
        if future is not None:
            future.set_result("no")  # wake the asker; it raises TurnCancelled

    # -- worker thread -------------------------------------------------------
    def run_turn_blocking(self, text: str) -> None:
        self.streamed_text = ""
        self._emit(events.turn_started())
        extra = {"on_text_delta": self._on_text_delta} if self._streaming else {}
        try:
            self._run_turn(
                self.messages,
                text,
                self._llm,
                tools=self._tools,
                on_tool_call=self._on_tool_call,
                policy=self._policy,
                asker=self._asker,
                system=self._system_prompt(),
                compact_threshold=self._compact_threshold,
                keep_recent=self._keep_recent,
                on_compact=self._on_compact,
                **extra,
            )
        except TurnCancelled:
            self._rollback()
            self._emit(events.turn_cancelled())
        except Exception as error:  # noqa: BLE001 — surfaced to the browser
            self._rollback()
            self._emit(events.turn_error(f"{type(error).__name__}: {error}"))
        else:
            self._emit(events.turn_done(list(self.messages)))
        finally:
            self.streamed_text = ""
            with self._lock:
                self.running = False
                self.pending_permission = None
                self._permission_future = None

    def _rollback(self) -> None:
        # mirror lesson-12 main.py: compaction may have shifted indices
        # mid-turn, so pop back to the last completed exchange instead of
        # slicing at a saved position
        while self.messages and not (
            self.messages[-1]["role"] == "assistant"
            and not self.messages[-1].get("tool_calls")
        ):
            self.messages.pop()

    # -- harness callbacks (worker thread) -----------------------------------
    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise TurnCancelled()

    def _on_text_delta(self, text: str) -> None:
        self._check_cancelled()
        self.streamed_text += text
        self._emit(events.text_delta(text))

    def _on_tool_call(self, name: str, args: dict) -> None:
        self._check_cancelled()
        self.streamed_text = ""  # a non-delta event closes the open bubble
        self._emit(events.tool_call(name, args))

    def _on_compact(self, summarized: int) -> None:
        self._emit(events.compaction(summarized))

    def _asker(self, name: str, args: dict) -> str:
        self._check_cancelled()
        request = {"id": f"perm-{next(self._ids)}", "name": name, "args": args}
        future = Future()
        with self._lock:
            self.pending_permission = request
            self._permission_future = future
        self.streamed_text = ""
        self._emit(events.permission_request(request["id"], name, args))
        answer = future.result()
        with self._lock:
            self.pending_permission = None
        self._check_cancelled()
        return answer

    def _wrap(self, tool: Tool) -> Tool:
        inner = tool.execute

        def execute(**kwargs):
            result = inner(**kwargs)
            self._emit(events.tool_result(tool.name, result))
            self._check_cancelled()
            return result

        return replace(tool, execute=execute)
