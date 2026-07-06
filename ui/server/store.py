"""Interim session store until the harness session-log seam lands
(docs/streams/ui/2026-07-06-seam-session-store.md): same surface the
harness-backed version will offer, no durability."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Session:
    id: str
    created_at: float
    updated_at: float
    # the very list handed to run_turn — the runner mutates it in place
    messages: list[dict] = field(default_factory=list)


class InMemorySessionStore:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        now = self._clock()
        session = Session(id=uuid.uuid4().hex[:12], created_at=now, updated_at=now)
        self._sessions[session.id] = session
        return session

    def list_sessions(self) -> list[Session]:
        return sorted(
            self._sessions.values(), key=lambda s: s.updated_at, reverse=True
        )

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def touch(self, session_id: str) -> None:
        self._sessions[session_id].updated_at = self._clock()
