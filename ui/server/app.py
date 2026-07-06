"""FastAPI transport: REST for sessions/meta, one WebSocket per session.

The app is assembled from injected HarnessDeps so the suite runs on
FakeLLM; __main__.py builds the real deps (CodexAdapter, sandboxed tools).
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from ui.server import events
from ui.server.runner import TurnRunner
from ui.server.store import InMemorySessionStore, Session


class EventSink:
    """Per-session fanout point. Worker threads push; the currently
    attached websocket's queue receives; detached means drop (the
    snapshot on reconnect rebuilds state, and turn_done self-heals)."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None

    def attach(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._loop, self._queue = loop, queue

    def detach(self) -> None:
        self._loop = self._queue = None

    def push(self, event: dict) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        loop.call_soon_threadsafe(queue.put_nowait, event)


@dataclass
class HarnessDeps:
    llm: object
    tools: dict
    policy_factory: Callable[[], object]  # fresh policy per session:
    # session_allowlist ("always" answers) must not leak across sessions
    system_prompt: Callable[[], str]
    subagent_system_prompt: Callable[[], str] | None
    mode: str
    workspace: str
    compact_threshold: int | None = None


def _session_meta(session: Session) -> dict:
    return {
        "id": session.id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def create_app(
    deps: HarnessDeps, store: InMemorySessionStore | None = None
) -> FastAPI:
    app = FastAPI()
    store = store or InMemorySessionStore()
    runners: dict[str, tuple[TurnRunner, EventSink]] = {}

    def get_runner(session: Session) -> tuple[TurnRunner, EventSink]:
        if session.id not in runners:
            sink = EventSink()
            runner = TurnRunner(
                llm=deps.llm,
                tools=deps.tools,
                policy=deps.policy_factory(),
                system_prompt=deps.system_prompt,
                messages=session.messages,
                emit=sink.push,
                subagent_system_prompt=deps.subagent_system_prompt,
                compact_threshold=deps.compact_threshold,
            )
            runners[session.id] = (runner, sink)
        return runners[session.id]

    app.state.store = store
    app.state.get_runner = get_runner
    app.state.deps = deps

    @app.get("/api/sessions")
    def list_sessions() -> list[dict]:
        return [_session_meta(s) for s in store.list_sessions()]

    @app.post("/api/sessions")
    def create_session() -> dict:
        return _session_meta(store.create())

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "mode": deps.mode,
            "workspace": deps.workspace,
            "system_prompt": deps.system_prompt(),
        }

    active_sockets: dict[str, WebSocket] = {}
    # A turn started on one connection must keep running after that
    # connection drops (protocol requirement), so its worker thread can
    # outlive the request that spawned it. asyncio.to_thread binds work to
    # the CURRENT event loop's own default executor; a TestClient websocket
    # session opens a fresh loop per connection, and closing that loop joins
    # its default executor's threads — deadlocking if the turn is still
    # blocked on an unanswered permission from an already-dropped socket.
    # A pool scoped to the app (not to any one connection's loop) sidesteps
    # that join entirely.
    executor = ThreadPoolExecutor()

    async def _drain(queue: asyncio.Queue, ws: WebSocket) -> None:
        while True:
            await ws.send_json(await queue.get())

    def _run_turn(runner: TurnRunner, session_id: str, text: str) -> None:
        runner.run_turn_blocking(text)
        store.touch(session_id)

    @app.websocket("/api/sessions/{session_id}/ws")
    async def session_ws(ws: WebSocket, session_id: str) -> None:
        session = store.get(session_id)
        if session is None:
            await ws.accept()
            await ws.close(code=4404)
            return
        await ws.accept()
        old = active_sockets.pop(session_id, None)
        if old is not None:
            await old.close(code=4000)  # latest connection wins
        active_sockets[session_id] = ws

        runner, sink = get_runner(session)
        queue: asyncio.Queue = asyncio.Queue()
        # attach BEFORE snapshotting, and send the snapshot through the
        # queue: the sender task is the socket's only writer, and any
        # event racing in lands after the (newer) snapshot anyway —
        # turn_done self-heals residual drift
        sink.attach(asyncio.get_running_loop(), queue)
        queue.put_nowait(
            events.session_snapshot(
                list(session.messages),
                runner.running,
                runner.pending_permission,
                runner.streamed_text,
            )
        )
        sender = asyncio.create_task(_drain(queue, ws))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # malformed: ignore, never crash the socket
                match msg.get("type"):
                    case "user_message" if isinstance(msg.get("text"), str):
                        if runner.try_begin():
                            asyncio.get_running_loop().run_in_executor(
                                executor, _run_turn, runner, session_id, msg["text"]
                            )
                        else:
                            sink.push(
                                events.turn_error("a turn is already running")
                            )
                    case "permission_answer" if msg.get("answer") in (
                        "yes", "no", "always",
                    ):
                        runner.answer_permission(msg.get("id"), msg["answer"])
                    case "cancel":
                        runner.cancel()
                    case _:
                        continue  # unknown type: ignore
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            if active_sockets.get(session_id) is ws:
                sink.detach()
                del active_sockets[session_id]

    return app
