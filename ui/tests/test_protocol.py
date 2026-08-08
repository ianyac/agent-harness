import json

import pytest
from pydantic import ValidationError

from server.protocol import (
    ActivityCompleted,
    ActivityStarted,
    AssistantDelta,
    ClientEvent,
    ContextUpdated,
    CancelTurn,
    ClearQueuedMessage,
    PermissionAnswer,
    PermissionRequested,
    PermissionResolved,
    PlanApprovalRequested,
    PlanApprovalResolved,
    PlanAnswer,
    QueuedMessage,
    SafetyUpdated,
    SetSessionMode,
    ServerEvent,
    SessionSnapshot,
    StreamReset,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    TurnStopping,
    UserMessage,
    dump_server_event,
    parse_client_event,
)


def test_parse_user_message_rejects_blank_text():
    with pytest.raises(ValidationError):
        parse_client_event('{"type":"send_message","text":"   ","mode":"base"}')


def test_permission_answer_requires_known_value():
    with pytest.raises(ValidationError):
        PermissionAnswer(
            type="answer_permission",
            request_id="p1",
            answer="sometimes",
        )


def test_user_message_is_trimmed_but_preserves_internal_newlines():
    event = parse_client_event(
        '{"type":"send_message","text":"  first\\nsecond  ","mode":"plan"}'
    )
    assert event == UserMessage(text="first\nsecond", mode="plan")


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        ('{"type":"send_message","text":"hello","mode":"base"}', UserMessage),
        ('{"type":"queue_message","text":"later","mode":"plan"}', QueuedMessage),
        ('{"type":"cancel_turn","turn_id":"t1"}', CancelTurn),
        ('{"type":"answer_permission","request_id":"p1","answer":"always"}', PermissionAnswer),
        ('{"type":"answer_plan","request_id":"r1","approved":false,"feedback":"revise"}', PlanAnswer),
        ('{"type":"set_session_mode","mode":"readOnly"}', SetSessionMode),
        ('{"type":"clear_queued_message"}', ClearQueuedMessage),
    ],
)
def test_parse_client_event_supports_the_complete_client_vocabulary(raw, expected_type):
    assert isinstance(parse_client_event(raw), expected_type)


def test_client_events_reject_unknown_fields_and_blank_identifiers():
    with pytest.raises(ValidationError):
        parse_client_event('{"type":"cancel_turn","turn_id":" "}')
    with pytest.raises(ValidationError):
        parse_client_event('{"type":"cancel_turn","turn_id":"t1","unexpected":true}')


def test_dump_server_event_round_trips_every_server_event_type():
    events: list[ServerEvent] = [
        SessionSnapshot(
            type="session_snapshot",
            session_id="s1",
            generation=1,
            sequence=1,
            messages=[{"role": "user", "content": "hello"}],
            running=False,
            queued_message=None,
            safety={"mode": "default", "sandbox": {"backend": "none"}},
        ),
        TurnStarted(type="turn_started", session_id="s1", generation=1, sequence=2, turn_id="t1", mode="plan"),
        AssistantDelta(type="assistant_delta", session_id="s1", generation=1, sequence=3, turn_id="t1", text="hello"),
        StreamReset(type="stream_reset", session_id="s1", generation=1, sequence=4, turn_id="t1"),
        ActivityStarted(
            type="activity_started", session_id="s1", generation=1, sequence=5, turn_id="t1",
            activity_id="a1", parent_activity_id=None, actor="tool", name="read_file",
            args={"path": "README.md"}, started_at="2026-08-08T00:00:00Z",
        ),
        ActivityCompleted(
            type="activity_completed", session_id="s1", generation=1, sequence=6, turn_id="t1",
            activity_id="a1", parent_activity_id=None, actor="tool", name="read_file",
            args={"path": "README.md"}, result={"content": "ok"}, is_error=False,
            started_at="2026-08-08T00:00:00Z", duration_ms=12,
        ),
        PermissionRequested(
            type="permission_requested", session_id="s1", generation=1, sequence=7, turn_id="t1",
            request_id="p1", action="write_file", scope="README.md", reason="Needs an edit",
        ),
        PermissionResolved(
            type="permission_resolved", session_id="s1", generation=1, sequence=8, turn_id="t1",
            request_id="p1", answer="yes",
        ),
        PlanApprovalRequested(
            type="plan_approval_requested", session_id="s1", generation=1, sequence=9, turn_id="t1",
            request_id="r1", plan="1. Make the change",
        ),
        PlanApprovalResolved(
            type="plan_approval_resolved", session_id="s1", generation=1, sequence=10, turn_id="t1",
            request_id="r1", approved=True, feedback="",
        ),
        ContextUpdated(
            type="context_updated", session_id="s1", generation=1, sequence=11, turn_id="t1",
            context={"mode": "compaction", "used_tokens": 123},
        ),
        TurnStopping(type="turn_stopping", session_id="s1", generation=1, sequence=12, turn_id="t1"),
        TurnCompleted(
            type="turn_completed", session_id="s1", generation=1, sequence=13, turn_id="t1",
            messages=[{"role": "assistant", "content": "done"}], final_text="done",
        ),
        TurnCancelled(type="turn_cancelled", session_id="s1", generation=1, sequence=14, turn_id="t1"),
        TurnFailed(
            type="turn_failed", session_id="s1", generation=1, sequence=15, turn_id="t1",
            error_category="provider", message="Connection failed",
        ),
        SafetyUpdated(
            type="safety_updated", session_id="s1", generation=1, sequence=16,
            safety={"mode": "readOnly", "sandbox": {"backend": "none"}},
        ),
    ]

    for event in events:
        assert json.loads(dump_server_event(event))["type"] == event.type


def test_server_events_enforce_envelope_bounds_and_forbid_extra_fields():
    with pytest.raises(ValidationError):
        AssistantDelta(
            type="assistant_delta", session_id="s1", generation=0, sequence=1,
            text="hello", extra="nope",
        )
