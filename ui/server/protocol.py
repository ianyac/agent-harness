"""Strict JSON vocabulary for the local session WebSocket."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    field_validator,
)


NonEmptyId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TurnMode = Literal["base", "plan"]
BaseMode = Literal["default", "acceptAll", "readOnly"]
PermissionDecision = Literal["yes", "no", "always"]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TextMessage(ProtocolModel):
    text: str
    mode: TurnMode

    @field_validator("text")
    @classmethod
    def trim_and_require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class UserMessage(_TextMessage):
    type: Literal["send_message"] = "send_message"


class QueuedMessage(_TextMessage):
    type: Literal["queue_message"] = "queue_message"


class CancelTurn(ProtocolModel):
    type: Literal["cancel_turn"] = "cancel_turn"
    turn_id: NonEmptyId


class PermissionAnswer(ProtocolModel):
    type: Literal["answer_permission"] = "answer_permission"
    request_id: NonEmptyId
    answer: PermissionDecision


class PlanAnswer(ProtocolModel):
    type: Literal["answer_plan"] = "answer_plan"
    request_id: NonEmptyId
    approved: bool
    feedback: str = ""


class SetSessionMode(ProtocolModel):
    type: Literal["set_session_mode"] = "set_session_mode"
    mode: BaseMode


class ClearQueuedMessage(ProtocolModel):
    type: Literal["clear_queued_message"] = "clear_queued_message"


ClientEvent = Annotated[
    Union[
        UserMessage,
        QueuedMessage,
        CancelTurn,
        PermissionAnswer,
        PlanAnswer,
        SetSessionMode,
        ClearQueuedMessage,
    ],
    Field(discriminator="type"),
]

_client_event_adapter = TypeAdapter(ClientEvent)


def parse_client_event(raw: str) -> ClientEvent:
    """Validate one JSON text frame received from a WebSocket client."""
    return _client_event_adapter.validate_json(raw)


class EventEnvelope(ProtocolModel):
    session_id: NonEmptyId
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1)
    turn_id: NonEmptyId | None = None


class SessionSnapshot(EventEnvelope):
    type: Literal["session_snapshot"] = "session_snapshot"
    messages: list[dict]
    running: bool
    queued_message: QueuedMessage | None = None
    safety: dict[str, JsonValue]


class TurnStarted(EventEnvelope):
    type: Literal["turn_started"] = "turn_started"
    turn_id: NonEmptyId
    mode: TurnMode


class AssistantDelta(EventEnvelope):
    type: Literal["assistant_delta"] = "assistant_delta"
    turn_id: NonEmptyId
    text: str


class StreamReset(EventEnvelope):
    type: Literal["stream_reset"] = "stream_reset"
    turn_id: NonEmptyId


class ActivityStarted(EventEnvelope):
    type: Literal["activity_started"] = "activity_started"
    turn_id: NonEmptyId
    activity_id: NonEmptyId
    parent_activity_id: NonEmptyId | None = None
    actor: NonEmptyId
    name: NonEmptyId
    args: dict[str, JsonValue]
    started_at: str


class ActivityCompleted(EventEnvelope):
    type: Literal["activity_completed"] = "activity_completed"
    turn_id: NonEmptyId
    activity_id: NonEmptyId
    parent_activity_id: NonEmptyId | None = None
    actor: NonEmptyId
    name: NonEmptyId
    args: dict[str, JsonValue]
    result: JsonValue
    is_error: bool
    started_at: str
    duration_ms: int = Field(ge=0)


class PermissionRequested(EventEnvelope):
    type: Literal["permission_requested"] = "permission_requested"
    turn_id: NonEmptyId
    request_id: NonEmptyId
    action: NonEmptyId
    scope: str
    reason: str


class PermissionResolved(EventEnvelope):
    type: Literal["permission_resolved"] = "permission_resolved"
    turn_id: NonEmptyId
    request_id: NonEmptyId
    answer: PermissionDecision


class PlanApprovalRequested(EventEnvelope):
    type: Literal["plan_approval_requested"] = "plan_approval_requested"
    turn_id: NonEmptyId
    request_id: NonEmptyId
    plan: str


class PlanApprovalResolved(EventEnvelope):
    type: Literal["plan_approval_resolved"] = "plan_approval_resolved"
    turn_id: NonEmptyId
    request_id: NonEmptyId
    approved: bool
    feedback: str = ""


class ContextUpdated(EventEnvelope):
    type: Literal["context_updated"] = "context_updated"
    context: dict[str, JsonValue]


class TurnStopping(EventEnvelope):
    type: Literal["turn_stopping"] = "turn_stopping"
    turn_id: NonEmptyId


class TurnCompleted(EventEnvelope):
    type: Literal["turn_completed"] = "turn_completed"
    turn_id: NonEmptyId
    messages: list[dict]
    final_text: str


class TurnCancelled(EventEnvelope):
    type: Literal["turn_cancelled"] = "turn_cancelled"
    turn_id: NonEmptyId


class TurnFailed(EventEnvelope):
    type: Literal["turn_failed"] = "turn_failed"
    turn_id: NonEmptyId
    error_category: NonEmptyId
    message: str


class SafetyUpdated(EventEnvelope):
    type: Literal["safety_updated"] = "safety_updated"
    safety: dict[str, JsonValue]


ServerEvent = Annotated[
    Union[
        SessionSnapshot,
        TurnStarted,
        AssistantDelta,
        StreamReset,
        ActivityStarted,
        ActivityCompleted,
        PermissionRequested,
        PermissionResolved,
        PlanApprovalRequested,
        PlanApprovalResolved,
        ContextUpdated,
        TurnStopping,
        TurnCompleted,
        TurnCancelled,
        TurnFailed,
        SafetyUpdated,
    ],
    Field(discriminator="type"),
]


def dump_server_event(event: ServerEvent) -> str:
    """Return a compact JSON text frame for a validated server event."""
    return event.model_dump_json()
