"""Synchronous harness turn runner that emits the UI protocol vocabulary."""

from __future__ import annotations

import contextvars
import copy
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import httpx
from harness.llm import RetryableHTTPError
from harness.loop import run_turn
from harness.tools.base import Tool
from server.bridge import (
    CancellableLLM,
    CancellationToken,
    DecisionBroker,
    EventSink,
    TurnCancelled,
    rollback_to_boundary,
)
from server.registry import build_system
from server.protocol import MalformedUnicodeError, validate_unicode_scalars
from server.runtime import HarnessRuntime


@dataclass
class _TurnContext:
    runtime: HarnessRuntime
    sink: EventSink
    turn_id: str
    token: CancellationToken
    safety: dict


_current_turn: contextvars.ContextVar[_TurnContext | None] = (
    contextvars.ContextVar("ui_current_turn", default=None)
)
_current_activity: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ui_current_activity", default=None
)
_runtime_setup_lock = threading.Lock()


class _MalformedToolPayload(BaseException):
    """Escape the harness's ordinary tool-error conversion for unsafe data."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safety(runtime: HarnessRuntime) -> dict:
    return asdict(runtime.safety_snapshot())


def _emit_safety_if_changed(context: _TurnContext) -> None:
    safety = _safety(context.runtime)
    if safety != context.safety:
        context.safety = safety
        context.sink.emit("safety_updated", safety=safety)


def _activity_tool(tool: Tool) -> Tool:
    execute = tool.execute

    def observed_execute(**args):
        context = _current_turn.get()
        if context is None:
            return execute(**args)
        context.token.check()
        _emit_safety_if_changed(context)
        activity_id = uuid.uuid4().hex
        parent_id = _current_activity.get()
        started_at = _timestamp()
        started = time.monotonic_ns()
        actor = "subagent" if tool.spawns_subagents else "tool"
        _emit_activity_event(
            context,
            "activity_started",
            turn_id=context.turn_id,
            activity_id=activity_id,
            parent_activity_id=parent_id,
            actor=actor,
            name=tool.name,
            args=args,
            started_at=started_at,
        )
        activity_token = _current_activity.set(activity_id)
        try:
            try:
                result = execute(**args)
            except Exception as error:
                rendered = f"Error: {type(error).__name__}: {error}"
                _emit_activity_event(
                    context,
                    "activity_completed",
                    turn_id=context.turn_id,
                    activity_id=activity_id,
                    parent_activity_id=parent_id,
                    actor=actor,
                    name=tool.name,
                    args=args,
                    result=rendered,
                    is_error=True,
                    started_at=started_at,
                    duration_ms=(time.monotonic_ns() - started) // 1_000_000,
                )
                raise
            _emit_activity_event(
                context,
                "activity_completed",
                turn_id=context.turn_id,
                activity_id=activity_id,
                parent_activity_id=parent_id,
                actor=actor,
                name=tool.name,
                args=args,
                result=result,
                is_error=False,
                started_at=started_at,
                duration_ms=(time.monotonic_ns() - started) // 1_000_000,
            )
            _emit_safety_if_changed(context)
            context.token.check()
            return result
        finally:
            _current_activity.reset(activity_token)

    return replace(tool, execute=observed_execute)


def _emit_activity_event(
    context: _TurnContext,
    event_type: str,
    **payload: object,
) -> None:
    try:
        validate_unicode_scalars(payload)
    except MalformedUnicodeError as error:
        raise _MalformedToolPayload(
            "malformed Unicode string in tool activity data"
        ) from error
    context.sink.emit(event_type, **payload)


def _prepare_runtime(runtime: HarnessRuntime) -> threading.Lock:
    with _runtime_setup_lock:
        turn_lock = getattr(runtime, "_ui_turn_lock", None)
        if turn_lock is None:
            turn_lock = threading.Lock()
            setattr(runtime, "_ui_turn_lock", turn_lock)
        if not getattr(runtime, "_ui_activity_wrapped", False):
            for name, tool in list(runtime.tools.items()):
                runtime.tools[name] = _activity_tool(tool)
            setattr(runtime, "_ui_activity_wrapped", True)
        return turn_lock


def _error_category(error: BaseException) -> str:
    if isinstance(error, _MalformedToolPayload):
        return "invalid_response"
    if isinstance(
        error,
        (TimeoutError, ConnectionError, httpx.TransportError, RetryableHTTPError),
    ):
        return "provider"
    if isinstance(error, RuntimeError) and (
        str(error).startswith("codex HTTP ")
        or str(error) == "codex stream ended without response.completed"
    ):
        return "provider"
    if isinstance(error, OSError):
        return "filesystem"
    if isinstance(error, ValueError):
        return "invalid_response"
    return "internal"


class TurnRunner:
    def __init__(
        self,
        runtime: HarnessRuntime,
        decisions: DecisionBroker | None = None,
    ) -> None:
        self.runtime = runtime
        self.decisions = decisions or DecisionBroker()
        self._turn_lock = _prepare_runtime(runtime)
        self._authoritative_messages_before_turn: list[dict] | None = None

    def run(
        self,
        text: str,
        mode: str,
        turn_id: str,
        sink: EventSink,
        token: CancellationToken,
        submission_id: str | None = None,
    ) -> None:
        if mode not in ("base", "plan"):
            raise ValueError(f"invalid turn mode: {mode!r}")
        with self._turn_lock:
            self._run_locked(text, mode, turn_id, sink, token, submission_id)

    def _run_locked(
        self,
        text: str,
        mode: str,
        turn_id: str,
        sink: EventSink,
        token: CancellationToken,
        submission_id: str | None,
    ) -> None:
        runtime = self.runtime
        context: _TurnContext | None = None
        turn_context: contextvars.Token[_TurnContext | None] | None = None
        activity_context: contextvars.Token[str | None] | None = None
        try:
            if getattr(runtime, "_ui_durability_failed", False):
                self._restore_authoritative_messages()
            self._authoritative_messages_before_turn = copy.deepcopy(
                runtime.messages
            )
            runtime.policy.mode = (
                "plan" if mode == "plan" else runtime.policy.base_mode
            )
            context = _TurnContext(runtime, sink, turn_id, token, _safety(runtime))
            turn_context = _current_turn.set(context)
            activity_context = _current_activity.set(None)
            runtime.bind_plan_reviewer(self._review_plan)
            sink.emit(
                "turn_started",
                turn_id=turn_id,
                mode=mode,
                submission_id=submission_id,
            )
            reply = run_turn(
                runtime.messages,
                text,
                CancellableLLM(runtime.llm, token),
                tools=runtime.tools,
                on_tool_call=lambda name, args: self._validate_tool_call(
                    name, args, token
                ),
                policy=runtime.policy,
                asker=self._ask_permission,
                system=lambda: build_system(runtime),
                compact_threshold=runtime.compact_threshold,
                on_compact=self._record_compaction,
                breadcrumbs=None,
                on_text_delta=lambda delta: sink.emit(
                    "assistant_delta", turn_id=turn_id, text=delta
                ),
                context=runtime.folding,
                on_stream_reset=lambda: sink.emit(
                    "stream_reset", turn_id=turn_id
                ),
            )
            token.check()
            self._restore_base_mode(context)
            self._record_authoritative_turn()
            sink.emit(
                "turn_completed",
                turn_id=turn_id,
                messages=copy.deepcopy(runtime.messages),
                final_text=reply.get("content") or "",
            )
        except TurnCancelled:
            self._finish_cancelled(sink, turn_id)
        except _MalformedToolPayload as error:
            self._finish_failed(sink, turn_id, error)
        except Exception as error:
            self._finish_failed(sink, turn_id, error)
        finally:
            runtime.policy.mode = runtime.policy.base_mode
            try:
                if activity_context is not None:
                    _current_activity.reset(activity_context)
            finally:
                if turn_context is not None:
                    _current_turn.reset(turn_context)

    def _finish_cancelled(
        self,
        sink: EventSink,
        turn_id: str,
    ) -> None:
        try:
            rollback_to_boundary(self.runtime.messages)
        except Exception:
            pass
        self._restore_base_mode_safely()
        self._record_surviving_boundary()
        self._emit_terminal_safely(sink, "turn_cancelled", turn_id=turn_id)

    def _finish_failed(
        self,
        sink: EventSink,
        turn_id: str,
        error: BaseException,
    ) -> None:
        try:
            rollback_to_boundary(self.runtime.messages)
        except Exception:
            pass
        self._restore_base_mode_safely()
        self._record_surviving_boundary()
        self._emit_terminal_safely(
            sink,
            "turn_failed",
            turn_id=turn_id,
            error_category=_error_category(error),
            message=self._safe_error_message(error),
        )

    def _restore_base_mode_safely(self) -> None:
        self.runtime.policy.mode = self.runtime.policy.base_mode

    @staticmethod
    def _emit_terminal_safely(
        sink: EventSink,
        event_type: str,
        **payload: object,
    ) -> None:
        try:
            sink.emit(event_type, **payload)
        except Exception:
            pass

    def _ask_permission(self, action: str, args: dict) -> str:
        context = self._require_context()
        context.token.check()
        request_id = uuid.uuid4().hex

        def announce() -> None:
            context.sink.emit(
                "permission_requested",
                turn_id=context.turn_id,
                request_id=request_id,
                action=action,
                scope=json.dumps(args, sort_keys=True, separators=(",", ":")),
                reason=f"{action} requires permission",
            )

        answer = self.decisions.request_permission(
            request_id, announce, token=context.token
        )
        context.sink.emit(
            "permission_resolved",
            turn_id=context.turn_id,
            request_id=request_id,
            answer=answer,
        )
        context.token.check()
        return answer

    @staticmethod
    def _validate_tool_call(
        name: str,
        args: dict,
        token: CancellationToken,
    ) -> None:
        token.check()
        validate_unicode_scalars({"name": name, "args": args})
        token.check()

    def _review_plan(self, plan: str) -> tuple[bool, str]:
        context = self._require_context()
        context.token.check()
        request_id = uuid.uuid4().hex

        def announce() -> None:
            context.sink.emit(
                "plan_approval_requested",
                turn_id=context.turn_id,
                request_id=request_id,
                plan=plan,
            )

        approved, feedback = self.decisions.request_plan(
            request_id, announce, token=context.token
        )
        context.sink.emit(
            "plan_approval_resolved",
            turn_id=context.turn_id,
            request_id=request_id,
            approved=approved,
            feedback=feedback,
        )
        context.token.check()
        return approved, feedback

    def _record_compaction(self, cut: int) -> None:
        context = self._require_context()
        context.token.check()
        summary = context.runtime.messages[0]
        validate_unicode_scalars(context.runtime.messages)
        try:
            context.runtime.session_log.record_compaction(cut, summary)
        except Exception as error:
            self._restore_authoritative_messages_after(error)
            raise
        trusted = self._authoritative_messages_before_turn or []
        self._authoritative_messages_before_turn = [
            copy.deepcopy(summary),
            *copy.deepcopy(trusted[cut:]),
        ]
        context.sink.emit(
            "context_updated",
            turn_id=context.turn_id,
            context={"mode": "compaction", "summarized_messages": cut},
        )
        context.token.check()

    def _record_surviving_boundary(self) -> None:
        if getattr(self.runtime, "_ui_durability_failed", False):
            try:
                self._restore_authoritative_messages()
            except Exception:
                return
            return
        try:
            self._record_authoritative_turn()
        except Exception:
            # The original turn failure remains authoritative. The helper has
            # already restored the live list and SessionLog cursor from disk.
            pass

    def _record_authoritative_turn(self) -> None:
        try:
            validate_unicode_scalars(self.runtime.messages)
            self.runtime.session_log.record_turn(self.runtime.messages)
        except Exception as error:
            self._restore_authoritative_messages_after(error)
            raise
        self._authoritative_messages_before_turn = copy.deepcopy(
            self.runtime.messages
        )

    def _restore_authoritative_messages_after(self, original: Exception) -> None:
        try:
            self._restore_authoritative_messages()
        except Exception as recovery_error:
            trusted = self._authoritative_messages_before_turn
            self.runtime.messages[:] = (
                copy.deepcopy(trusted) if trusted is not None else []
            )
            setattr(self.runtime, "_ui_durability_failed", True)
            original.add_note(
                "authoritative transcript reload failed: "
                f"{type(recovery_error).__name__}: {recovery_error}"
            )

    def _restore_authoritative_messages(self) -> None:
        restored = self.runtime.session_log.load()
        validate_unicode_scalars(restored)
        self.runtime.messages[:] = copy.deepcopy(restored)
        self._authoritative_messages_before_turn = copy.deepcopy(restored)
        setattr(self.runtime, "_ui_durability_failed", False)

    @staticmethod
    def _safe_error_message(error: BaseException) -> str:
        message = str(error)
        try:
            validate_unicode_scalars(message)
        except MalformedUnicodeError:
            return "upstream failure contained malformed Unicode"
        return message

    @staticmethod
    def _require_context() -> _TurnContext:
        context = _current_turn.get()
        if context is None:
            raise RuntimeError("turn callback invoked outside an active turn")
        return context

    @staticmethod
    def _restore_base_mode(context: _TurnContext) -> None:
        context.runtime.policy.mode = context.runtime.policy.base_mode
        _emit_safety_if_changed(context)
