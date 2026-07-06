# Agent Harness Web UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A browser chat client for the agent harness whose transcript pane doubles as a transcript inspector, per the approved spec `docs/streams/ui/2026-07-06-web-ui-design.md`.

**Architecture:** A FastAPI backend imports `harness` directly and plays the role `main.py` plays for the CLI: it builds the tool registry, runs each blocking `run_turn` on a worker thread (TurnRunner), and bridges everything to one WebSocket per session as typed events. A React/Vite frontend reduces that event stream into transcript state; reconstruction from a snapshot and live rendering share the same item constructors.

**Tech Stack:** Python 3.14 + uv + FastAPI + uvicorn (backend); Vite + React 19 + TypeScript + Vitest + React Testing Library (frontend); pytest for the backend suite.

## Global Constraints

- Baseline: main @ `148edf2` (lesson 12). Branch: `ui/scaffold`.
- Write only inside `ui/` and `docs/streams/ui/` — plus ONE flagged hunk in root `pyproject.toml`/`uv.lock` (Task 1); never touch `harness/`, `tests/`, `main.py`, `conftest.py`, `.github/`.
- Consume the harness through public seams only: `run_turn` (+ `on_tool_call`, `asker`, `on_compact`, `compact_threshold`, `keep_recent`), `LLMClient` protocol, `Tool`/registry + tool factories (`read_file_tool`, `write_file_tool`, `list_dir_tool`, `bash_tool`, `agent_tool`), `PermissionPolicy`/`MODES`, `build_system_prompt`/`Environment`, `default_sandbox`/`SandboxPolicy`.
- Root `uv run pytest` must stay green WITHOUT the `ui` dependency group installed, and must NOT collect ui tests (governance: no cross-lane CI coupling). The ui backend suite runs only via `UI_TESTS=1 uv run --group ui pytest ui/server/tests`.
- Degraded mode until the streaming seam lands: TurnRunner detects `on_text_delta` in the `run_turn` signature at runtime; no harness edits, ever.
- Event vocabulary exactly as spec'd: `session_snapshot`, `turn_started`, `text_delta`, `tool_call`, `tool_result`, `permission_request`, `compaction`, `turn_done`, `turn_cancelled`, `turn_error` down; `user_message`, `permission_answer`, `cancel` up. `turn_done` carries the FULL authoritative messages list (compaction can shift indices mid-turn, so a "this turn's slice" is ill-defined; full list is self-healing).
- Rollback on cancel/error mirrors lesson-12 `main.py`: pop messages from the tail until the last message is a plain assistant message (no `tool_calls`) — never `del messages[turn_start:]` (compaction breaks saved indices).
- Frontend: no component library, no state library; plain React + one CSS file. All suites offline.
- Commit per task, imperative one-liners prefixed `ui: `.

## File Structure

```
pyproject.toml                    # MODIFY (Task 1 only): add [dependency-groups] ui
ui/
  __init__.py                     # package markers (Task 1)
  conftest.py                     # keeps ui tests out of the root suite (Task 1)
  server/
    __init__.py
    events.py                     # event dict factories — the wire vocabulary (Task 2)
    store.py                      # Session + InMemorySessionStore (Task 3)
    runner.py                     # TurnCancelled, TurnRunner (Tasks 5-8)
    app.py                        # EventSink, HarnessDeps, create_app, routes, WS (Tasks 9-10)
    __main__.py                   # real wiring: CodexAdapter, tools, uvicorn (Task 11)
    tests/
      __init__.py
      fake_llm.py                 # scripted LLMClient double + reply helpers (Task 4)
      test_events.py              # Task 2
      test_store.py               # Task 3
      test_fake_llm.py            # Task 4
      test_runner.py              # Tasks 5-8
      test_app.py                 # Task 9
      test_ws.py                  # Task 10
  frontend/
    package.json  tsconfig.json  vite.config.ts  index.html  .gitignore   # Task 12
    src/
      main.tsx  App.tsx  styles.css  test-setup.ts                        # Tasks 12, 16
      types/events.ts             # TS mirror of events.py (Task 13)
      state/reducer.ts  state/reducer.test.ts                             # Task 13
      ws/client.ts  ws/client.test.ts                                     # Task 14
      components/Transcript.tsx  ToolCard.tsx  PermissionPrompt.tsx
                 Composer.tsx  PermissionPrompt.test.tsx                  # Task 15
      components/SessionSidebar.tsx  InspectorPane.tsx  Header.tsx        # Task 16
```

---

### Task 1: Backend scaffolding and dependency plumbing

**Files:**
- Modify: `pyproject.toml` (root — THE flagged shared-file hunk)
- Create: `ui/__init__.py`, `ui/server/__init__.py`, `ui/server/tests/__init__.py`, `ui/conftest.py`

**Interfaces:**
- Produces: importable packages `ui.server` and `ui.server.tests`; command `UI_TESTS=1 uv run --group ui pytest ui/server/tests` as the ui suite entry point.

- [ ] **Step 1: Add the `ui` dependency group to root pyproject.toml**

In `pyproject.toml`, extend the existing `[dependency-groups]` table (do not touch anything else):

```toml
[dependency-groups]
dev = [
    "pytest>=9.1.1",
]
ui = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.35",
]
```

This is the one shared-file hunk the spec authorizes; the eventual PR description must flag it for yc's routing call. If a permission gate blocks the write, stop and surface to yc.

- [ ] **Step 2: Create package markers and the collection guard**

`ui/__init__.py`, `ui/server/__init__.py`, `ui/server/tests/__init__.py` — all empty files.

`ui/conftest.py`:

```python
# The root `uv run pytest` run is the harness lane's gate; ui tests joining
# it would couple the lanes' CI (a harness seam change would turn a harness
# PR red on ui code it cannot touch). Governance decision, not a default:
# the ui suite runs only when asked for explicitly:
#   UI_TESTS=1 uv run --group ui pytest ui/server/tests
import os

if not os.environ.get("UI_TESTS"):
    collect_ignore = ["server"]
```

- [ ] **Step 3: Sync and verify both invocations behave**

Run: `uv sync --group ui`
Expected: resolves and installs fastapi + uvicorn; `uv.lock` updated.

Run: `uv run pytest`
Expected: the existing harness suite passes; NO tests collected from `ui/` (check the collection summary).

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests`
Expected: `no tests ran` (exit code 5 is fine at this point) — proves the path is collectable when asked.

Run: `uv run --group ui python -c "import fastapi, uvicorn; import ui.server"`
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock ui/
git commit -m "ui: scaffold server package; ui dependency group (shared pyproject hunk, flagged)"
```

---

### Task 2: Event vocabulary (`events.py`)

**Files:**
- Create: `ui/server/events.py`
- Test: `ui/server/tests/test_events.py`

**Interfaces:**
- Produces (exact signatures, all returning `dict` with a `"type"` key):
  - `session_snapshot(messages: list[dict], turn_running: bool, pending_permission: dict | None, streamed_text: str)`
  - `turn_started()` · `text_delta(text: str)` · `tool_call(name: str, args: dict)` · `tool_result(name: str, result: str)` · `permission_request(request_id: str, name: str, args: dict)` · `compaction(summarized: int)` · `turn_done(messages: list[dict])` · `turn_cancelled()` · `turn_error(message: str)`

- [ ] **Step 1: Write the failing test**

`ui/server/tests/test_events.py`:

```python
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
        events.turn_cancelled(),
        events.turn_error("RuntimeError: boom"),
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
    assert events.turn_error("boom")["message"] == "boom"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_events.py -v`
Expected: FAIL — `AttributeError: module 'ui.server.events' has no attribute ...` (create an empty `events.py` first if the import itself fails).

- [ ] **Step 3: Implement**

`ui/server/events.py`:

```python
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


def turn_cancelled() -> dict:
    return {"type": "turn_cancelled"}


def turn_error(message: str) -> dict:
    return {"type": "turn_error", "message": message}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_events.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/server/events.py ui/server/tests/test_events.py
git commit -m "ui: event vocabulary factories"
```

---

### Task 3: In-memory session store (`store.py`)

**Files:**
- Create: `ui/server/store.py`
- Test: `ui/server/tests/test_store.py`

**Interfaces:**
- Produces:
  - `@dataclass Session` — fields `id: str`, `created_at: float`, `updated_at: float`, `messages: list[dict]` (mutable, shared with the runner).
  - `InMemorySessionStore(clock: Callable[[], float] = time.time)` with `create() -> Session`, `list_sessions() -> list[Session]` (most recently updated first), `get(session_id: str) -> Session | None`, `touch(session_id: str) -> None`.

- [ ] **Step 1: Write the failing test**

`ui/server/tests/test_store.py`:

```python
from ui.server.store import InMemorySessionStore


def make_clock(start=1000.0):
    state = {"now": start}

    def clock():
        state["now"] += 1.0
        return state["now"]

    return clock


def test_create_get_and_shared_messages_list():
    store = InMemorySessionStore(clock=make_clock())
    session = store.create()
    assert store.get(session.id) is session
    assert session.messages == []
    session.messages.append({"role": "user", "content": "hi"})
    assert store.get(session.id).messages == [{"role": "user", "content": "hi"}]
    assert store.get("nope") is None


def test_list_orders_by_recent_update():
    store = InMemorySessionStore(clock=make_clock())
    first = store.create()
    second = store.create()
    assert [s.id for s in store.list_sessions()] == [second.id, first.id]
    store.touch(first.id)
    assert [s.id for s in store.list_sessions()] == [first.id, second.id]
    assert store.get(first.id).updated_at > store.get(second.id).updated_at
```

- [ ] **Step 2: Run it to verify it fails**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.server.store'`.

- [ ] **Step 3: Implement**

`ui/server/store.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_store.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/server/store.py ui/server/tests/test_store.py
git commit -m "ui: in-memory session store behind the future seam surface"
```

---

### Task 4: FakeLLM test double

**Files:**
- Create: `ui/server/tests/fake_llm.py`
- Test: `ui/server/tests/test_fake_llm.py`

**Interfaces:**
- Produces (every later backend test consumes these):
  - `FakeLLM(replies: list[dict])` — implements the harness `LLMClient` protocol; `complete(messages, tools=None, system=None)` pops and returns the next scripted reply, recording each request in `self.requests` as `{"messages": <deep-ish copy>, "tools": tools, "system": system}`.
  - `text_reply(text: str) -> dict` — `{"role": "assistant", "content": text}`.
  - `tool_reply(*calls: tuple[str, dict]) -> dict` — assistant message with `content: None` and one `tool_calls` entry per `(name, args)`; call ids are `"call-1"`, `"call-2"`, ... in order. `arguments` is a JSON STRING (the OpenAI wart the harness expects).

- [ ] **Step 1: Write the failing test**

`ui/server/tests/test_fake_llm.py`:

```python
import json

from ui.server.tests.fake_llm import FakeLLM, text_reply, tool_reply


def test_replies_in_order_and_requests_recorded():
    llm = FakeLLM([text_reply("one"), text_reply("two")])
    first = llm.complete([{"role": "user", "content": "q"}], system="sys")
    assert first == {"role": "assistant", "content": "one"}
    assert llm.complete([]) == {"role": "assistant", "content": "two"}
    assert llm.requests[0]["system"] == "sys"
    assert llm.requests[0]["messages"] == [{"role": "user", "content": "q"}]


def test_tool_reply_shape_matches_harness_expectations():
    reply = tool_reply(("echo", {"x": 1}), ("bash", {"command": "ls"}))
    assert reply["role"] == "assistant" and reply["content"] is None
    calls = reply["tool_calls"]
    assert [c["id"] for c in calls] == ["call-1", "call-2"]
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "echo"
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_fake_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.server.tests.fake_llm'`.

- [ ] **Step 3: Implement**

`ui/server/tests/fake_llm.py`:

```python
"""Scripted LLMClient double — the ui suite's model, like the harness
suite's fake but owned here (tests/ is the harness lane)."""

import json


class FakeLLM:
    def __init__(self, replies: list[dict]):
        self._replies = list(replies)
        self.requests: list[dict] = []

    def complete(self, messages, tools=None, system=None):
        self.requests.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "system": system}
        )
        if not self._replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        return self._replies.pop(0)


def text_reply(text: str) -> dict:
    return {"role": "assistant", "content": text}


def tool_reply(*calls: tuple[str, dict]) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls, start=1)
        ],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_fake_llm.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/server/tests/fake_llm.py ui/server/tests/test_fake_llm.py
git commit -m "ui: scripted FakeLLM double for the backend suite"
```

---

### Task 5: TurnRunner core — happy path, tool events, rollback

**Files:**
- Create: `ui/server/runner.py`
- Test: `ui/server/tests/test_runner.py`

**Interfaces:**
- Consumes: `ui.server.events` factories (Task 2); `FakeLLM`/`text_reply`/`tool_reply` (Task 4); harness `run_turn`, `Tool`, `PermissionPolicy`.
- Produces:
  - `class TurnCancelled(Exception)`
  - `TurnRunner(llm, tools: dict[str, Tool], policy, system_prompt: Callable[[], str], messages: list[dict], emit: Callable[[dict], None], run_turn_fn=run_turn, subagent_system_prompt: Callable[[], str] | None = None, compact_threshold: int | None = None, keep_recent: int = 8)`
  - Methods: `try_begin() -> bool` (atomically claims the turn slot; MUST be called before `run_turn_blocking`), `run_turn_blocking(text: str) -> None` (worker thread), `answer_permission(request_id: str, answer: str) -> None`, `cancel() -> None` — the latter two are thread-safe.
  - Attributes read by the app layer: `running: bool`, `pending_permission: dict | None`, `streamed_text: str`, `messages: list[dict]`.
  - `emit` MUST be thread-safe; it is called from the worker thread.

- [ ] **Step 1: Write the failing tests (happy path, tools, error rollback, try_begin)**

`ui/server/tests/test_runner.py`:

```python
import queue
import threading

from harness.permissions import PermissionPolicy
from harness.tools.base import Tool

from ui.server.runner import TurnCancelled, TurnRunner
from ui.server.tests.fake_llm import FakeLLM, text_reply, tool_reply


def echo_tool(read_only=True):
    return Tool(
        name="echo",
        description="echo x back",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        execute=lambda x: f"echo:{x}",
        read_only=read_only,
    )


def make_runner(llm, messages=None, tools=None, policy=None, **kwargs):
    events_q = queue.Queue()
    messages = messages if messages is not None else []
    runner = TurnRunner(
        llm=llm,
        tools=tools or {},
        policy=policy,
        system_prompt=lambda: "test system",
        messages=messages,
        emit=events_q.put,
        **kwargs,
    )
    return runner, events_q, messages


def run_to_completion(runner, text):
    assert runner.try_begin()
    runner.run_turn_blocking(text)


def drain(events_q):
    out = []
    while True:
        try:
            out.append(events_q.get_nowait())
        except queue.Empty:
            return out


def wait_for(events_q, type_, timeout=5):
    """Pop events until one of the given type arrives (or time out)."""
    while True:
        event = events_q.get(timeout=timeout)
        if event["type"] == type_:
            return event


def test_text_only_turn():
    runner, events_q, messages = make_runner(FakeLLM([text_reply("hi there")]))
    run_to_completion(runner, "hello")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == ["turn_started", "turn_done"]
    assert evts[-1]["messages"] is not messages  # a copy, not the live list
    assert evts[-1]["messages"] == messages
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[-1]["content"] == "hi there"
    assert runner.running is False


def test_tool_turn_emits_call_and_result():
    llm = FakeLLM([tool_reply(("echo", {"x": 7})), text_reply("done")])
    runner, events_q, messages = make_runner(llm, tools={"echo": echo_tool()})
    run_to_completion(runner, "go")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == [
        "turn_started", "tool_call", "tool_result", "turn_done",
    ]
    assert evts[1] == {"type": "tool_call", "name": "echo", "args": {"x": 7}}
    assert evts[2] == {"type": "tool_result", "name": "echo", "result": "echo:7"}
    assert messages[-1]["content"] == "done"
    assert any(m.get("role") == "tool" and m["content"] == "echo:7" for m in messages)


def test_llm_failure_rolls_back_and_emits_turn_error():
    class ExplodingLLM:
        def complete(self, messages, tools=None, system=None):
            raise RuntimeError("boom")

    prior = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
    ]
    runner, events_q, messages = make_runner(ExplodingLLM(), messages=list(prior))
    run_to_completion(runner, "new question")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == ["turn_started", "turn_error"]
    assert "RuntimeError: boom" in evts[-1]["message"]
    assert messages == prior  # the broken exchange is gone, history intact
    assert runner.running is False


def test_error_on_first_turn_rolls_back_to_empty():
    class ExplodingLLM:
        def complete(self, messages, tools=None, system=None):
            raise RuntimeError("boom")

    runner, events_q, messages = make_runner(ExplodingLLM())
    run_to_completion(runner, "hello")
    assert messages == []


def test_try_begin_rejects_second_claim():
    runner, _, _ = make_runner(FakeLLM([text_reply("hi")]))
    assert runner.try_begin() is True
    assert runner.try_begin() is False
    runner.run_turn_blocking("hello")
    assert runner.try_begin() is True  # slot free again after the turn
```

- [ ] **Step 2: Run to verify failure**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.server.runner'`.

- [ ] **Step 3: Implement the runner core**

`ui/server/runner.py`:

```python
"""TurnRunner: one session's bridge from the blocking run_turn to an
event sink.

The caller owns the worker thread: call try_begin() (atomically claims
the turn slot), then run_turn_blocking() on a thread. answer_permission()
and cancel() are called from other threads. emit must be thread-safe."""

import inspect
import itertools
import threading
from concurrent.futures import Future
from dataclasses import replace
from typing import Callable

from harness.loop import run_turn
from harness.tools.base import Tool

from ui.server import events


class TurnCancelled(Exception):
    """Raised inside harness callbacks to abort the running turn."""


class TurnRunner:
    def __init__(
        self,
        llm,
        tools: dict[str, Tool],
        policy,
        system_prompt: Callable[[], str],
        messages: list[dict],
        emit: Callable[[dict], None],
        run_turn_fn=run_turn,
        subagent_system_prompt: Callable[[], str] | None = None,
        compact_threshold: int | None = None,
        keep_recent: int = 8,
    ):
        self._llm = llm
        self._policy = policy
        self._system_prompt = system_prompt  # re-evaluated per turn, like main.py
        self._emit = emit
        self._run_turn = run_turn_fn
        self._compact_threshold = compact_threshold
        self._keep_recent = keep_recent
        # degraded mode until the streaming seam lands
        # (docs/streams/ui/2026-07-06-seam-token-streaming.md): probe the
        # signature instead of editing harness/
        self._streaming = (
            "on_text_delta" in inspect.signature(run_turn_fn).parameters
        )
        self.messages = messages
        self._tools = {name: self._wrap(tool) for name, tool in tools.items()}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.running = False
        self.pending_permission: dict | None = None
        self.streamed_text = ""
        self._permission_future: Future | None = None
        self._cancelled = False

    # -- called from the event-loop thread --------------------------------
    def try_begin(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self._cancelled = False
            return True

    # -- called from any thread --------------------------------------------
    def answer_permission(self, request_id: str, answer: str) -> None:
        with self._lock:
            pending, future = self.pending_permission, self._permission_future
            if pending is None or pending["id"] != request_id or future is None:
                return  # stale or unknown: ignore, never crash the socket
            self._permission_future = None
        future.set_result(answer)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            future, self._permission_future = self._permission_future, None
        if future is not None:
            future.set_result("no")  # wake the asker; it raises TurnCancelled

    # -- worker thread -------------------------------------------------------
    def run_turn_blocking(self, text: str) -> None:
        self.streamed_text = ""
        self._emit(events.turn_started())
        extra = {"on_text_delta": self._on_text_delta} if self._streaming else {}
        try:
            self._run_turn(
                self.messages,
                text,
                self._llm,
                tools=self._tools,
                on_tool_call=self._on_tool_call,
                policy=self._policy,
                asker=self._asker,
                system=self._system_prompt(),
                compact_threshold=self._compact_threshold,
                keep_recent=self._keep_recent,
                on_compact=self._on_compact,
                **extra,
            )
        except TurnCancelled:
            self._rollback()
            self._emit(events.turn_cancelled())
        except Exception as error:  # noqa: BLE001 — surfaced to the browser
            self._rollback()
            self._emit(events.turn_error(f"{type(error).__name__}: {error}"))
        else:
            self._emit(events.turn_done(list(self.messages)))
        finally:
            self.streamed_text = ""
            with self._lock:
                self.running = False
                self.pending_permission = None
                self._permission_future = None

    def _rollback(self) -> None:
        # mirror lesson-12 main.py: compaction may have shifted indices
        # mid-turn, so pop back to the last completed exchange instead of
        # slicing at a saved position
        while self.messages and not (
            self.messages[-1]["role"] == "assistant"
            and not self.messages[-1].get("tool_calls")
        ):
            self.messages.pop()

    # -- harness callbacks (worker thread) -----------------------------------
    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise TurnCancelled()

    def _on_text_delta(self, text: str) -> None:
        self._check_cancelled()
        self.streamed_text += text
        self._emit(events.text_delta(text))

    def _on_tool_call(self, name: str, args: dict) -> None:
        self._check_cancelled()
        self.streamed_text = ""  # a non-delta event closes the open bubble
        self._emit(events.tool_call(name, args))

    def _on_compact(self, summarized: int) -> None:
        self._emit(events.compaction(summarized))

    def _asker(self, name: str, args: dict) -> str:
        self._check_cancelled()
        request = {"id": f"perm-{next(self._ids)}", "name": name, "args": args}
        future = Future()
        with self._lock:
            self.pending_permission = request
            self._permission_future = future
        self.streamed_text = ""
        self._emit(events.permission_request(request["id"], name, args))
        answer = future.result()
        with self._lock:
            self.pending_permission = None
        self._check_cancelled()
        return answer

    def _wrap(self, tool: Tool) -> Tool:
        inner = tool.execute

        def execute(**kwargs):
            result = inner(**kwargs)
            self._emit(events.tool_result(tool.name, result))
            self._check_cancelled()
            return result

        return replace(tool, execute=execute)
```

(The `subagent_system_prompt` parameter is accepted but unused until Task 8 — add it to the signature now so the interface is stable, and store it: `self._subagent_system_prompt = subagent_system_prompt`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_runner.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/server/runner.py ui/server/tests/test_runner.py
git commit -m "ui: TurnRunner core — thread bridge, tool events, pop-back rollback"
```

---

### Task 6: TurnRunner permission bridge

**Files:**
- Modify: `ui/server/runner.py` (only if tests reveal gaps — the bridge shipped in Task 5)
- Test: `ui/server/tests/test_runner.py` (append)

**Interfaces:**
- Consumes: `TurnRunner.answer_permission(request_id, answer)`, `pending_permission`; harness `PermissionPolicy("default")` (non-read-only tools → `"ask"`).

- [ ] **Step 1: Write the failing tests**

Append to `ui/server/tests/test_runner.py`:

```python
def run_on_thread(runner, text):
    assert runner.try_begin()
    thread = threading.Thread(target=runner.run_turn_blocking, args=(text,))
    thread.start()
    return thread


def test_permission_yes_runs_the_tool():
    llm = FakeLLM([tool_reply(("echo", {"x": 1})), text_reply("ok")])
    runner, events_q, _ = make_runner(
        llm,
        tools={"echo": echo_tool(read_only=False)},
        policy=PermissionPolicy("default"),
    )
    thread = run_on_thread(runner, "go")
    request = wait_for(events_q, "permission_request")
    assert request["name"] == "echo" and runner.pending_permission["id"] == request["id"]
    runner.answer_permission(request["id"], "yes")
    thread.join(timeout=5)
    assert not thread.is_alive()
    types = [e["type"] for e in drain(events_q)]
    assert "tool_call" in types and "tool_result" in types and "turn_done" in types
    assert runner.pending_permission is None


def test_permission_no_denies_without_running():
    executed = []
    tool = Tool(
        name="touchy",
        description="side effect",
        parameters={"type": "object", "properties": {}},
        execute=lambda: executed.append(True) or "did it",
        read_only=False,
    )
    llm = FakeLLM([tool_reply(("touchy", {})), text_reply("understood")])
    runner, events_q, messages = make_runner(
        llm, tools={"touchy": tool}, policy=PermissionPolicy("default")
    )
    thread = run_on_thread(runner, "go")
    request = wait_for(events_q, "permission_request")
    runner.answer_permission(request["id"], "no")
    thread.join(timeout=5)
    assert executed == []
    assert any(
        m.get("role") == "tool" and "Permission denied" in m["content"]
        for m in messages
    )
    assert [e["type"] for e in drain(events_q)][-1] == "turn_done"


def test_permission_always_skips_second_prompt():
    llm = FakeLLM([
        tool_reply(("echo", {"x": 1})),
        tool_reply(("echo", {"x": 2})),
        text_reply("done"),
    ])
    runner, events_q, _ = make_runner(
        llm,
        tools={"echo": echo_tool(read_only=False)},
        policy=PermissionPolicy("default"),
    )
    thread = run_on_thread(runner, "go")
    request = wait_for(events_q, "permission_request")
    runner.answer_permission(request["id"], "always")
    thread.join(timeout=5)
    remaining = [e["type"] for e in drain(events_q)]
    assert remaining.count("permission_request") == 0  # allowlisted after "always"
    assert remaining.count("tool_result") == 2


def test_stale_permission_answer_is_ignored():
    runner, _, _ = make_runner(FakeLLM([text_reply("hi")]))
    runner.answer_permission("perm-999", "yes")  # nothing pending: no crash
```

- [ ] **Step 2: Run tests**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_runner.py -v`
Expected: all pass if Task 5's bridge is correct; fix `runner.py` if any fail (the code in Task 5 Step 3 is the reference).

- [ ] **Step 3: Commit**

```bash
git add ui/server/tests/test_runner.py ui/server/runner.py
git commit -m "ui: permission bridge tests — yes/no/always and stale answers"
```

---

### Task 7: TurnRunner cancellation and the streaming switch

**Files:**
- Modify: `ui/server/runner.py` (only for gaps)
- Test: `ui/server/tests/test_runner.py` (append)

**Interfaces:**
- Consumes: `TurnRunner.cancel()`, `run_turn_fn` injection, `TurnCancelled`, `streamed_text`.

- [ ] **Step 1: Write the failing tests**

Append to `ui/server/tests/test_runner.py`:

```python
def test_cancel_during_permission_wait_rolls_back():
    llm = FakeLLM([tool_reply(("echo", {"x": 1})), text_reply("never sent")])
    prior = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
    ]
    runner, events_q, messages = make_runner(
        llm,
        messages=list(prior),
        tools={"echo": echo_tool(read_only=False)},
        policy=PermissionPolicy("default"),
    )
    thread = run_on_thread(runner, "go")
    wait_for(events_q, "permission_request")
    runner.cancel()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [e["type"] for e in drain(events_q)][-1] == "turn_cancelled"
    assert messages == prior


def test_cancel_flag_raises_at_next_callback():
    runner, _, _ = make_runner(FakeLLM([text_reply("hi")]))
    assert runner.try_begin()
    runner.cancel()
    try:
        runner._on_tool_call("echo", {})
        raise AssertionError("expected TurnCancelled")
    except TurnCancelled:
        pass


def fake_streaming_run_turn(
    messages, user_input, llm, tools=None, max_iterations=20,
    on_tool_call=None, policy=None, asker=None, system=None,
    compact_threshold=None, keep_recent=8, on_compact=None,
    breadcrumbs=None, on_text_delta=None,
):
    """Stand-in for run_turn once the streaming seam lands."""
    messages.append({"role": "user", "content": user_input})
    for chunk in ("he", "llo"):
        if on_text_delta is not None:
            on_text_delta(chunk)
    reply = {"role": "assistant", "content": "hello"}
    messages.append(reply)
    return reply


def test_streaming_run_turn_forwards_deltas():
    runner, events_q, _ = make_runner(
        FakeLLM([]), run_turn_fn=fake_streaming_run_turn
    )
    run_to_completion(runner, "hi")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == [
        "turn_started", "text_delta", "text_delta", "turn_done",
    ]
    assert "".join(e["text"] for e in evts if e["type"] == "text_delta") == "hello"
    assert runner.streamed_text == ""  # cleared once the turn ends


def test_current_harness_run_turn_stays_degraded():
    runner, events_q, _ = make_runner(FakeLLM([text_reply("whole message")]))
    run_to_completion(runner, "hi")
    assert all(e["type"] != "text_delta" for e in drain(events_q))
```

- [ ] **Step 2: Run tests**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_runner.py -v`
Expected: all pass against Task 5's implementation; fix gaps if not.

- [ ] **Step 3: Commit**

```bash
git add ui/server/tests/test_runner.py ui/server/runner.py
git commit -m "ui: cancellation and streaming-switch tests for TurnRunner"
```

---

### Task 8: Subagent tool wired per session

**Files:**
- Modify: `ui/server/runner.py`
- Test: `ui/server/tests/test_runner.py` (append)

**Interfaces:**
- Consumes: harness `agent_tool(llm, tools, *, policy, asker, system, on_tool_call, max_iterations=20, compact_threshold=None, keep_recent=8)` — its arg schema takes one string param `task`; it filters ITSELF out of the registry by object identity at call time.
- Produces: when `subagent_system_prompt` is given, the runner's registry gains an `"agent"` tool whose sub-activity flows through the SAME emit (flat interleave: sub tool calls/results appear as ordinary `tool_call`/`tool_result` events, sub permission prompts route to the browser).

**The identity subtlety (why the copy matters):** `agent_tool` removes itself from the registry it holds by `t is not tool` — comparing against the UNwrapped tool object. If we insert the WRAPPED agent into the same dict we handed it, the filter misses and a subagent could recurse. So: snapshot the registry BEFORE inserting the agent tool, and hand `agent_tool` that snapshot.

- [ ] **Step 1: Write the failing test**

Append to `ui/server/tests/test_runner.py`:

```python
def test_subagent_activity_interleaves_flat():
    # outer turn delegates; inner turn calls echo once, then answers;
    # FakeLLM serves outer and inner run_turns from one script, in order
    llm = FakeLLM([
        tool_reply(("agent", {"task": "count things"})),   # outer iteration 1
        tool_reply(("echo", {"x": 2})),                    # inner iteration 1
        text_reply("sub answer: 2"),                       # inner iteration 2
        text_reply("the sub said 2"),                      # outer iteration 2
    ])
    runner, events_q, messages = make_runner(
        llm,
        tools={"echo": echo_tool()},
        subagent_system_prompt=lambda: "sub system",
    )
    run_to_completion(runner, "delegate please")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == [
        "turn_started",
        "tool_call",      # agent
        "tool_call",      # echo, inside the sub — flat interleave
        "tool_result",    # echo
        "tool_result",    # agent (its final answer as result text)
        "turn_done",
    ]
    assert evts[1]["name"] == "agent"
    assert evts[4] == {"type": "tool_result", "name": "agent", "result": "sub answer: 2"}
    # the inner run_turn saw the sub system prompt and a registry sans agent
    inner_request = llm.requests[1]
    assert inner_request["system"] == "sub system"
    assert all(t["function"]["name"] != "agent" for t in inner_request["tools"])
    assert messages[-1]["content"] == "the sub said 2"
```

- [ ] **Step 2: Run to verify it fails**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_runner.py::test_subagent_activity_interleaves_flat -v`
Expected: FAIL — no `"agent"` tool in the registry (`tool_result` for a missing tool / unknown-tool error text in events).

- [ ] **Step 3: Implement**

In `TurnRunner.__init__`, after `self._tools = {...}` (requires `from harness.tools.agent import agent_tool` at module top):

```python
        if subagent_system_prompt is not None:
            # snapshot BEFORE inserting: agent_tool filters itself out by
            # identity, and wrapping would break that check — the copy
            # guarantees subagents never see an agent tool at all
            sub_registry = dict(self._tools)
            agent = agent_tool(
                llm,
                sub_registry,
                policy=policy,
                asker=self._asker,
                system=subagent_system_prompt,
                on_tool_call=self._on_tool_call,
                compact_threshold=compact_threshold,
                keep_recent=keep_recent,
            )
            self._tools["agent"] = self._wrap(agent)
```

- [ ] **Step 4: Run the full runner suite**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_runner.py -v`
Expected: all pass (12 tests).

- [ ] **Step 5: Commit**

```bash
git add ui/server/runner.py ui/server/tests/test_runner.py
git commit -m "ui: per-session subagent tool, flat event interleave"
```

---

### Task 9: FastAPI app — EventSink, deps, REST routes

**Files:**
- Create: `ui/server/app.py`
- Test: `ui/server/tests/test_app.py`

**Interfaces:**
- Consumes: `InMemorySessionStore`/`Session` (Task 3), `TurnRunner` (Task 5), `events` (Task 2).
- Produces:
  - `class EventSink` — `attach(loop, queue)`, `detach()`, `push(event)` (thread-safe; drops when detached).
  - `@dataclass HarnessDeps` — `llm`, `tools: dict`, `policy_factory: Callable[[], PermissionPolicy | None]`, `system_prompt: Callable[[], str]`, `subagent_system_prompt: Callable[[], str] | None`, `mode: str`, `workspace: str`, `compact_threshold: int | None = None`.
  - `create_app(deps: HarnessDeps, store: InMemorySessionStore | None = None) -> FastAPI` with routes `GET /api/sessions`, `POST /api/sessions`, `GET /api/meta` (WS endpoint arrives in Task 10; static mount in Task 11).

- [ ] **Step 1: Write the failing tests**

`ui/server/tests/test_app.py`:

```python
import asyncio

from fastapi.testclient import TestClient

from ui.server.app import EventSink, HarnessDeps, create_app
from ui.server.tests.fake_llm import FakeLLM, text_reply


def make_deps(replies=None, tools=None):
    return HarnessDeps(
        llm=FakeLLM(replies or [text_reply("hi")]),
        tools=tools or {},
        policy_factory=lambda: None,
        system_prompt=lambda: "the system prompt",
        subagent_system_prompt=None,
        mode="default",
        workspace="/tmp/workspace",
    )


def test_sessions_create_and_list():
    client = TestClient(create_app(make_deps()))
    assert client.get("/api/sessions").json() == []
    created = client.post("/api/sessions").json()
    assert set(created) == {"id", "created_at", "updated_at"}
    listed = client.get("/api/sessions").json()
    assert [s["id"] for s in listed] == [created["id"]]


def test_meta_reports_mode_workspace_and_system_prompt():
    client = TestClient(create_app(make_deps()))
    meta = client.get("/api/meta").json()
    assert meta == {
        "mode": "default",
        "workspace": "/tmp/workspace",
        "system_prompt": "the system prompt",
    }


def test_event_sink_delivers_and_drops():
    async def scenario():
        sink = EventSink()
        sink.push({"type": "dropped"})  # detached: silently dropped
        q = asyncio.Queue()
        sink.attach(asyncio.get_running_loop(), q)
        await asyncio.to_thread(sink.push, {"type": "delivered"})
        assert await asyncio.wait_for(q.get(), 1) == {"type": "delivered"}
        sink.detach()
        sink.push({"type": "dropped again"})
        assert q.empty()

    asyncio.run(scenario())
```

- [ ] **Step 2: Run to verify failure**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.server.app'`.

- [ ] **Step 3: Implement**

`ui/server/app.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_app.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/server/app.py ui/server/tests/test_app.py
git commit -m "ui: FastAPI app factory, session/meta routes, EventSink"
```

---

### Task 10: The WebSocket endpoint

**Files:**
- Modify: `ui/server/app.py`
- Test: `ui/server/tests/test_ws.py`

**Interfaces:**
- Produces: `WS /api/sessions/{session_id}/ws`. Protocol: server sends `session_snapshot` immediately after accept, then events as they happen; client sends `user_message`/`permission_answer`/`cancel`. Unknown session → close code 4404. A second connection to the same session supersedes the first (old socket closed with code 4000). Malformed/unknown client messages are ignored. ONLY the sender task writes to the socket (snapshot travels through the queue too — single-writer discipline).

- [ ] **Step 1: Write the failing tests**

`ui/server/tests/test_ws.py`:

```python
import threading

from fastapi.testclient import TestClient
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool

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
    client = TestClient(create_app(make_deps()))
    with client.websocket_connect("/api/sessions/nope/ws") as ws:
        assert ws.receive_json() == {"type": "close", "code": 4404}  # see impl note


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
```

Implementation note for `test_unknown_session_closes_4404`: Starlette's `TestClient` surfaces a server-initiated close as a `websocket.close` message; if the installed version raises `WebSocketDisconnect` instead, assert that — adjust the test to the observed behavior and leave a comment naming the version.

- [ ] **Step 2: Run to verify failure**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_ws.py -v`
Expected: FAIL — 404 on the WS route (endpoint doesn't exist yet).

- [ ] **Step 3: Implement the endpoint**

Add to `create_app` in `ui/server/app.py` (new imports at top: `import json`, `from fastapi import WebSocket`, `from starlette.websockets import WebSocketDisconnect`; module import of `events`: `from ui.server import events`):

```python
    active_sockets: dict[str, WebSocket] = {}

    async def _drain(queue: asyncio.Queue, ws: WebSocket) -> None:
        while True:
            await ws.send_json(await queue.get())

    async def _run_turn(runner: TurnRunner, session_id: str, text: str) -> None:
        await asyncio.to_thread(runner.run_turn_blocking, text)
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
                            asyncio.create_task(
                                _run_turn(runner, session_id, msg["text"])
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
```

- [ ] **Step 4: Run the ws suite, then the whole backend suite**

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests/test_ws.py -v`
Expected: 6 passed (adjust the 4404 assertion per the implementation note if needed).

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests -v`
Expected: all pass.

Run: `uv run pytest`
Expected: harness suite still green, ui still not collected.

- [ ] **Step 5: Commit**

```bash
git add ui/server/app.py ui/server/tests/test_ws.py
git commit -m "ui: websocket endpoint — snapshot, turn events, permission answers, cancel"
```

---

### Task 11: Real wiring (`__main__.py`) and static file serving

**Files:**
- Create: `ui/server/__main__.py`
- Modify: `ui/server/app.py` (static mount)

**Interfaces:**
- Consumes: `CodexAdapter` (`.context_window`), `MODES`, `PermissionPolicy`, `build_system_prompt`/`Environment`, `default_sandbox`/`SandboxPolicy`, tool factories, `create_app`/`HarnessDeps`.
- Produces: `uv run --group ui python -m ui.server --workspace DIR [--mode MODE] [--host H] [--port P]`; `create_app` mounts `ui/frontend/dist` at `/` when it exists.

- [ ] **Step 1: Add the static mount to `create_app`**

At the end of `create_app`, just before `return app` (new imports: `from pathlib import Path`, `from fastapi.staticfiles import StaticFiles`):

```python
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
```

Run: `UI_TESTS=1 uv run --group ui pytest ui/server/tests -v`
Expected: still all green (no `dist/` exists yet; mount is skipped).

- [ ] **Step 2: Write `__main__.py`**

`ui/server/__main__.py`:

```python
"""Real wiring: the web equivalent of main.py's REPL setup."""

import argparse
import datetime
import platform
from pathlib import Path

import uvicorn

from harness.llm import CodexAdapter
from harness.permissions import MODES, PermissionPolicy
from harness.prompts import Environment, build_system_prompt
from harness.sandbox import SandboxPolicy, default_sandbox
from harness.tools.bash import bash_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool

from ui.server.app import HarnessDeps, create_app

KEEP_RECENT = 8  # mirror main.py
COMPACT_FRACTION = 0.8

SUBAGENT_SECTION = (
    "You are a subagent: another agent delegated one self-contained "
    "task to you. Work it to completion and make your final reply "
    "the complete answer — it is the only thing the delegating "
    "agent will see."
)


def environment(workspace: Path) -> Environment:
    return Environment(
        cwd=str(Path.cwd().resolve()),
        workspace=str(workspace),
        os=platform.platform(),
        date=datetime.date.today().isoformat(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-harness web UI server")
    parser.add_argument("--mode", choices=MODES, default="default")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="root the agent may read/write/run within (default: cwd)",
    )
    parser.add_argument("--host", default="127.0.0.1")  # localhost only: no auth
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    sandbox = default_sandbox(SandboxPolicy(workspace))
    llm = CodexAdapter()
    tools = {
        tool.name: tool
        for tool in [
            read_file_tool(workspace=workspace),
            write_file_tool(workspace=workspace),
            list_dir_tool(workspace=workspace),
            bash_tool(sandbox=sandbox),
        ]
    }
    deps = HarnessDeps(
        llm=llm,
        tools=tools,
        policy_factory=lambda: PermissionPolicy(args.mode),
        system_prompt=lambda: build_system_prompt(environment(workspace)),
        subagent_system_prompt=lambda: build_system_prompt(
            environment(workspace), extra_sections=[SUBAGENT_SECTION]
        ),
        mode=args.mode,
        workspace=str(workspace),
        compact_threshold=int(COMPACT_FRACTION * llm.context_window),
    )
    uvicorn.run(create_app(deps), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Smoke the wiring without codex credentials**

Run: `uv run --group ui python -c "import ui.server.__main__"`
Expected: imports cleanly (constructing CodexAdapter needs credentials; importing must not).

If codex credentials exist locally (`~/.codex/auth.json`), optionally:
Run: `uv run --group ui python -m ui.server --workspace /tmp --port 8801` then `curl -s 127.0.0.1:8801/api/meta` in another shell.
Expected: JSON with mode/workspace/system_prompt. Ctrl-C the server.

- [ ] **Step 4: Commit**

```bash
git add ui/server/__main__.py ui/server/app.py
git commit -m "ui: real server wiring and static frontend mount"
```

---

### Task 12: Frontend scaffold

**Files:**
- Create: `ui/frontend/package.json`, `ui/frontend/tsconfig.json`, `ui/frontend/vite.config.ts`, `ui/frontend/index.html`, `ui/frontend/.gitignore`, `ui/frontend/src/main.tsx`, `ui/frontend/src/App.tsx`, `ui/frontend/src/styles.css`, `ui/frontend/src/test-setup.ts`

**Interfaces:**
- Produces: `npm run dev` (Vite, proxies `/api` incl. WS to `127.0.0.1:8000`), `npm run build` (tsc + vite → `dist/`), `npm test` (vitest, jsdom). `App` is a placeholder shell replaced in Task 16.

- [ ] **Step 1: Write the config files**

`ui/frontend/package.json`:

```json
{
  "name": "agent-harness-ui-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^19.1.0",
    "react-dom": "^19.1.0"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.0",
    "@testing-library/react": "^16.3.0",
    "@testing-library/user-event": "^14.6.0",
    "@types/react": "^19.1.0",
    "@types/react-dom": "^19.1.0",
    "@vitejs/plugin-react": "^4.6.0",
    "jsdom": "^26.1.0",
    "typescript": "~5.8.0",
    "vite": "^7.0.0",
    "vitest": "^3.2.0"
  }
}
```

`ui/frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "skipLibCheck": true,
    "noEmit": true,
    "types": ["vite/client", "vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src"]
}
```

`ui/frontend/vite.config.ts`:

```ts
/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test-setup.ts',
  },
})
```

`ui/frontend/.gitignore`:

```
node_modules
dist
```

`ui/frontend/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>agent harness</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`ui/frontend/src/test-setup.ts`:

```ts
import '@testing-library/jest-dom/vitest'
```

`ui/frontend/src/main.tsx`:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

`ui/frontend/src/App.tsx` (placeholder until Task 16):

```tsx
export default function App() {
  return <div className="app">agent harness web ui — scaffold</div>
}
```

`ui/frontend/src/styles.css` (final layout skeleton now; component classes land with their components):

```css
:root {
  color-scheme: light dark;
  font-family: ui-sans-serif, system-ui, sans-serif;
  --border: color-mix(in srgb, currentColor 18%, transparent);
  --muted: color-mix(in srgb, currentColor 55%, transparent);
  --accent: #4a7dff;
}
* { box-sizing: border-box; }
body { margin: 0; }
.app { display: grid; grid-template-columns: 220px 1fr; height: 100vh; }
.app.with-inspector { grid-template-columns: 220px 1fr 360px; }

.sidebar { border-right: 1px solid var(--border); padding: 0.75rem; overflow-y: auto; }
.sidebar button.new-session { width: 100%; margin-bottom: 0.75rem; }
.sidebar .session { display: block; width: 100%; text-align: left; padding: 0.4rem;
  border: 0; background: none; cursor: pointer; border-radius: 6px; }
.sidebar .session.active { background: var(--border); }
.sidebar .mode { margin-top: 1rem; font-size: 0.8rem; color: var(--muted); }

.main { display: flex; flex-direction: column; min-width: 0; }
.header { display: flex; gap: 1rem; align-items: center; padding: 0.5rem 1rem;
  border-bottom: 1px solid var(--border); font-size: 0.85rem; color: var(--muted); }
.header .spacer { flex: 1; }

.transcript { flex: 1; overflow-y: auto; padding: 1rem; display: flex;
  flex-direction: column; gap: 0.6rem; }
.bubble { max-width: 46rem; padding: 0.6rem 0.9rem; border-radius: 10px;
  white-space: pre-wrap; cursor: pointer; }
.bubble.user { align-self: flex-end; background: var(--accent); color: white; }
.bubble.assistant { align-self: flex-start; border: 1px solid var(--border); }
.bubble.selected { outline: 2px solid var(--accent); }
.streaming-cursor::after { content: '▋'; animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

.tool-card { align-self: stretch; border: 1px solid var(--border); border-radius: 8px;
  font-family: ui-monospace, monospace; font-size: 0.85rem; cursor: pointer; }
.tool-card summary { padding: 0.4rem 0.8rem; cursor: pointer; list-style: none; }
.tool-card .pending { color: var(--muted); }
.tool-card pre { margin: 0; padding: 0.5rem 0.8rem; overflow-x: auto;
  border-top: 1px solid var(--border); }

.permission { align-self: stretch; border: 1px solid var(--accent); border-radius: 8px;
  padding: 0.6rem 0.9rem; }
.permission .buttons { display: flex; gap: 0.5rem; margin-top: 0.5rem; }
.permission .answered { color: var(--muted); font-style: italic; }

.notice { align-self: center; color: var(--muted); font-size: 0.85rem; }
.compaction-divider { align-self: stretch; text-align: center; color: var(--muted);
  font-size: 0.8rem; border-top: 1px dashed var(--border); padding-top: 0.3rem; }

.composer { display: flex; gap: 0.5rem; padding: 0.75rem 1rem;
  border-top: 1px solid var(--border); }
.composer textarea { flex: 1; resize: none; min-height: 3rem; padding: 0.5rem;
  border-radius: 8px; border: 1px solid var(--border); background: transparent;
  color: inherit; font: inherit; }

.inspector { border-left: 1px solid var(--border); overflow-y: auto; padding: 0.75rem;
  font-size: 0.8rem; }
.inspector pre { white-space: pre-wrap; word-break: break-word; }
.inspector .tabs { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; }
```

- [ ] **Step 2: Install and verify the toolchain**

Run: `cd ui/frontend && npm install`
Expected: installs cleanly; `package-lock.json` created (commit it).

Run: `cd ui/frontend && npm run build`
Expected: `dist/` produced, no TS errors.

Run: `cd ui/frontend && npm test -- --passWithNoTests`
Expected: vitest runs, exits 0 (no test files yet; the flag is only needed this once).

- [ ] **Step 3: Commit**

```bash
git add ui/frontend
git commit -m "ui: vite/react/ts frontend scaffold with test toolchain"
```

---

### Task 13: Event types and the reducer

**Files:**
- Create: `ui/frontend/src/types/events.ts`, `ui/frontend/src/state/reducer.ts`
- Test: `ui/frontend/src/state/reducer.test.ts`

**Interfaces:**
- Produces (consumed by every later frontend task):

```ts
// types/events.ts
export type Message = { role: string } & Record<string, unknown>
export interface PermissionRequest { id: string; name: string; args: Record<string, unknown> }
export type ServerEvent = /* discriminated union mirroring events.py, below */
export type ClientMessage =
  | { type: 'user_message'; text: string }
  | { type: 'permission_answer'; id: string; answer: 'yes' | 'no' | 'always' }
  | { type: 'cancel' }

// state/reducer.ts
export type TranscriptItem =
  | { kind: 'user'; text: string; message?: Message }
  | { kind: 'assistant'; text: string; streaming: boolean; message?: Message }
  | { kind: 'tool'; name: string; args: Record<string, unknown>; result: string | null; message?: Message }
  | { kind: 'permission'; id: string; name: string; args: Record<string, unknown>; answer: string | null }
  | { kind: 'compaction'; summarized: number }
  | { kind: 'notice'; text: string }
export interface SessionState {
  items: TranscriptItem[]; rawMessages: Message[]; turnRunning: boolean;
  pendingPermission: PermissionRequest | null; turnStartIndex: number; lastError: string | null;
}
export type Action = ServerEvent
  | { type: 'local_user_message'; text: string }
  | { type: 'local_permission_answer'; id: string; answer: string }
  | { type: 'reset' }
export const initialState: SessionState
export function reducer(state: SessionState, action: Action): SessionState
export function buildItemsFromMessages(messages: Message[]): TranscriptItem[]
```

- [ ] **Step 1: Write `types/events.ts`** (types only — no test)

```ts
export type Message = { role: string } & Record<string, unknown>

export interface PermissionRequest {
  id: string
  name: string
  args: Record<string, unknown>
}

export type ServerEvent =
  | { type: 'session_snapshot'; messages: Message[]; turn_running: boolean;
      pending_permission: PermissionRequest | null; streamed_text: string }
  | { type: 'turn_started' }
  | { type: 'text_delta'; text: string }
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; name: string; result: string }
  | { type: 'permission_request'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'compaction'; summarized: number }
  | { type: 'turn_done'; messages: Message[] }
  | { type: 'turn_cancelled' }
  | { type: 'turn_error'; message: string }

export type ClientMessage =
  | { type: 'user_message'; text: string }
  | { type: 'permission_answer'; id: string; answer: 'yes' | 'no' | 'always' }
  | { type: 'cancel' }
```

- [ ] **Step 2: Write the failing reducer tests**

`ui/frontend/src/state/reducer.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import type { Message } from '../types/events'
import {
  buildItemsFromMessages, initialState, reducer, type Action, type SessionState,
} from './reducer'

function play(actions: Action[], from: SessionState = initialState): SessionState {
  return actions.reduce(reducer, from)
}

const toolCallMessage: Message = {
  role: 'assistant', content: null,
  tool_calls: [{ id: 'call-1', type: 'function',
    function: { name: 'echo', arguments: '{"x": 1}' } }],
}
const toolResultMessage: Message = { role: 'tool', tool_call_id: 'call-1', content: 'echo:1' }

describe('buildItemsFromMessages', () => {
  it('maps user, assistant, and tool exchanges', () => {
    const items = buildItemsFromMessages([
      { role: 'user', content: 'hi' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'done' },
    ])
    expect(items.map((i) => i.kind)).toEqual(['user', 'tool', 'assistant'])
    expect(items[1]).toMatchObject({ name: 'echo', args: { x: 1 }, result: 'echo:1' })
    expect(items[2]).toMatchObject({ text: 'done', streaming: false })
    expect(items[2].kind === 'assistant' && items[2].message).toBeTruthy()
  })
})

describe('reducer', () => {
  it('snapshot rebuilds state including a mid-turn stream', () => {
    const state = play([{
      type: 'session_snapshot',
      messages: [{ role: 'user', content: 'q' }],
      turn_running: true,
      pending_permission: { id: 'perm-1', name: 'bash', args: { command: 'ls' } },
      streamed_text: 'partial ans',
    }])
    expect(state.turnRunning).toBe(true)
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'permission'])
    expect(state.items[1]).toMatchObject({ text: 'partial ans', streaming: true })
    expect(state.pendingPermission?.id).toBe('perm-1')
  })

  it('live turn: user, deltas, tool events, done reconciliation', () => {
    const authoritative: Message[] = [
      { role: 'user', content: 'do it' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'all done' },
    ]
    const state = play([
      { type: 'local_user_message', text: 'do it' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
      { type: 'tool_result', name: 'echo', result: 'echo:1' },
      { type: 'text_delta', text: 'all ' },
      { type: 'text_delta', text: 'done' },
      { type: 'turn_done', messages: authoritative },
    ])
    expect(state.turnRunning).toBe(false)
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'tool', 'assistant'])
    expect(state.items[2]).toMatchObject({ text: 'all done', streaming: false })
    expect(state.rawMessages).toEqual(authoritative)
    // reconciliation attached authoritative dicts to this turn's items
    expect(state.items[0].kind === 'user' && state.items[0].message).toEqual(authoritative[0])
    expect(state.items[2].kind === 'assistant' && state.items[2].message).toEqual(authoritative[3])
  })

  it('permission flow: request appends item, local answer records it', () => {
    const state = play([
      { type: 'local_user_message', text: 'go' },
      { type: 'permission_request', id: 'perm-1', name: 'bash', args: { command: 'rm' } },
      { type: 'local_permission_answer', id: 'perm-1', answer: 'no' },
    ])
    const perm = state.items.find((i) => i.kind === 'permission')
    expect(perm).toMatchObject({ id: 'perm-1', answer: 'no' })
    expect(state.pendingPermission).toBeNull()
  })

  it('degraded mode: whole text arrives only at turn_done', () => {
    const state = play([
      { type: 'local_user_message', text: 'hi' },
      { type: 'turn_started' },
      { type: 'turn_done', messages: [
        { role: 'user', content: 'hi' },
        { role: 'assistant', content: 'whole answer' },
      ]},
    ])
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'assistant'])
    expect(state.items[1]).toMatchObject({ text: 'whole answer' })
  })

  it('cancel and error drop the turn items and leave a notice', () => {
    const base: Action[] = [
      { type: 'local_user_message', text: 'q1' },
      { type: 'turn_started' },
      { type: 'turn_done', messages: [
        { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' },
      ]},
      { type: 'local_user_message', text: 'q2' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
    ]
    const cancelled = play([...base, { type: 'turn_cancelled' }])
    expect(cancelled.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'notice'])
    const failed = play([...base, { type: 'turn_error', message: 'RuntimeError: boom' }])
    expect(failed.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'notice'])
    expect(failed.lastError).toBe('RuntimeError: boom')
  })

  it('replay equals live for the message-backed items', () => {
    const authoritative: Message[] = [
      { role: 'user', content: 'do it' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'all done' },
    ]
    const live = play([
      { type: 'local_user_message', text: 'do it' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
      { type: 'tool_result', name: 'echo', result: 'echo:1' },
      { type: 'turn_done', messages: authoritative },
    ])
    const replayed = buildItemsFromMessages(authoritative)
    expect(live.items.map(({ message, ...rest }) => rest))
      .toEqual(replayed.map(({ message, ...rest }) => rest))
  })
})
```

- [ ] **Step 3: Run to verify failure**

Run: `cd ui/frontend && npm test`
Expected: FAIL — `reducer.ts` doesn't exist.

- [ ] **Step 4: Implement the reducer**

`ui/frontend/src/state/reducer.ts`:

```ts
import type { Message, PermissionRequest, ServerEvent } from '../types/events'

export type TranscriptItem =
  | { kind: 'user'; text: string; message?: Message }
  | { kind: 'assistant'; text: string; streaming: boolean; message?: Message }
  | { kind: 'tool'; name: string; args: Record<string, unknown>;
      result: string | null; message?: Message }
  | { kind: 'permission'; id: string; name: string;
      args: Record<string, unknown>; answer: string | null }
  | { kind: 'compaction'; summarized: number }
  | { kind: 'notice'; text: string }

export interface SessionState {
  items: TranscriptItem[]
  rawMessages: Message[]
  turnRunning: boolean
  pendingPermission: PermissionRequest | null
  turnStartIndex: number
  lastError: string | null
}

export type Action =
  | ServerEvent
  | { type: 'local_user_message'; text: string }
  | { type: 'local_permission_answer'; id: string; answer: string }
  | { type: 'reset' }

export const initialState: SessionState = {
  items: [],
  rawMessages: [],
  turnRunning: false,
  pendingPermission: null,
  turnStartIndex: 0,
  lastError: null,
}

export function buildItemsFromMessages(messages: Message[]): TranscriptItem[] {
  const items: TranscriptItem[] = []
  const toolItemsByCallId = new Map<string, Extract<TranscriptItem, { kind: 'tool' }>>()
  for (const message of messages) {
    if (message.role === 'user' && typeof message.content === 'string') {
      items.push({ kind: 'user', text: message.content, message })
    } else if (message.role === 'assistant') {
      const calls = (message.tool_calls ?? []) as Array<{
        id: string; function: { name: string; arguments: string }
      }>
      for (const call of calls) {
        const item: Extract<TranscriptItem, { kind: 'tool' }> = {
          kind: 'tool',
          name: call.function.name,
          args: safeParse(call.function.arguments),
          result: null,
          message,
        }
        toolItemsByCallId.set(call.id, item)
        items.push(item)
      }
      if (typeof message.content === 'string' && message.content) {
        items.push({ kind: 'assistant', text: message.content, streaming: false, message })
      }
    } else if (message.role === 'tool') {
      const item = toolItemsByCallId.get(message.tool_call_id as string)
      if (item) item.result = String(message.content ?? '')
    }
  }
  return items
}

function safeParse(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw)
  } catch {
    return { raw }
  }
}

/** Attach authoritative message dicts to this turn's live items, walking
 * both lists from the tail. Live-only items (permission, compaction,
 * notice) are skipped — the inspector shows them as ephemeral. */
function attachAuthoritative(
  items: TranscriptItem[], turnStartIndex: number, messages: Message[],
): TranscriptItem[] {
  const out = items.map((item) => ({ ...item }))
  let messageIndex = messages.length - 1
  for (let i = out.length - 1; i >= turnStartIndex && messageIndex >= 0; i--) {
    const item = out[i]
    if (item.kind === 'permission' || item.kind === 'compaction' || item.kind === 'notice') continue
    for (let m = messageIndex; m >= 0; m--) {
      const message = messages[m]
      if (
        (item.kind === 'user' && message.role === 'user') ||
        (item.kind === 'assistant' && message.role === 'assistant' &&
          typeof message.content === 'string' && message.content !== null) ||
        (item.kind === 'tool' && message.role === 'assistant' &&
          Array.isArray(message.tool_calls))
      ) {
        item.message = message
        messageIndex = m - 1
        break
      }
    }
  }
  return out
}

function closeStream(items: TranscriptItem[]): TranscriptItem[] {
  const last = items[items.length - 1]
  if (last?.kind === 'assistant' && last.streaming) {
    return [...items.slice(0, -1), { ...last, streaming: false }]
  }
  return items
}

export function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case 'reset':
      return initialState
    case 'session_snapshot': {
      const items = buildItemsFromMessages(action.messages)
      if (action.streamed_text) {
        items.push({ kind: 'assistant', text: action.streamed_text, streaming: true })
      }
      if (action.pending_permission) {
        items.push({ ...action.pending_permission, kind: 'permission', answer: null })
      }
      return {
        items,
        rawMessages: action.messages,
        turnRunning: action.turn_running,
        pendingPermission: action.pending_permission,
        turnStartIndex: items.length,
        lastError: null,
      }
    }
    case 'local_user_message':
      return {
        ...state,
        turnStartIndex: state.items.length,
        items: [...state.items, { kind: 'user', text: action.text }],
        turnRunning: true,
        lastError: null,
      }
    case 'turn_started':
      return { ...state, turnRunning: true }
    case 'text_delta': {
      const last = state.items[state.items.length - 1]
      if (last?.kind === 'assistant' && last.streaming) {
        const grown = { ...last, text: last.text + action.text }
        return { ...state, items: [...state.items.slice(0, -1), grown] }
      }
      return {
        ...state,
        items: [...state.items, { kind: 'assistant', text: action.text, streaming: true }],
      }
    }
    case 'tool_call':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'tool', name: action.name, args: action.args, result: null }],
      }
    case 'tool_result': {
      const items = [...state.items]
      for (let i = items.length - 1; i >= 0; i--) {
        const item = items[i]
        if (item.kind === 'tool' && item.name === action.name && item.result === null) {
          items[i] = { ...item, result: action.result }
          break
        }
      }
      return { ...state, items }
    }
    case 'permission_request':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'permission', id: action.id, name: action.name,
            args: action.args, answer: null }],
        pendingPermission: { id: action.id, name: action.name, args: action.args },
      }
    case 'local_permission_answer':
      return {
        ...state,
        items: state.items.map((item) =>
          item.kind === 'permission' && item.id === action.id
            ? { ...item, answer: action.answer }
            : item,
        ),
        pendingPermission: null,
      }
    case 'compaction':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'compaction', summarized: action.summarized }],
      }
    case 'turn_done':
      return {
        ...state,
        items: attachAuthoritative(
          closeStream(state.items), state.turnStartIndex, action.messages),
        rawMessages: action.messages,
        turnRunning: false,
        pendingPermission: null,
      }
    case 'turn_cancelled':
      return {
        ...state,
        items: [...state.items.slice(0, state.turnStartIndex),
          { kind: 'notice', text: 'turn cancelled' }],
        turnRunning: false,
        pendingPermission: null,
      }
    case 'turn_error':
      return {
        ...state,
        items: [...state.items.slice(0, state.turnStartIndex),
          { kind: 'notice', text: `turn failed: ${action.message}` }],
        turnRunning: false,
        pendingPermission: null,
        lastError: action.message,
      }
    default:
      return state
  }
}
```

Note on the turn_done reconciliation test: the live assistant item was built from deltas; reconciliation must also sync its text to the authoritative content. Extend `attachAuthoritative`: when attaching to an `assistant` item, also set `text: message.content as string` and `streaming: false`. (The test `live turn: ...` pins this.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ui/frontend && npm test`
Expected: all reducer tests pass.

- [ ] **Step 6: Commit**

```bash
git add ui/frontend/src/types ui/frontend/src/state
git commit -m "ui: event types and event-sourced transcript reducer"
```

---

### Task 14: WebSocket client

**Files:**
- Create: `ui/frontend/src/ws/client.ts`
- Test: `ui/frontend/src/ws/client.test.ts`

**Interfaces:**
- Produces:

```ts
export type SocketStatus = 'connecting' | 'open' | 'closed'
export class SessionSocket {
  constructor(
    url: string,
    onEvent: (event: ServerEvent) => void,
    onStatus: (status: SocketStatus) => void,
    wsFactory?: (url: string) => WebSocket,   // injectable for tests
  )
  connect(): void
  close(): void                                // intentional: no reconnect
  send(message: ClientMessage): void           // silently dropped unless open
}
```
- Reconnect on unexpected close with capped exponential backoff (500ms · 2ⁿ, max 10s); no reconnect after `close()` or a 4000/4404 close code (superseded/unknown — reconnecting would fight the other tab).

- [ ] **Step 1: Write the failing tests**

`ui/frontend/src/ws/client.test.ts`:

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionSocket, type SocketStatus } from './client'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onopen: (() => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  sent: string[] = []
  readyState = 0 // CONNECTING
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3 }
  open() { this.readyState = 1; this.onopen?.() }
  serverClose(code: number) { this.readyState = 3; this.onclose?.({ code }) }
  message(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }) }
}

describe('SessionSocket', () => {
  let events: unknown[]
  let statuses: SocketStatus[]
  let socket: SessionSocket

  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    events = []
    statuses = []
    socket = new SessionSocket(
      'ws://test/api/sessions/s1/ws',
      (e) => events.push(e),
      (s) => statuses.push(s),
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
    )
  })
  afterEach(() => vi.useRealTimers())

  it('delivers parsed events and sends only when open', () => {
    socket.connect()
    const ws = FakeWebSocket.instances[0]
    socket.send({ type: 'user_message', text: 'dropped' })
    expect(ws.sent).toEqual([])
    ws.open()
    socket.send({ type: 'user_message', text: 'hi' })
    expect(JSON.parse(ws.sent[0])).toEqual({ type: 'user_message', text: 'hi' })
    ws.message({ type: 'turn_started' })
    expect(events).toEqual([{ type: 'turn_started' }])
    expect(statuses).toEqual(['connecting', 'open'])
  })

  it('reconnects with backoff on unexpected close', () => {
    socket.connect()
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].serverClose(1006)
    expect(statuses.at(-1)).toBe('closed')
    vi.advanceTimersByTime(500)
    expect(FakeWebSocket.instances).toHaveLength(2)
    FakeWebSocket.instances[1].serverClose(1006)
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances).toHaveLength(2) // backoff doubled
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(3)
  })

  it('does not reconnect after intentional close or supersede codes', () => {
    socket.connect()
    FakeWebSocket.instances[0].open()
    socket.close()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)

    socket.connect()
    FakeWebSocket.instances[1].open()
    FakeWebSocket.instances[1].serverClose(4000)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui/frontend && npm test`
Expected: FAIL — `client.ts` doesn't exist.

- [ ] **Step 3: Implement**

`ui/frontend/src/ws/client.ts`:

```ts
import type { ClientMessage, ServerEvent } from '../types/events'

export type SocketStatus = 'connecting' | 'open' | 'closed'

const NO_RECONNECT_CODES = new Set([4000, 4404]) // superseded / unknown session

export class SessionSocket {
  private ws: WebSocket | null = null
  private attempts = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private closed = false

  constructor(
    private url: string,
    private onEvent: (event: ServerEvent) => void,
    private onStatus: (status: SocketStatus) => void,
    private wsFactory: (url: string) => WebSocket = (u) => new WebSocket(u),
  ) {}

  connect(): void {
    this.closed = false
    this.onStatus('connecting')
    const ws = this.wsFactory(this.url)
    this.ws = ws
    ws.onopen = () => {
      this.attempts = 0
      this.onStatus('open')
    }
    ws.onmessage = (e) => {
      try {
        this.onEvent(JSON.parse(e.data as string) as ServerEvent)
      } catch {
        // malformed server frame: ignore
      }
    }
    ws.onclose = (e) => {
      this.onStatus('closed')
      if (this.closed || NO_RECONNECT_CODES.has(e.code)) return
      const delay = Math.min(500 * 2 ** this.attempts, 10_000)
      this.attempts += 1
      this.timer = setTimeout(() => this.connect(), delay)
    }
  }

  close(): void {
    this.closed = true
    if (this.timer) clearTimeout(this.timer)
    this.ws?.close()
  }

  send(message: ClientMessage): void {
    if (this.ws && this.ws.readyState === 1 /* OPEN */) {
      this.ws.send(JSON.stringify(message))
    }
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ui/frontend && npm test`
Expected: reducer + client suites pass.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/ws
git commit -m "ui: reconnecting websocket client"
```

---

### Task 15: Transcript components

**Files:**
- Create: `ui/frontend/src/components/Transcript.tsx`, `ui/frontend/src/components/ToolCard.tsx`, `ui/frontend/src/components/PermissionPrompt.tsx`, `ui/frontend/src/components/Composer.tsx`
- Test: `ui/frontend/src/components/PermissionPrompt.test.tsx`

**Interfaces:**
- Consumes: `TranscriptItem`, `SessionState` (Task 13).
- Produces:
  - `Transcript({ items, selectedIndex, onSelect }: { items: TranscriptItem[]; selectedIndex: number | null; onSelect: (i: number) => void })` — permission items render `PermissionPrompt` ONLY as answered records here; the live prompt with buttons renders in App (it needs the socket).
  - `ToolCard({ item, selected, onSelect })` — `<details>` card, collapsed summary `name(args…)`, expanded shows args JSON + result (or "running…").
  - `PermissionPrompt({ name, args, answer, onAnswer }: { name: string; args: Record<string, unknown>; answer: string | null; onAnswer?: (a: 'yes' | 'no' | 'always') => void })` — buttons hidden once `answer` is set.
  - `Composer({ disabled, onSend, onCancel, turnRunning, initialText }: { disabled: boolean; onSend: (text: string) => void; onCancel: () => void; turnRunning: boolean; initialText: string })` — textarea + Send (Enter) / Cancel while running; `initialText` restores input after `turn_error`.

- [ ] **Step 1: Write the failing PermissionPrompt test**

`ui/frontend/src/components/PermissionPrompt.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { PermissionPrompt } from './PermissionPrompt'

describe('PermissionPrompt', () => {
  it('shows the tool, args, and three answers', async () => {
    const onAnswer = vi.fn()
    render(
      <PermissionPrompt
        name="bash" args={{ command: 'rm -rf build' }} answer={null} onAnswer={onAnswer}
      />,
    )
    expect(screen.getByText(/bash/)).toBeInTheDocument()
    expect(screen.getByText(/rm -rf build/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /always/i }))
    expect(onAnswer).toHaveBeenCalledWith('always')
  })

  it('renders the recorded decision without buttons once answered', () => {
    render(<PermissionPrompt name="bash" args={{}} answer="no" />)
    expect(screen.getByText(/denied/i)).toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui/frontend && npm test`
Expected: FAIL — component missing.

- [ ] **Step 3: Implement the four components**

`ui/frontend/src/components/PermissionPrompt.tsx`:

```tsx
const LABELS: Record<string, string> = {
  yes: 'allowed once', no: 'denied', always: 'always allowed for this tool',
}

export function PermissionPrompt({ name, args, answer, onAnswer }: {
  name: string
  args: Record<string, unknown>
  answer: string | null
  onAnswer?: (a: 'yes' | 'no' | 'always') => void
}) {
  return (
    <div className="permission">
      <div>
        agent wants to run <strong>{name}</strong>
        <code> {JSON.stringify(args)}</code>
      </div>
      {answer ? (
        <div className="answered">{LABELS[answer] ?? answer}</div>
      ) : (
        <div className="buttons">
          <button onClick={() => onAnswer?.('yes')}>Yes</button>
          <button onClick={() => onAnswer?.('no')}>No</button>
          <button onClick={() => onAnswer?.('always')}>Always for this tool</button>
        </div>
      )}
    </div>
  )
}
```

`ui/frontend/src/components/ToolCard.tsx`:

```tsx
import type { TranscriptItem } from '../state/reducer'

export function ToolCard({ item, selected, onSelect }: {
  item: Extract<TranscriptItem, { kind: 'tool' }>
  selected: boolean
  onSelect: () => void
}) {
  const summary = `${item.name}(${JSON.stringify(item.args)})`
  return (
    <details className={`tool-card${selected ? ' selected' : ''}`} onClick={onSelect}>
      <summary>
        ⚙ {summary.length > 120 ? summary.slice(0, 117) + '…' : summary}
        {item.result === null && <span className="pending"> · running…</span>}
      </summary>
      <pre>{JSON.stringify(item.args, null, 2)}</pre>
      {item.result !== null && <pre>{item.result}</pre>}
    </details>
  )
}
```

`ui/frontend/src/components/Transcript.tsx`:

```tsx
import type { TranscriptItem } from '../state/reducer'
import { PermissionPrompt } from './PermissionPrompt'
import { ToolCard } from './ToolCard'

export function Transcript({ items, selectedIndex, onSelect }: {
  items: TranscriptItem[]
  selectedIndex: number | null
  onSelect: (index: number) => void
}) {
  return (
    <div className="transcript">
      {items.map((item, index) => {
        const selected = index === selectedIndex
        const select = () => onSelect(index)
        switch (item.kind) {
          case 'user':
          case 'assistant': {
            const classes = ['bubble', item.kind]
            if (selected) classes.push('selected')
            if (item.kind === 'assistant' && item.streaming) classes.push('streaming-cursor')
            return (
              <div key={index} className={classes.join(' ')} onClick={select}>
                {item.text}
              </div>
            )
          }
          case 'tool':
            return <ToolCard key={index} item={item} selected={selected} onSelect={select} />
          case 'permission':
            // answered record only; the live prompt (with buttons) is App's
            return (
              <PermissionPrompt key={index} name={item.name}
                args={item.args} answer={item.answer ?? '(pending)'} />
            )
          case 'compaction':
            return (
              <div key={index} className="compaction-divider">
                {item.summarized} messages compacted into a summary
              </div>
            )
          case 'notice':
            return <div key={index} className="notice">{item.text}</div>
        }
      })}
    </div>
  )
}
```

`ui/frontend/src/components/Composer.tsx`:

```tsx
import { useEffect, useState } from 'react'

export function Composer({ disabled, onSend, onCancel, turnRunning, initialText }: {
  disabled: boolean
  onSend: (text: string) => void
  onCancel: () => void
  turnRunning: boolean
  initialText: string
}) {
  const [text, setText] = useState(initialText)
  useEffect(() => setText(initialText), [initialText])

  const send = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
  }

  return (
    <div className="composer">
      <textarea
        value={text}
        placeholder="Message the agent…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            send()
          }
        }}
      />
      {turnRunning
        ? <button onClick={onCancel}>Cancel</button>
        : <button onClick={send} disabled={disabled}>Send</button>}
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ui/frontend && npm test`
Expected: all suites pass. Then `npm run build` — no TS errors.

- [ ] **Step 5: Commit**

```bash
git add ui/frontend/src/components
git commit -m "ui: transcript, tool card, permission prompt, composer components"
```

---

### Task 16: App integration — sidebar, header, inspector, wiring

**Files:**
- Create: `ui/frontend/src/components/SessionSidebar.tsx`, `ui/frontend/src/components/InspectorPane.tsx`, `ui/frontend/src/components/Header.tsx`
- Modify: `ui/frontend/src/App.tsx` (replace the placeholder)

**Interfaces:**
- Consumes: everything above. REST: `GET/POST /api/sessions` → `{id, created_at, updated_at}[]`, `GET /api/meta` → `{mode, workspace, system_prompt}`.
- Produces:
  - `SessionSidebar({ sessions, activeId, mode, onSelect, onCreate })`
  - `InspectorPane({ item, systemPrompt }: { item: TranscriptItem | null; systemPrompt: string })` — tabs "message" / "system prompt"; message tab shows `JSON.stringify(item.message, null, 2)` or `(ephemeral — not part of the transcript state)` for live-only items, `(select a transcript item)` when nothing selected.
  - `Header({ meta, socketStatus, inspectorOpen, onToggleInspector })`

- [ ] **Step 1: Implement the three chrome components**

`ui/frontend/src/components/SessionSidebar.tsx`:

```tsx
export interface SessionMeta { id: string; created_at: number; updated_at: number }

export function SessionSidebar({ sessions, activeId, mode, onSelect, onCreate }: {
  sessions: SessionMeta[]
  activeId: string | null
  mode: string
  onSelect: (id: string) => void
  onCreate: () => void
}) {
  return (
    <nav className="sidebar">
      <button className="new-session" onClick={onCreate}>+ new session</button>
      {sessions.map((s) => (
        <button
          key={s.id}
          className={`session${s.id === activeId ? ' active' : ''}`}
          onClick={() => onSelect(s.id)}
        >
          {s.id} · {new Date(s.updated_at * 1000).toLocaleTimeString()}
        </button>
      ))}
      <div className="mode">mode: {mode}</div>
    </nav>
  )
}
```

`ui/frontend/src/components/InspectorPane.tsx`:

```tsx
import { useState } from 'react'
import type { TranscriptItem } from '../state/reducer'

export function InspectorPane({ item, systemPrompt }: {
  item: TranscriptItem | null
  systemPrompt: string
}) {
  const [tab, setTab] = useState<'message' | 'system'>('message')
  const message = item && 'message' in item ? item.message : undefined
  return (
    <aside className="inspector">
      <div className="tabs">
        <button onClick={() => setTab('message')} disabled={tab === 'message'}>message</button>
        <button onClick={() => setTab('system')} disabled={tab === 'system'}>system prompt</button>
      </div>
      {tab === 'system' ? (
        <pre>{systemPrompt}</pre>
      ) : item === null ? (
        <p>(select a transcript item)</p>
      ) : message === undefined ? (
        <p>(ephemeral — not part of the transcript state)</p>
      ) : (
        <pre>{JSON.stringify(message, null, 2)}</pre>
      )}
    </aside>
  )
}
```

`ui/frontend/src/components/Header.tsx`:

```tsx
import type { SocketStatus } from '../ws/client'

export interface Meta { mode: string; workspace: string; system_prompt: string }

export function Header({ meta, socketStatus, inspectorOpen, onToggleInspector }: {
  meta: Meta | null
  socketStatus: SocketStatus
  inspectorOpen: boolean
  onToggleInspector: () => void
}) {
  return (
    <div className="header">
      <span>workspace: {meta?.workspace ?? '…'}</span>
      <span>mode: {meta?.mode ?? '…'}</span>
      <span className="spacer" />
      <span>{socketStatus}</span>
      <button onClick={onToggleInspector}>
        {inspectorOpen ? 'hide inspector' : 'inspector'}
      </button>
    </div>
  )
}
```

- [ ] **Step 2: Replace `App.tsx`**

```tsx
import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { Composer } from './components/Composer'
import { Header, type Meta } from './components/Header'
import { InspectorPane } from './components/InspectorPane'
import { PermissionPrompt } from './components/PermissionPrompt'
import { SessionSidebar, type SessionMeta } from './components/SessionSidebar'
import { Transcript } from './components/Transcript'
import { initialState, reducer } from './state/reducer'
import { SessionSocket, type SocketStatus } from './ws/client'

function wsUrl(sessionId: string): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}/api/sessions/${sessionId}/ws`
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const [sessions, setSessions] = useState<SessionMeta[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [socketStatus, setSocketStatus] = useState<SocketStatus>('closed')
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [restoredInput, setRestoredInput] = useState('')
  const socketRef = useRef<SessionSocket | null>(null)
  const lastSentRef = useRef('')

  const refreshSessions = useCallback(async () => {
    setSessions(await (await fetch('/api/sessions')).json())
  }, [])

  useEffect(() => {
    refreshSessions()
    fetch('/api/meta').then(async (r) => setMeta(await r.json()))
  }, [refreshSessions])

  useEffect(() => {
    if (!activeId) return
    dispatch({ type: 'reset' })
    setSelectedIndex(null)
    const socket = new SessionSocket(wsUrl(activeId), (event) => {
      dispatch(event)
      if (event.type === 'turn_error') setRestoredInput(lastSentRef.current)
      if (event.type === 'turn_done') refreshSessions()
    }, setSocketStatus)
    socketRef.current = socket
    socket.connect()
    return () => socket.close()
  }, [activeId, refreshSessions])

  const createSession = async () => {
    const created = await (await fetch('/api/sessions', { method: 'POST' })).json()
    await refreshSessions()
    setActiveId(created.id)
  }

  const send = (text: string) => {
    lastSentRef.current = text
    setRestoredInput('')
    socketRef.current?.send({ type: 'user_message', text })
    dispatch({ type: 'local_user_message', text })
  }

  const answer = (a: 'yes' | 'no' | 'always') => {
    const pending = state.pendingPermission
    if (!pending) return
    socketRef.current?.send({ type: 'permission_answer', id: pending.id, answer: a })
    dispatch({ type: 'local_permission_answer', id: pending.id, answer: a })
  }

  return (
    <div className={`app${inspectorOpen ? ' with-inspector' : ''}`}>
      <SessionSidebar
        sessions={sessions} activeId={activeId} mode={meta?.mode ?? '…'}
        onSelect={setActiveId} onCreate={createSession}
      />
      <div className="main">
        <Header
          meta={meta} socketStatus={socketStatus} inspectorOpen={inspectorOpen}
          onToggleInspector={() => setInspectorOpen((v) => !v)}
        />
        {activeId ? (
          <>
            <Transcript
              items={state.items} selectedIndex={selectedIndex} onSelect={setSelectedIndex}
            />
            {state.pendingPermission && (
              <PermissionPrompt
                name={state.pendingPermission.name}
                args={state.pendingPermission.args}
                answer={null} onAnswer={answer}
              />
            )}
            <Composer
              disabled={state.turnRunning || socketStatus !== 'open'}
              onSend={send}
              onCancel={() => socketRef.current?.send({ type: 'cancel' })}
              turnRunning={state.turnRunning}
              initialText={restoredInput}
            />
          </>
        ) : (
          <div className="transcript">
            <div className="notice">create or pick a session to start</div>
          </div>
        )}
      </div>
      {inspectorOpen && (
        <InspectorPane
          item={selectedIndex === null ? null : state.items[selectedIndex] ?? null}
          systemPrompt={meta?.system_prompt ?? ''}
        />
      )}
    </div>
  )
}
```

Note: the pending permission renders twice by design — as a record inside the transcript (no buttons) and as the actionable prompt above the composer. If that looks redundant in the smoke test, drop the in-transcript render for pending (keep answered records) — one-line change in `Transcript.tsx`.

- [ ] **Step 3: Verify everything still builds and tests pass**

Run: `cd ui/frontend && npm test && npm run build`
Expected: all green, `dist/` builds.

- [ ] **Step 4: Commit**

```bash
git add ui/frontend/src
git commit -m "ui: app shell — sessions, header, inspector, socket wiring"
```

---

### Task 17: Full verification and PR preparation

**Files:** none new (fixes only, if verification finds problems)

- [ ] **Step 1: Run every suite**

```bash
uv run pytest                                          # harness suite: green, ui NOT collected
UI_TESTS=1 uv run --group ui pytest ui/server/tests -v # backend suite: green
cd ui/frontend && npm test && npm run build            # frontend suite + build: green
```

Expected: all pass. Fix anything red before proceeding.

- [ ] **Step 2: Manual smoke (requires `~/.codex/auth.json`)**

```bash
uv run --group ui python -m ui.server --workspace /tmp/smoke --port 8000
# (create /tmp/smoke first: mkdir -p /tmp/smoke)
cd ui/frontend && npm run dev   # second shell; open http://localhost:5173
```

Checklist (degraded mode — no token streaming until seam 1 lands):
1. Create a session; send "create hello.txt containing hi, then read it back".
2. Tool cards for `write_file`/`read_file` appear live; permission prompt appears for `write_file`; answer Yes.
3. Reply text appears at turn end (whole message — expected in degraded mode).
4. Open the inspector; click the assistant bubble → raw message dict; click the answered permission record → "(ephemeral…)"; system prompt tab shows the real prompt.
5. Refresh the page mid-turn → snapshot restores the transcript; a pending permission re-appears.
6. Send a message, hit Cancel during tool activity → "turn cancelled" notice, transcript rolled back.
7. `curl -s localhost:8000/api/sessions` → session listed with updated timestamp.

- [ ] **Step 3: Rebase and push**

```bash
git fetch origin && git rebase origin/main
uv run pytest && UI_TESTS=1 uv run --group ui pytest ui/server/tests
git push -u origin ui/scaffold
```

- [ ] **Step 4: Open the PR (do not merge — yc's gate)**

```bash
gh pr create --title "ui: web UI v1 — chat client + inspector over the harness seams" --body "$(cat <<'EOF'
Implements docs/streams/ui/2026-07-06-web-ui-design.md (spec) via
docs/streams/ui/2026-07-06-web-ui-plan.md.

- FastAPI backend (ui/server): TurnRunner thread bridge over run_turn,
  one WebSocket per session, typed events, permission bridge, cancel,
  pop-back rollback, in-memory session store behind the seam-2 surface.
- React/Vite frontend (ui/frontend): event-sourced transcript, tool
  cards, permission prompts, inspector pane, reconnecting socket.
- Degraded mode until the seam requests land (streaming, session logs):
  text arrives at turn_done; sessions are in-memory.

**Shared-file hunk for routing:** pyproject.toml + uv.lock gain a `ui`
dependency group (fastapi, uvicorn). Flagged per the ownership map.

Verification: root `uv run pytest` green and does NOT collect ui tests;
`UI_TESTS=1 uv run --group ui pytest ui/server/tests` green;
`npm test` + `npm run build` green; manual smoke checklist in the plan
completed against the real adapter.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR opens against main; CI (`test`) runs the harness suite only. Report the PR URL and the shared-file hunk to yc.

---

## Plan Self-Review (completed at write time)

- **Spec coverage:** event vocabulary (Task 2 + 10), TurnRunner mechanics incl. permission bridge/cancel/rollback (5–7), subagent flat rendering (8), REST + WS + snapshot/reconnect (9–10), real wiring + localhost binding (11), frontend reducer one-code-path claim (13), reconnect/backoff (14), tool cards/permission UX/composer restore-on-error (15–16), inspector incl. ephemeral teaching moment (16), testing strategy (throughout), degraded mode (5, 7, 13, 17), out-of-scope list respected (nothing here builds auth/markdown/multi-client). Compaction: event + reducer + divider covered; the server wires `on_compact` and passes main.py's threshold — `turn_done` carries the full list precisely so compaction can't desync the client.
- **Type consistency:** `try_begin`/`run_turn_blocking`/`answer_permission`/`cancel` names match across Tasks 5–10; event payload keys match events.py ↔ types/events.ts ↔ reducer; `SessionMeta`/`Session` REST shape matches store ↔ app ↔ sidebar.
- **Known judgment calls encoded:** registry-copy-before-agent-insert (Task 8), single-writer socket discipline (Task 10), pop-back rollback not index slicing (Task 5), ui suite kept out of the root run via env-guarded conftest (Task 1).
