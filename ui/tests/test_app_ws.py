import json
import sqlite3
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.exceptions import StarletteDeprecationWarning
from starlette.websockets import WebSocket, WebSocketDisconnect

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from server.app import AppSettings, create_app
from tests.fake_llm import FakeLLM


ORIGIN = "http://testserver"
SECRET = "websocket-test-secret"
REST_HEADERS = {
    "Authorization": f"Bearer {SECRET}",
    "Origin": ORIGIN,
}


class WholeTextLLM:
    context_window = 128_000

    def __init__(self, *responses: str):
        self.responses = list(responses)

    def complete(
        self,
        _messages,
        tools=None,
        system=None,
        on_text_delta=None,
        projection_hash=None,
        on_stream_reset=None,
    ):
        content = self.responses.pop(0)
        if on_text_delta is not None:
            on_text_delta(content)
        return {"role": "assistant", "content": content}


class BlockingLLM:
    context_window = 128_000

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(
        self,
        _messages,
        tools=None,
        system=None,
        on_text_delta=None,
        projection_hash=None,
        on_stream_reset=None,
    ):
        if on_text_delta is not None:
            on_text_delta("partial")
        self.started.set()
        assert self.release.wait(timeout=3)
        return {"role": "assistant", "content": "finished"}


class BlockingFirstTurnLLM:
    context_window = 128_000

    def __init__(self):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(
        self,
        _messages,
        tools=None,
        system=None,
        on_text_delta=None,
        projection_hash=None,
        on_stream_reset=None,
    ):
        self.calls += 1
        if self.calls == 1:
            if on_text_delta is not None:
                on_text_delta("partial")
            self.started.set()
            assert self.release.wait(timeout=3)
            content = "first done"
        else:
            content = "second done"
            if on_text_delta is not None:
                on_text_delta(content)
        return {"role": "assistant", "content": content}


class ReconnectThenPermissionLLM:
    context_window = 128_000

    def __init__(self):
        self.calls = 0
        self.streaming = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def complete(
        self,
        _messages,
        tools=None,
        system=None,
        on_text_delta=None,
        projection_hash=None,
        on_stream_reset=None,
    ):
        self.calls += 1
        if self.calls == 1:
            if on_text_delta is not None:
                on_text_delta("thinking")
            self.streaming.set()
            assert self.release.wait(timeout=3)
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"after-reconnect.txt","content":"yes"}',
                        },
                    }
                ],
            }
        if on_text_delta is not None:
            on_text_delta("done")
        self.finished.set()
        return {"role": "assistant", "content": "done"}


class FinishedFakeLLM(FakeLLM):
    def __init__(self, script: list[dict]):
        super().__init__(script)
        self.finished = threading.Event()

    def complete(self, *args, **kwargs):
        reply = super().complete(*args, **kwargs)
        if not reply.get("tool_calls"):
            self.finished.set()
        return reply


@pytest.fixture
def service(tmp_path: Path):
    count = 0

    @contextmanager
    def run(llm, *, mode: str = "default"):
        nonlocal count
        count += 1
        root = tmp_path / f"service-{count}"
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        settings = AppSettings(
            metadata_path=root / "metadata.sqlite3",
            base_workspace=workspace,
            launch_secret=SECRET,
            allowed_origins=frozenset({ORIGIN}),
        )
        app = create_app(settings, lambda: llm)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.post(
                "/api/sessions",
                headers=REST_HEADERS,
                json={
                    "workspace": str(workspace),
                    "mode": mode,
                    "context_mode": "compaction",
                    "title": "Socket test",
                },
            )
            assert response.status_code == 201, response.text
            yield client, response.json()["session_id"], workspace, app

    return run


def connect(client: TestClient, session_id: str):
    return client.websocket_connect(
        f"/ws/sessions/{session_id}",
        headers={"Origin": ORIGIN},
        subprotocols=["harness-ui", SECRET],
    )


def receive_until(ws, event_type: str) -> tuple[list[dict], dict]:
    events = []
    for _ in range(30):
        event = ws.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events, event
        if event["type"] in {"turn_completed", "turn_cancelled", "turn_failed"}:
            raise AssertionError(
                f"received terminal {event['type']} before {event_type}"
            )
    raise AssertionError(f"did not receive {event_type}")


def test_websocket_sends_snapshot_before_turn_events(service):
    with service(WholeTextLLM("hello back")) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "session_snapshot"
            assert snapshot["sequence"] == 1
            assert snapshot["generation"] == 1
            assert snapshot["messages"] == []
            assert snapshot["running"] is False
            assert snapshot["queued_message"] is None

            ws.send_json({"type": "send_message", "text": "hello", "mode": "base"})
            started = ws.receive_json()
            delta = ws.receive_json()
            done = ws.receive_json()

            assert [started["type"], delta["type"], done["type"]] == [
                "turn_started",
                "assistant_delta",
                "turn_completed",
            ]
            assert [started["sequence"], delta["sequence"], done["sequence"]] == [
                2,
                3,
                4,
            ]
            assert done["messages"][-1]["content"] == "hello back"


def test_duplicate_connection_supersedes_sender_and_restamps_future_events(service):
    llm = BlockingLLM()
    with service(llm) as (client, session_id, _, _):
        with connect(client, session_id) as first:
            assert first.receive_json()["generation"] == 1
            first.send_json(
                {"type": "send_message", "text": "hello", "mode": "base"}
            )
            started = first.receive_json()
            assert started["type"] == "turn_started"
            assert first.receive_json()["type"] == "assistant_delta"

            with connect(client, session_id) as second:
                snapshot = second.receive_json()
                assert snapshot["type"] == "session_snapshot"
                assert snapshot["generation"] == 2
                assert snapshot["sequence"] == 1
                assert snapshot["running"] is True
                assert snapshot["turn_id"] == started["turn_id"]

                with pytest.raises(WebSocketDisconnect) as superseded:
                    first.receive_json()
                assert superseded.value.code == 1000

                llm.release.set()
                events, completed = receive_until(second, "turn_completed")
                assert all(event["generation"] == 2 for event in events)
                assert [event["sequence"] for event in events] == list(
                    range(2, 2 + len(events))
                )
                assert completed["messages"][-1]["content"] == "finished"


def test_reconnect_between_terminal_production_and_cleanup_receives_terminal(
    service, monkeypatch
):
    from server import sessions as sessions_module

    terminal_produced = threading.Event()
    release_cleanup = threading.Event()
    original_relay_emit = sessions_module._EventRelay.emit
    original_sink_emit = sessions_module.EventSink.emit

    def coordinated_relay_emit(relay, event_type: str, **payload):
        original_relay_emit(relay, event_type, **payload)
        if event_type in {"turn_completed", "turn_cancelled", "turn_failed"}:
            terminal_produced.set()
            assert release_cleanup.wait(timeout=3)

    def coordinated_sink_emit(sink, event_type: str, **payload):
        if event_type in {"turn_completed", "turn_cancelled", "turn_failed"}:
            terminal_produced.set()
            assert release_cleanup.wait(timeout=3)
        original_sink_emit(sink, event_type, **payload)

    monkeypatch.setattr(
        sessions_module._EventRelay, "emit", coordinated_relay_emit
    )
    monkeypatch.setattr(sessions_module.EventSink, "emit", coordinated_sink_emit)
    with service(WholeTextLLM("done")) as (client, session_id, _, _):
        with connect(client, session_id) as first:
            first.receive_json()
            first.send_json(
                {"type": "send_message", "text": "finish", "mode": "base"}
            )
            assert first.receive_json()["type"] == "turn_started"
            assert first.receive_json()["type"] == "assistant_delta"
            assert terminal_produced.wait(timeout=2)

            with connect(client, session_id) as second:
                snapshot = second.receive_json()
                assert snapshot["running"] is True
                assert snapshot["generation"] == 2
                with pytest.raises(WebSocketDisconnect):
                    first.receive_json()

                received: list[dict | BaseException] = []

                def receive_terminal():
                    try:
                        received.append(second.receive_json())
                    except BaseException as error:
                        received.append(error)

                receiver = threading.Thread(target=receive_terminal)
                receiver.start()
                release_cleanup.set()
                receiver.join(timeout=0.5)
                if receiver.is_alive():
                    with connect(client, session_id) as third:
                        third.receive_json()
                    receiver.join(timeout=1)

                assert not receiver.is_alive()
                assert len(received) == 1
                assert isinstance(received[0], dict)
                assert received[0]["type"] == "turn_completed"
                assert received[0]["generation"] == 2
                assert received[0]["sequence"] == 2
                assert received[0]["messages"][-1]["content"] == "done"


def test_running_reconnect_snapshot_can_cancel_with_active_turn_id(service):
    llm = BlockingLLM()
    with service(llm) as (client, session_id, _, _):
        with connect(client, session_id) as first:
            first.receive_json()
            first.send_json(
                {"type": "send_message", "text": "cancel later", "mode": "base"}
            )
            started = first.receive_json()
            assert started["type"] == "turn_started"
            assert first.receive_json()["type"] == "assistant_delta"

            with connect(client, session_id) as second:
                snapshot = second.receive_json()
                try:
                    assert snapshot["turn_id"] == started["turn_id"]
                    second.send_json(
                        {"type": "cancel_turn", "turn_id": snapshot["turn_id"]}
                    )
                    stopping = second.receive_json()
                    assert stopping["type"] == "turn_stopping"
                finally:
                    llm.release.set()
                cancelled = second.receive_json()

                assert cancelled["type"] == "turn_cancelled"
                assert cancelled["generation"] == 2


def test_reconnected_socket_owns_permission_requested_after_supersession(service):
    llm = ReconnectThenPermissionLLM()
    with service(llm) as (client, session_id, workspace, _):
        first_context = connect(client, session_id)
        first = first_context.__enter__()
        try:
            first.receive_json()
            first.send_json(
                {"type": "send_message", "text": "write later", "mode": "base"}
            )
            assert first.receive_json()["type"] == "turn_started"
            assert first.receive_json()["type"] == "assistant_delta"

            with connect(client, session_id) as second:
                snapshot = second.receive_json()
                assert snapshot["generation"] == 2
                assert snapshot["running"] is True
                with pytest.raises(WebSocketDisconnect):
                    first.receive_json()
                first_context.__exit__(None, None, None)
                first_context = None

                llm.release.set()
                _, requested = receive_until(second, "permission_requested")
                second.send_json(
                    {
                        "type": "answer_permission",
                        "request_id": requested["request_id"],
                        "answer": "yes",
                    }
                )
                _, completed = receive_until(second, "turn_completed")

                assert completed["messages"][-1]["content"] == "done"
                assert (workspace / "after-reconnect.txt").read_text() == "yes"
        finally:
            if first_context is not None:
                first_context.__exit__(None, None, None)


def test_failed_accept_keeps_healthy_owner_and_does_not_consume_generation(
    service, monkeypatch
):
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "still-owned.txt", "content": "yes"},
                    }
                ],
            },
            {"type": "text", "content": "owner stayed live"},
        ]
    )
    with service(llm) as (client, session_id, workspace, _):
        first_context = connect(client, session_id)
        first = first_context.__enter__()
        try:
            first_snapshot = first.receive_json()
            assert first_snapshot["generation"] == 1

            original_accept = WebSocket.accept

            async def fail_accept(_websocket, *args, **kwargs):
                raise RuntimeError("peer left during accept")

            monkeypatch.setattr(WebSocket, "accept", fail_accept)
            try:
                with pytest.raises(RuntimeError, match="peer left during accept"):
                    connect(client, session_id).__enter__()
            finally:
                monkeypatch.setattr(WebSocket, "accept", original_accept)

            first.send_json(
                {"type": "send_message", "text": "write", "mode": "base"}
            )
            assert first.receive_json()["type"] == "turn_started"
            _, requested = receive_until(first, "permission_requested")
            first.send_json(
                {
                    "type": "answer_permission",
                    "request_id": requested["request_id"],
                    "answer": "yes",
                }
            )
            _, completed = receive_until(first, "turn_completed")
            assert completed["messages"][-1]["content"] == "owner stayed live"
            assert (workspace / "still-owned.txt").read_text() == "yes"

            with connect(client, session_id) as replacement:
                replacement_snapshot = replacement.receive_json()
                assert replacement_snapshot["generation"] == 2
                with pytest.raises(WebSocketDisconnect):
                    first.receive_json()
        finally:
            if first_context is not None:
                first_context.__exit__(None, None, None)


def test_permission_answers_require_the_visible_request_id(service):
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "approved.txt", "content": "yes"},
                    }
                ],
            },
            {"type": "text", "content": "done"},
        ]
    )
    with service(llm) as (client, session_id, workspace, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "write it", "mode": "base"})
            _, requested = receive_until(ws, "permission_requested")

            ws.send_json(
                {
                    "type": "answer_permission",
                    "request_id": "stale-request",
                    "answer": "yes",
                }
            )
            ws.send_json(
                {
                    "type": "answer_permission",
                    "request_id": requested["request_id"],
                    "answer": "yes",
                }
            )
            events, completed = receive_until(ws, "turn_completed")

            resolved = next(
                event for event in events if event["type"] == "permission_resolved"
            )
            assert resolved["request_id"] == requested["request_id"]
            assert resolved["answer"] == "yes"
            assert completed["messages"][-1]["content"] == "done"
            assert (workspace / "approved.txt").read_text() == "yes"


@pytest.mark.parametrize(
    ("approved", "feedback"),
    [(True, ""), (False, "Please revise the risky step.")],
)
def test_plan_answers_preserve_approval_and_feedback(
    service, approved: bool, feedback: str
):
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "exit_plan_mode",
                        "arguments": {"plan": "Make the change safely"},
                    }
                ],
            },
            {"type": "text", "content": "plan handled"},
        ]
    )
    with service(llm, mode="acceptAll") as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "plan", "mode": "plan"})
            _, requested = receive_until(ws, "plan_approval_requested")

            ws.send_json(
                {
                    "type": "answer_plan",
                    "request_id": "stale-request",
                    "approved": not approved,
                    "feedback": "wrong request",
                }
            )
            ws.send_json(
                {
                    "type": "answer_plan",
                    "request_id": requested["request_id"],
                    "approved": approved,
                    "feedback": feedback,
                }
            )
            events, _ = receive_until(ws, "turn_completed")

            resolved = next(
                event
                for event in events
                if event["type"] == "plan_approval_resolved"
            )
            assert resolved["request_id"] == requested["request_id"]
            assert resolved["approved"] is approved
            assert resolved["feedback"] == feedback


def test_cancellation_emits_stopping_then_rolls_back_before_terminal(service):
    llm = BlockingLLM()
    with service(llm) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "cancel me", "mode": "base"})
            started = ws.receive_json()
            assert started["type"] == "turn_started"
            assert ws.receive_json()["type"] == "assistant_delta"

            ws.send_json({"type": "cancel_turn", "turn_id": started["turn_id"]})
            stopping = ws.receive_json()
            assert stopping["type"] == "turn_stopping"
            assert stopping["turn_id"] == started["turn_id"]
            llm.release.set()
            cancelled = ws.receive_json()

            assert cancelled["type"] == "turn_cancelled"
            assert cancelled["sequence"] == stopping["sequence"] + 1

        with connect(client, session_id) as healed:
            snapshot = healed.receive_json()
            assert snapshot["running"] is False
            assert snapshot["messages"] == []


def test_queue_replacement_launches_only_the_latest_follow_up(service):
    llm = BlockingFirstTurnLLM()
    with service(llm) as (client, session_id, _, app):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "first", "mode": "base"})
            assert ws.receive_json()["type"] == "turn_started"
            assert ws.receive_json()["type"] == "assistant_delta"

            ws.send_json({"type": "queue_message", "text": "old", "mode": "base"})
            ws.send_json(
                {"type": "queue_message", "text": "latest", "mode": "plan"}
            )
            channel = app.state.session_manager._channels[session_id]
            for _ in range(1000):
                queued = channel.queued_message
                if (
                    queued is not None
                    and queued.text == "latest"
                    and queued.mode == "plan"
                ):
                    break
                time.sleep(0.001)
            assert channel.queued_message is not None
            assert channel.queued_message.text == "latest"
            assert channel.queued_message.mode == "plan"
            llm.release.set()

            _, first_done = receive_until(ws, "turn_completed")
            assert first_done["messages"][-1]["content"] == "first done"
            second_started = ws.receive_json()
            assert second_started["type"] == "turn_started"
            assert second_started["mode"] == "plan"
            _, second_done = receive_until(ws, "turn_completed")
            assert second_done["messages"][-2:] == [
                {"role": "user", "content": "latest"},
                {"role": "assistant", "content": "second done"},
            ]


def test_clear_queued_message_prevents_follow_up_and_snapshot_heals_queue(service):
    llm = BlockingFirstTurnLLM()
    with service(llm) as (client, session_id, _, app):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "first", "mode": "base"})
            assert ws.receive_json()["type"] == "turn_started"
            assert ws.receive_json()["type"] == "assistant_delta"
            ws.send_json({"type": "queue_message", "text": "later", "mode": "base"})
            channel = app.state.session_manager._channels[session_id]
            for _ in range(1000):
                if channel.queued_message is not None:
                    break
                time.sleep(0.001)
            assert channel.queued_message is not None
            ws.send_json({"type": "clear_queued_message"})
            for _ in range(1000):
                if channel.queued_message is None:
                    break
                time.sleep(0.001)
            assert channel.queued_message is None
            llm.release.set()
            receive_until(ws, "turn_completed")

        with connect(client, session_id) as healed:
            snapshot = healed.receive_json()
            assert snapshot["running"] is False
            assert snapshot["queued_message"] is None
            assert [message["content"] for message in snapshot["messages"]] == [
                "first",
                "first done",
            ]


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "unknown"},
        {"type": "send_message", "text": "   ", "mode": "base"},
        {"type": "send_message", "text": "hello", "mode": "base", "extra": 1},
        {"type": "send_message", "text": "hello", "mode": "default"},
    ],
)
def test_invalid_client_json_frames_close_with_policy_violation(service, frame: dict):
    with service(WholeTextLLM("unused")) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json(frame)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()

        assert closed.value.code == 1008


def test_binary_client_frame_closes_with_policy_violation(service):
    with service(WholeTextLLM("unused")) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_bytes(b'{"type":"clear_queued_message"}')
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()

        assert closed.value.code == 1008


def test_disconnect_during_permission_fails_closed_and_leaves_no_pending_turn(service):
    llm = FinishedFakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "must-not-exist.txt", "content": "no"},
                    }
                ],
            },
            {"type": "text", "content": "permission denied safely"},
            {"type": "text", "content": "next turn works"},
        ]
    )
    with service(llm) as (client, session_id, workspace, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "write", "mode": "base"})
            receive_until(ws, "permission_requested")

        assert llm.finished.wait(timeout=2)
        assert not (workspace / "must-not-exist.txt").exists()

        for _ in range(100):
            with connect(client, session_id) as healing:
                snapshot = healing.receive_json()
            if not snapshot["running"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("disconnected permission turn did not finish")

        with connect(client, session_id) as healed:
            snapshot = healed.receive_json()
            assert snapshot["running"] is False
            assert snapshot["messages"][-1]["content"] == "permission denied safely"
            healed.send_json(
                {"type": "send_message", "text": "again", "mode": "base"}
            )
            _, completed = receive_until(healed, "turn_completed")
            assert completed["messages"][-1]["content"] == "next turn works"


@pytest.mark.parametrize("send_error", [RuntimeError, OSError])
def test_outbound_send_failure_wakes_receiver_and_releases_turn_ownership(
    service, monkeypatch, send_error
):
    from server import sessions as sessions_module

    llm = FinishedFakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "must-not-exist.txt", "content": "no"},
                    }
                ],
            },
            {"type": "text", "content": "permission denied safely"},
            {"type": "text", "content": "next turn works"},
        ]
    )
    send_failed = threading.Event()
    released = threading.Event()
    release_calls: list[int] = []
    original_send_text = WebSocket.send_text
    original_release = sessions_module._SessionChannel.release

    async def fail_permission_send(websocket, data: str):
        if json.loads(data)["type"] == "permission_requested":
            send_failed.set()
            raise send_error("outbound transport failed")
        await original_send_text(websocket, data)

    def tracked_release(channel, connection):
        release_calls.append(connection.generation)
        original_release(channel, connection)
        released.set()

    monkeypatch.setattr(WebSocket, "send_text", fail_permission_send)
    monkeypatch.setattr(sessions_module._SessionChannel, "release", tracked_release)

    with service(llm) as (client, session_id, workspace, app):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json(
                {"type": "send_message", "text": "write", "mode": "base"}
            )
            assert ws.receive_json()["type"] == "turn_started"
            assert send_failed.wait(timeout=1)
            assert released.wait(timeout=1)
            assert llm.finished.wait(timeout=1)

            channel = app.state.session_manager._channels[session_id]
            for _ in range(1000):
                if not channel.running:
                    break
                time.sleep(0.001)
            assert channel.running is False
            assert channel.current is None
            assert channel.worker is None
            assert channel.runner is None
            assert release_calls == [1]
            assert not (workspace / "must-not-exist.txt").exists()

        monkeypatch.setattr(WebSocket, "send_text", original_send_text)
        with connect(client, session_id) as healed:
            snapshot = healed.receive_json()
            assert snapshot["generation"] == 2
            assert snapshot["running"] is False
            healed.send_json(
                {"type": "send_message", "text": "again", "mode": "base"}
            )
            _, completed = receive_until(healed, "turn_completed")
            assert completed["messages"][-1]["content"] == "next turn works"


def test_second_simultaneous_turn_is_rejected_without_starting_another_worker(service):
    llm = BlockingLLM()
    with service(llm) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "first", "mode": "base"})
            assert ws.receive_json()["type"] == "turn_started"
            assert ws.receive_json()["type"] == "assistant_delta"
            ws.send_json({"type": "send_message", "text": "second", "mode": "base"})
            try:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert closed.value.code == 1008
            finally:
                llm.release.set()


def test_running_turn_does_not_block_other_requests_on_the_event_loop(service):
    llm = BlockingLLM()
    with service(llm) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "send_message", "text": "wait", "mode": "base"})
            assert ws.receive_json()["type"] == "turn_started"
            assert ws.receive_json()["type"] == "assistant_delta"
            started = time.monotonic()
            response = client.get("/api/health", headers=REST_HEADERS)
            elapsed = time.monotonic() - started
            llm.release.set()
            receive_until(ws, "turn_completed")

        assert response.status_code == 200
        assert elapsed < 0.5


def test_set_session_mode_emits_updated_safety_without_constructing_plan(service):
    with service(WholeTextLLM("unused")) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            snapshot = ws.receive_json()
            assert snapshot["safety"]["mode"] == "default"
            ws.send_json({"type": "set_session_mode", "mode": "readOnly"})
            updated = ws.receive_json()

            assert updated["type"] == "safety_updated"
            assert updated["safety"]["mode"] == "readOnly"


def test_set_session_mode_updates_metadata_and_survives_service_restart(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    first_app = create_app(settings, lambda: WholeTextLLM("unused"))
    with TestClient(first_app, base_url=ORIGIN) as client:
        created = client.post(
            "/api/sessions",
            headers=REST_HEADERS,
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Durable mode",
            },
        )
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        with connect(client, session_id) as ws:
            ws.receive_json()
            ws.send_json({"type": "set_session_mode", "mode": "readOnly"})
            assert ws.receive_json()["safety"]["mode"] == "readOnly"

        record = client.get(
            f"/api/sessions/{session_id}", headers=REST_HEADERS
        )
        assert record.status_code == 200
        assert record.json()["mode"] == "readOnly"

    restarted_app = create_app(settings, lambda: WholeTextLLM("unused"))
    with TestClient(restarted_app, base_url=ORIGIN) as restarted:
        record = restarted.get(
            f"/api/sessions/{session_id}", headers=REST_HEADERS
        )
        safety = restarted.get(
            f"/api/sessions/{session_id}/safety", headers=REST_HEADERS
        )

        assert record.status_code == 200
        assert record.json()["mode"] == "readOnly"
        assert safety.status_code == 200
        assert safety.json()["mode"] == "readOnly"


def test_session_mode_result_read_failure_rolls_back_metadata_and_live_policy(
    service, monkeypatch
):
    with service(WholeTextLLM("unused")) as (client, session_id, _, app):
        with connect(client, session_id) as ws:
            snapshot = ws.receive_json()
            manager = app.state.session_manager
            channel = manager._channels[session_id]
            assert client.portal is not None

            def current_state():
                return (
                    manager.get_session(session_id),
                    json.loads(json.dumps(channel.safety)),
                    channel.runtime.policy.base_mode,
                    channel.runtime.policy.mode,
                )

            before_record, before_safety, before_base_mode, before_mode = (
                client.portal.call(current_state)
            )
            original_get_session_row = manager.metadata._get_session_row

            def fail_result_select(_session_id: str):
                raise sqlite3.OperationalError("result SELECT failed")

            monkeypatch.setattr(
                manager.metadata, "_get_session_row", fail_result_select
            )
            try:
                with pytest.raises(
                    sqlite3.OperationalError, match="result SELECT failed"
                ):
                    client.portal.call(channel._set_session_mode, "readOnly")
            finally:
                monkeypatch.setattr(
                    manager.metadata,
                    "_get_session_row",
                    original_get_session_row,
                )

            after_record, after_safety, after_base_mode, after_mode = (
                client.portal.call(current_state)
            )
            assert after_record == before_record
            assert after_record.mode == "default"
            assert before_base_mode == after_base_mode == "default"
            assert before_mode == after_mode == "default"
            assert after_safety == before_safety
            assert snapshot["safety"] == before_safety


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN},
        {"Origin": "https://example.com"},
    ],
)
def test_websocket_route_enforces_launch_auth_and_origin(service, headers: dict):
    with service(WholeTextLLM("unused")) as (client, session_id, _, _):
        protocols = None if headers["Origin"] == ORIGIN else ["harness-ui", SECRET]
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                f"/ws/sessions/{session_id}",
                headers=headers,
                subprotocols=protocols,
            ):
                pass

        assert closed.value.code == 1008


def test_websocket_selects_only_the_public_subprotocol(service):
    with service(WholeTextLLM("unused")) as (client, session_id, _, _):
        with connect(client, session_id) as ws:
            assert ws.accepted_subprotocol == "harness-ui"
            assert SECRET not in ws.accepted_subprotocol
            assert ws.receive_json()["type"] == "session_snapshot"


@pytest.mark.parametrize(
    ("headers", "subprotocols"),
    [
        ({"Origin": ORIGIN}, None),
        ({"Origin": ORIGIN}, ["harness-ui", SECRET]),
    ],
)
def test_unknown_websocket_routes_are_authenticated_and_never_served_by_spa(
    service, headers: dict, subprotocols: list[str] | None
):
    with service(WholeTextLLM("unused")) as (client, _session_id, _, _):
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                "/ws/not-a-route",
                headers=headers,
                subprotocols=subprotocols,
            ):
                pass

        assert closed.value.code == 1008
