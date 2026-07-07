import json

from ui.server import events


def test_every_event_has_a_type_and_serializes():
    all_events = [
        events.session_snapshot([{"role": "user", "content": "hi"}], True, None, ""),
        events.turn_started(),
        events.text_delta("chunk"),
        events.tool_call("bash", {"command": "ls"}),
        events.tool_result("bash", "file.txt"),
        events.permission_request("perm-1", "bash", {"command": "rm x"}),
        events.compaction(12),
        events.turn_done([{"role": "assistant", "content": "done"}]),
        events.turn_cancelled([]),
        events.turn_error("RuntimeError: boom", []),
    ]
    types = [e["type"] for e in all_events]
    assert types == [
        "session_snapshot", "turn_started", "text_delta", "tool_call",
        "tool_result", "permission_request", "compaction", "turn_done",
        "turn_cancelled", "turn_error",
    ]
    for event in all_events:
        json.dumps(event)  # the wire format must always serialize


def test_payload_keys():
    snap = events.session_snapshot([], False, {"id": "perm-1"}, "so far")
    assert (snap["messages"], snap["turn_running"]) == ([], False)
    assert (snap["pending_permission"], snap["streamed_text"]) == ({"id": "perm-1"}, "so far")
    assert events.text_delta("x")["text"] == "x"
    call = events.tool_call("echo", {"x": 1})
    assert (call["name"], call["args"]) == ("echo", {"x": 1})
    result = events.tool_result("echo", "1")
    assert (result["name"], result["result"]) == ("echo", "1")
    perm = events.permission_request("perm-2", "bash", {"command": "ls"})
    assert (perm["id"], perm["name"], perm["args"]) == ("perm-2", "bash", {"command": "ls"})
    assert events.compaction(3)["summarized"] == 3
    assert events.turn_done([{"role": "user", "content": "q"}])["messages"] == [{"role": "user", "content": "q"}]
    history = [{"role": "assistant", "content": "kept"}]
    cancelled = events.turn_cancelled(history)
    assert cancelled["messages"] == history
    error = events.turn_error("boom", history)
    assert (error["message"], error["messages"]) == ("boom", history)
