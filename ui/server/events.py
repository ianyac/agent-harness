"""The wire vocabulary between server and browser — one factory per event.

Everything the harness does becomes one of these dicts; the frontend's
reducer is a mirror of this module. turn_done carries the FULL messages
list (not a slice): compaction can rewrite history mid-turn, so the only
self-consistent payload is the whole authoritative state.
"""


def session_snapshot(
    messages: list[dict],
    turn_running: bool,
    pending_permission: dict | None,
    streamed_text: str,
) -> dict:
    return {
        "type": "session_snapshot",
        "messages": messages,
        "turn_running": turn_running,
        "pending_permission": pending_permission,
        "streamed_text": streamed_text,
    }


def turn_started() -> dict:
    return {"type": "turn_started"}


def text_delta(text: str) -> dict:
    return {"type": "text_delta", "text": text}


def tool_call(name: str, args: dict) -> dict:
    return {"type": "tool_call", "name": name, "args": args}


def tool_result(name: str, result: str) -> dict:
    return {"type": "tool_result", "name": name, "result": result}


def permission_request(request_id: str, name: str, args: dict) -> dict:
    return {"type": "permission_request", "id": request_id, "name": name, "args": args}


def compaction(summarized: int) -> dict:
    return {"type": "compaction", "summarized": summarized}


def turn_done(messages: list[dict]) -> dict:
    return {"type": "turn_done", "messages": messages}


def turn_cancelled(messages: list[dict]) -> dict:
    # carries the post-rollback authoritative list so a client that
    # reconnected mid-turn (snapshot included the now-rolled-back turn)
    # self-heals instead of keeping phantom items
    return {"type": "turn_cancelled", "messages": messages}


def turn_error(message: str, messages: list[dict]) -> dict:
    return {"type": "turn_error", "message": message, "messages": messages}
