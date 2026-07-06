import threading

import pytest
from fastapi.testclient import TestClient
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool
from starlette.websockets import WebSocketDisconnect

from ui.server.app import create_app
from ui.server.tests.fake_llm import FakeLLM, text_reply, tool_reply
from ui.server.tests.test_app import make_deps


def collect_until(ws, type_):
    """Receive events until one of the given type arrives; return it."""
    while True:
        event = ws.receive_json()
        if event["type"] == type_:
            return event


def test_snapshot_then_full_turn():
    app = create_app(make_deps([text_reply("hi there")]))
    client = TestClient(app)
    sid = client.post("/api/sessions").json()["id"]
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "session_snapshot"
        assert snap["messages"] == [] and snap["turn_running"] is False
        ws.send_json({"type": "user_message", "text": "hello"})
        assert collect_until(ws, "turn_started")
        done = collect_until(ws, "turn_done")
        assert done["messages"][-1]["content"] == "hi there"


def test_unknown_session_closes_4404():
    # Implementation note: with starlette 1.3.1, TestClient's
    # WebSocketTestSession.receive_json() surfaces a server-initiated close
    # as a raised WebSocketDisconnect (carrying the close code), not as a
    # `{"type": "close", ...}` JSON message — observed directly, adjusted
    # here per the task brief's guidance.
    client = TestClient(create_app(make_deps()))
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/sessions/nope/ws") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4404


def test_permission_round_trip_over_ws():
    side_effect = Tool(
        name="touchy",
        description="side effect",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "done it",
        read_only=False,
    )
    deps = make_deps(
        [tool_reply(("touchy", {})), text_reply("all done")],
        tools={"touchy": side_effect},
    )
    deps.policy_factory = lambda: PermissionPolicy("default")
    client = TestClient(create_app(deps))
    sid = client.post("/api/sessions").json()["id"]
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        ws.receive_json()  # snapshot
        ws.send_json({"type": "user_message", "text": "do it"})
        request = collect_until(ws, "permission_request")
        ws.send_json(
            {"type": "permission_answer", "id": request["id"], "answer": "yes"}
        )
        result = collect_until(ws, "tool_result")
        assert result["result"] == "done it"
        collect_until(ws, "turn_done")


def test_busy_rejection_while_turn_runs():
    gate = threading.Event()

    class BlockingLLM:
        def complete(self, messages, tools=None, system=None):
            gate.wait(timeout=10)
            return text_reply("finally")

    deps = make_deps()
    deps.llm = BlockingLLM()
    client = TestClient(create_app(deps))
    sid = client.post("/api/sessions").json()["id"]
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        ws.receive_json()  # snapshot
        ws.send_json({"type": "user_message", "text": "first"})
        collect_until(ws, "turn_started")
        ws.send_json({"type": "user_message", "text": "second"})
        error = collect_until(ws, "turn_error")
        assert "already running" in error["message"]
        gate.set()
        collect_until(ws, "turn_done")


def test_malformed_messages_are_ignored():
    client = TestClient(create_app(make_deps()))
    sid = client.post("/api/sessions").json()["id"]
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        ws.receive_json()  # snapshot
        ws.send_text("not json at all")
        ws.send_json({"type": "unknown_thing"})
        ws.send_json({"type": "user_message", "text": "still works"})
        collect_until(ws, "turn_done")


def test_reconnect_snapshot_carries_pending_permission():
    side_effect = Tool(
        name="touchy",
        description="side effect",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "done it",
        read_only=False,
    )
    deps = make_deps(
        [tool_reply(("touchy", {})), text_reply("all done")],
        tools={"touchy": side_effect},
    )
    deps.policy_factory = lambda: PermissionPolicy("default")
    client = TestClient(create_app(deps))
    sid = client.post("/api/sessions").json()["id"]
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_message", "text": "do it"})
        request = collect_until(ws, "permission_request")
    # socket dropped mid-permission; the turn is still parked on the future
    with client.websocket_connect(f"/api/sessions/{sid}/ws") as ws:
        snap = ws.receive_json()
        assert snap["turn_running"] is True
        assert snap["pending_permission"]["id"] == request["id"]
        ws.send_json(
            {"type": "permission_answer", "id": request["id"], "answer": "yes"}
        )
        collect_until(ws, "turn_done")
