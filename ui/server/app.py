"""FastAPI transport: REST for sessions/meta, one WebSocket per session.

The app is assembled from injected HarnessDeps so the suite runs on
FakeLLM; __main__.py builds the real deps (CodexAdapter, sandboxed tools).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI

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

    return app
