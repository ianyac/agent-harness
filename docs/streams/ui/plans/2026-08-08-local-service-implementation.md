# Local Harness Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the authenticated local Python service that exposes harness sessions, streaming turns, permissions, plan review, cancellation, recovery, and safety state to both the browser and Tauri clients.

**Architecture:** A standalone FastAPI project under `ui/` imports only public harness seams. Each open session owns one `HarnessRuntime`, one worker lane, one authoritative `SessionLog`, and one current WebSocket generation. Synchronous harness callbacks cross into the async server through a thread-safe event sink and bounded decision brokers.

**Tech Stack:** Python 3.14, FastAPI, Uvicorn, Pydantic 2, HTTPX, standard-library `asyncio`, `sqlite3`, and `threading`, pytest 9, FastAPI TestClient

## Global Constraints

- Work only in `ui/` and `docs/streams/ui/`; do not modify `harness/`, `main.py`, the root dependency files, or `.github/`.
- Bind runtime HTTP only to `127.0.0.1` and authenticate every REST and WebSocket request with a per-launch secret.
- Keep `.agent/sessions/*.jsonl`, `.context-mode`, `.folds.sqlite3`, and `.fold-decisions.jsonl` authoritative.
- Support `default`, `acceptAll`, and `readOnly` as base modes; `plan` is per-turn only.
- Do not execute hook, MCP, or skill shell commands in v1. Non-command skills remain usable; command blocks render as not run.
- Use harness public seams and documented session artifacts only. Write a seam request instead of importing a private helper.
- Keep all automated tests offline with scripted fake model responses and the vendored tiktoken cache.
- Every implementation task follows red → green → focused commit.

## File Structure

```text
ui/
├── pyproject.toml                  # standalone service dependencies and pytest config
├── uv.lock                         # locked Python environment
├── README.md                       # local service commands and supported capabilities
├── server/
│   ├── __init__.py
│   ├── __main__.py                 # CLI, launch secret, bootstrap URL, Uvicorn
│   ├── _paths.py                   # development import path and tiktoken cache
│   ├── app.py                      # FastAPI factory and route composition
│   ├── auth.py                     # launch authentication and bootstrap cookie
│   ├── protocol.py                 # typed client/server event vocabulary
│   ├── metadata.py                 # SQLite session index and preferences
│   ├── context_mode.py             # documented context-mode artifact rules
│   ├── registry.py                 # public-seam tool and prompt assembly
│   ├── runtime.py                  # HarnessRuntime lifecycle and safety snapshot
│   ├── bridge.py                   # event sink, decisions, cancellation, rollback
│   ├── runner.py                   # one synchronous turn behind the async boundary
│   ├── sessions.py                 # SessionManager and per-session locks
│   └── static.py                   # built frontend discovery and static fallback
└── tests/
    ├── conftest.py
    ├── fake_llm.py
    ├── test_imports.py
    ├── test_protocol.py
    ├── test_metadata.py
    ├── test_auth.py
    ├── test_context_mode.py
    ├── test_registry.py
    ├── test_runtime.py
    ├── test_bridge.py
    ├── test_runner.py
    ├── test_app_rest.py
    ├── test_app_ws.py
    └── test_cli.py
```

---

### Task 1: Scaffold the standalone service project

**Files:**
- Create: `ui/pyproject.toml`
- Create: `ui/server/__init__.py`
- Create: `ui/server/_paths.py`
- Create: `ui/tests/__init__.py`
- Create: `ui/tests/conftest.py`
- Create: `ui/tests/test_imports.py`
- Create: `ui/README.md`
- Generate: `ui/uv.lock`
- Create: `docs/streams/ui/2026-08-08-root-pytest-scope-request.md`

**Interfaces:**
- Consumes: repository root containing `harness/` and `vendor/tiktoken/`
- Produces: importable `server` package; offline pytest environment; `ui/.venv`

- [ ] **Step 1: Write the root pytest ownership request and failing import test**

The mailbox note asks the owner of the shared root `pyproject.toml` to add:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

Explain that the UI owns an independent `ui/tests` suite with different
dependencies, so unscoped root collection imports a project the root
environment does not install. Do not edit the root file from the UI lane.

```python
# ui/tests/test_imports.py
def test_server_bootstrap_imports_harness():
    import server._paths  # noqa: F401
    from harness.loop import run_turn

    assert callable(run_turn)
```

- [ ] **Step 2: Run the import test and verify the standalone project is absent**

Run: `cd ui && uv run pytest tests/test_imports.py -v`

Expected: FAIL because `ui/pyproject.toml` and the `server` package do not exist.

- [ ] **Step 3: Add the standalone project and bootstrap**

Create `ui/pyproject.toml` with Python `>=3.14`, runtime dependencies
`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `platformdirs`, and the
existing harness dependencies `tiktoken` and `requests`. Add a `dev` dependency
group containing `pytest`, `pytest-asyncio`, and `pytest-cov`. Set pytest's
`testpaths = ["tests"]` and `pythonpath = ["."]`.

```python
# ui/server/_paths.py
import os
import sys
from pathlib import Path

UI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = UI_ROOT.parent

if (REPO_ROOT / "harness").is_dir() and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR", str(REPO_ROOT / "vendor" / "tiktoken")
)
```

Import `server._paths` first from `ui/server/__init__.py`. Mirror the cache
setup in `ui/tests/conftest.py` before test collection imports harness modules.
Document `uv sync` and `uv run pytest` in `ui/README.md`.

- [ ] **Step 4: Lock dependencies and run the import test**

Run: `cd ui && uv sync && uv run pytest tests/test_imports.py -v`

Expected: PASS with one test and no network access during test execution.

- [ ] **Step 5: Commit the standalone scaffold**

```bash
git add ui/pyproject.toml ui/uv.lock ui/README.md ui/server ui/tests docs/streams/ui/2026-08-08-root-pytest-scope-request.md
git commit -m "ui: scaffold local service project"
```

### Task 2: Request the stream-reset harness seam

**Files:**
- Create: `docs/streams/ui/2026-08-08-seam-stream-reset.md`

**Interfaces:**
- Consumes: current public `LLMClient.complete(...)`, `CodexAdapter.complete(...)`, and `run_turn(...)`
- Produces: requested optional `on_stream_reset: Callable[[], None] | None` callback that Task 7 consumes

- [ ] **Step 1: Write the exact seam contract**

The note must request all of these guarantees:

```text
1. LLMClient.complete, CodexAdapter.complete, and run_turn accept the optional
   on_stream_reset callback.
2. CodexAdapter invokes it immediately before a retry attempt begins, but only
   if the abandoned attempt delivered at least one non-empty text delta.
3. Callback exceptions propagate, matching on_text_delta cancellation behavior.
4. No reset fires for a first attempt, a retry before any text, compaction, or
   a new tool/model iteration inside one run_turn call.
5. FakeLLM can script a reset so downstream protocol tests remain offline.
6. Omitting the callback preserves every existing caller.
```

Include a UI signature probe that the harness lane can use as an acceptance
test:

```python
import inspect

from harness.loop import run_turn

assert "on_stream_reset" in inspect.signature(run_turn).parameters
```

- [ ] **Step 2: Verify the note has no implementation outside the UI mailbox**

Run: `git diff --check -- docs/streams/ui/2026-08-08-seam-stream-reset.md`

Expected: exit 0; the diff contains one mailbox document and no `harness/` edit.

- [ ] **Step 3: Commit the seam request**

```bash
git add docs/streams/ui/2026-08-08-seam-stream-reset.md
git commit -m "ui: request stream reset callback"
```

Task 7 may proceed on a branch only after this seam is available. Do not
subclass `CodexAdapter`, call `_attempt`, or copy retry logic into `ui/`.

### Task 3: Define the typed event protocol

**Files:**
- Create: `ui/server/protocol.py`
- Create: `ui/tests/test_protocol.py`

**Interfaces:**
- Consumes: JSON text frames
- Produces: `ClientEvent`, `ServerEvent`, `parse_client_event(raw: str)`, and `dump_server_event(event: ServerEvent)`

- [ ] **Step 1: Write failing protocol tests**

```python
# ui/tests/test_protocol.py
import pytest
from pydantic import ValidationError

from server.protocol import PermissionAnswer, UserMessage, parse_client_event


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
```

- [ ] **Step 2: Run the protocol tests and verify failure**

Run: `cd ui && uv run pytest tests/test_protocol.py -v`

Expected: FAIL because `server.protocol` does not exist.

- [ ] **Step 3: Implement the protocol models**

Use Pydantic models with `extra="forbid"`, non-empty ids, and discriminated
`type` literals. Define client models for `send_message`, `queue_message`,
`cancel_turn`, `answer_permission`, `answer_plan`, `set_session_mode`, and
`clear_queued_message`. `UserMessage.mode` and `QueuedMessage.mode` accept
`"base" | "plan"`; base mode changes use the three constructible modes.

Define server models for the complete vocabulary in the design spec. Every
incremental server event inherits this envelope:

```python
class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1)
    turn_id: str | None = None
```

Represent `session_snapshot.messages` as `list[dict]` because the harness's
plain message dictionaries are the contract. Represent activity payloads with
`activity_id`, `parent_activity_id`, `actor`, `name`, `args`, `result`,
`is_error`, `started_at`, and `duration_ms`. Parse client events with one
module-level `TypeAdapter` over an `Annotated[Union[...], Field(discriminator="type")]`.

- [ ] **Step 4: Run the protocol tests**

Run: `cd ui && uv run pytest tests/test_protocol.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the protocol**

```bash
git add ui/server/protocol.py ui/tests/test_protocol.py
git commit -m "ui: define local service protocol"
```

### Task 4: Persist session metadata without duplicating transcripts

**Files:**
- Create: `ui/server/metadata.py`
- Create: `ui/tests/test_metadata.py`

**Interfaces:**
- Consumes: `MetadataStore(path: Path)` and workspace/session facts
- Produces: `SessionRecord`, `PreferenceRecord`, and transactional CRUD methods

- [ ] **Step 1: Write failing metadata tests**

```python
# ui/tests/test_metadata.py
from pathlib import Path

from server.metadata import MetadataStore, NewSession


def test_session_rows_round_trip_without_message_content(tmp_path: Path):
    store = MetadataStore(tmp_path / "ui.sqlite3")
    created = store.create_session(
        NewSession(
            session_id="s1",
            workspace=tmp_path / "project",
            title="Streaming retries",
            mode="default",
            context_mode="folding",
        )
    )
    assert store.get_session("s1") == created
    assert "message" not in store.raw_session_columns()


def test_archived_sessions_are_excluded_unless_requested(tmp_path: Path):
    store = MetadataStore(tmp_path / "ui.sqlite3")
    store.create_session(NewSession.defaults("s1", tmp_path))
    store.archive_session("s1")
    assert store.list_sessions() == []
    assert [row.session_id for row in store.list_sessions(include_archived=True)] == ["s1"]
```

- [ ] **Step 2: Run the metadata tests and verify failure**

Run: `cd ui && uv run pytest tests/test_metadata.py -v`

Expected: FAIL because `server.metadata` does not exist.

- [ ] **Step 3: Implement the SQLite store**

Use `sqlite3.connect(path)`, `PRAGMA journal_mode=WAL`, foreign keys, explicit
transactions, and a schema-version table. The `sessions` table contains only
`session_id`, canonical `workspace`, `title`, `mode`, `context_mode`,
`created_at`, `updated_at`, `last_opened_at`, and `archived_at`. The
`preferences` table is a key/value JSON store.

Normalize workspace paths with `Path.resolve()`. Validate base mode against
`harness.permissions.STARTUP_MODES` and context mode against
`("compaction", "folding")`. Provide `create_session`, `get_session`,
`list_sessions`, `rename_session`, `touch_session`, `archive_session`,
`upsert_discovered_session`, `get_preference`, and `set_preference`.

- [ ] **Step 4: Run metadata tests**

Run: `cd ui && uv run pytest tests/test_metadata.py -v`

Expected: PASS, including reopen persistence in a second `MetadataStore`.

- [ ] **Step 5: Commit metadata persistence**

```bash
git add ui/server/metadata.py ui/tests/test_metadata.py
git commit -m "ui: persist rebuildable session metadata"
```

### Task 5: Implement launch authentication and browser bootstrap

**Files:**
- Create: `ui/server/auth.py`
- Create: `ui/tests/test_auth.py`

**Interfaces:**
- Consumes: `LaunchAuth(secret: str, allowed_origins: set[str])`
- Produces: `require_http(request)`, `require_websocket(websocket)`, and one-use `bootstrap_response(token)`

- [ ] **Step 1: Write failing authentication tests**

```python
# ui/tests/test_auth.py
import pytest
from starlette.requests import Request

from server.auth import LaunchAuth


def test_bootstrap_token_is_single_use():
    auth = LaunchAuth("secret", {"http://127.0.0.1:8000"})
    first = auth.consume_bootstrap("secret")
    second = auth.consume_bootstrap("secret")
    assert first is True
    assert second is False


def test_tokens_are_compared_without_plain_equality(monkeypatch):
    seen = []
    monkeypatch.setattr("server.auth.hmac.compare_digest", lambda a, b: seen.append((a, b)) or True)
    assert LaunchAuth("secret", set()).matches("candidate")
    assert seen == [("secret", "candidate")]
```

- [ ] **Step 2: Run the authentication tests and verify failure**

Run: `cd ui && uv run pytest tests/test_auth.py -v`

Expected: FAIL because `server.auth` does not exist.

- [ ] **Step 3: Implement authentication**

Use `hmac.compare_digest`, cookie name `harness_ui_session`,
`HttpOnly`, `SameSite=strict`, and no persistent expiry. Accept either
`Authorization: Bearer <secret>` for Tauri or the cookie for the browser. For
the Tauri WebSocket, accept the URL-safe secret only as the second
`Sec-WebSocket-Protocol` value after `harness-ui`; echo `harness-ui` as the
selected protocol and never echo the secret. Reject missing or unexpected
`Origin` values for unsafe REST methods and all WebSocket upgrades. Allow
origins are the concrete loopback server origin and `tauri://localhost`.

`consume_bootstrap` must atomically transition from unused to used under a
`threading.Lock`. Never log the token or include it in exception text.

- [ ] **Step 4: Run authentication tests**

Run: `cd ui && uv run pytest tests/test_auth.py -v`

Expected: PASS.

- [ ] **Step 5: Commit launch authentication**

```bash
git add ui/server/auth.py ui/tests/test_auth.py
git commit -m "ui: authenticate local service clients"
```

### Task 6: Implement context-mode artifacts and runtime registry assembly

**Files:**
- Create: `ui/server/context_mode.py`
- Create: `ui/server/registry.py`
- Create: `ui/server/runtime.py`
- Create: `ui/tests/test_context_mode.py`
- Create: `ui/tests/test_registry.py`
- Create: `ui/tests/test_runtime.py`

**Interfaces:**
- Consumes: `RuntimeConfig`, `LLMClient`, workspace path, session path
- Produces: `HarnessRuntime`, `build_registry(runtime)`, `build_system(runtime)`, `SafetySnapshot`

- [ ] **Step 1: Write failing context-mode and registry tests**

```python
# ui/tests/test_context_mode.py
from pathlib import Path
import pytest

from server.context_mode import prepare_context_mode


def test_folding_session_cannot_resume_without_ledger(tmp_path: Path):
    session = tmp_path / "s.jsonl"
    session.touch()
    session.with_suffix(".context-mode").write_text("folding\n")
    with pytest.raises(ValueError, match="ledger is missing"):
        prepare_context_mode(session, requested=None, resuming=True)
```

```python
# ui/tests/test_registry.py
from server.registry import build_registry


def test_v1_registry_excludes_hooks_mcp_and_skill_shell(tmp_path, runtime_parts):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "plain.md").write_text(
        "---\nname: plain\ndescription: plain\n---\nRead this guidance."
    )
    (tmp_path / "skills" / "command.md").write_text(
        "---\nname: command\ndescription: command\n---\n!`git status`"
    )
    tools, skills = build_registry(workspace=tmp_path, **runtime_parts)
    assert "skill" in tools
    assert tools["skill"].execute(name="command") == (
        "[skill command not run: this agent cannot run shell commands]"
    )
    assert {skill.name for skill in skills} == {"command", "plain"}
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `cd ui && uv run pytest tests/test_context_mode.py tests/test_registry.py tests/test_runtime.py -v`

Expected: FAIL because the runtime modules do not exist.

- [ ] **Step 3: Implement context-mode preparation**

Mirror the documented CLI invariants in one focused module: `.context-mode`
accepts only `compaction` or `folding`; an existing fold ledger implies folding
for legacy sessions; folding requires its ledger on resume; a compacted session
cannot switch to folding; folding and a compact threshold are mutually
exclusive. Return a `PreparedContext` containing `mode`, `compact_threshold`,
and an optional open `FoldingContext`. Give it an idempotent `close()`.

- [ ] **Step 4: Implement registry and runtime assembly**

Create `RuntimeConfig(session_id, workspace, mode, context_mode,
compact_threshold)` and `HarnessRuntime`. Acquire `harness.session.lock` before
loading and always release it from `close()`.

Build filesystem, bash, `web_fetch`, optional `web_search`, folding tools,
`agent`, non-executing `skill`, and `exit_plan_mode` from their public
factories. Reuse `PermissionPolicy`, `SandboxPolicy`, `default_sandbox`,
`discover`, `skills_section`, `build_system_prompt`, and `SessionLog`.

`exit_plan_mode` receives a runtime callback with this exact signature:

```python
PlanReviewer = Callable[[str], tuple[bool, str]]
```

It is bound later by the turn runner. Build the safety snapshot by calling
`policy.decide(tool)` for each registered tool and reporting backend class,
workspace write boundary, read breadth, and network policy. Label `web_fetch`
and `web_search` as network egress even though they are read-only.

- [ ] **Step 5: Run runtime tests**

Run: `cd ui && uv run pytest tests/test_context_mode.py tests/test_registry.py tests/test_runtime.py -v`

Expected: PASS for new sessions, resume, mode validation, registry membership,
skill command suppression, lock release, and exact safety decisions.

- [ ] **Step 6: Commit runtime assembly**

```bash
git add ui/server/context_mode.py ui/server/registry.py ui/server/runtime.py ui/tests/test_context_mode.py ui/tests/test_registry.py ui/tests/test_runtime.py
git commit -m "ui: assemble harness session runtime"
```

### Task 7: Bridge synchronous turns into typed async events

**Files:**
- Create: `ui/server/bridge.py`
- Create: `ui/server/runner.py`
- Create: `ui/tests/fake_llm.py`
- Create: `ui/tests/test_bridge.py`
- Create: `ui/tests/test_runner.py`

**Interfaces:**
- Consumes: `HarnessRuntime`, merged `run_turn(..., on_stream_reset=...)`, client decision messages
- Produces: `EventSink`, `DecisionBroker`, `CancellationToken`, `TurnRunner.run(...)`

- [ ] **Step 1: Write failing bridge tests**

```python
# ui/tests/test_bridge.py
import asyncio
import pytest

from server.bridge import CancellationToken, DecisionBroker, EventSink, TurnCancelled


def test_cancel_token_raises_only_after_request():
    token = CancellationToken()
    token.check()
    token.cancel()
    with pytest.raises(TurnCancelled):
        token.check()


@pytest.mark.asyncio
async def test_event_sink_numbers_events_in_one_generation():
    sink = EventSink(session_id="s1", generation=3, loop=asyncio.get_running_loop())
    sink.emit("turn_started", turn_id="t1")
    sink.emit("assistant_delta", turn_id="t1", text="hi")
    assert (await sink.next()).sequence == 1
    assert (await sink.next()).sequence == 2
```

Add runner tests proving streamed text, tool activity, permission blocking,
plan-review blocking, `always` allowlisting, stream reset, cancellation rollback,
and authoritative `turn_completed.messages`.

- [ ] **Step 2: Run bridge and runner tests and verify failure**

Run: `cd ui && uv run pytest tests/test_bridge.py tests/test_runner.py -v`

Expected: FAIL because the bridge and runner do not exist.

- [ ] **Step 3: Implement the thread-safe bridge primitives**

`EventSink.emit()` must call `loop.call_soon_threadsafe(queue.put_nowait,
event)`. `DecisionBroker` owns one pending request id and a `queue.Queue` of
size one; mismatched or duplicate answers return `False`. `disconnect()`
resolves a pending permission as `"no"` and a pending plan as `(False, "")`.

`CancellationToken` wraps a `threading.Event`. `CancellableLLM.complete()`
checks before the call, inside every text delta, inside every stream reset, and
after the call. The reset callback emits `stream_reset` before forwarding.

```python
def rollback_to_boundary(messages: list[dict]) -> int:
    dropped = 0
    while messages and not (
        messages[-1]["role"] == "assistant"
        and not messages[-1].get("tool_calls")
    ):
        messages.pop()
        dropped += 1
    return dropped
```

- [ ] **Step 4: Implement `TurnRunner`**

`TurnRunner.run(text, mode, turn_id, sink, token)` runs under the runtime's
single-turn lock. It sets `policy.mode` to `plan` only for this turn, restores
the base mode after completion/rejection, records the boundary, and calls
`run_turn` with callable system prompt, compaction/folding, tool callback,
permission asker, plan reviewer, text delta, stream reset, and compaction
callbacks.

Wrap tools once per runtime to emit `activity_completed` with duration and the
same error string `_run_one_call` will return. Use a `contextvars.ContextVar`
for current activity parent so nested subagent tool events retain parentage.
On success, record the turn and emit the complete authoritative message list.
On `TurnCancelled` or any other exception, rollback, record any completed
boundary the log lacks, and emit `turn_cancelled` or categorized `turn_failed`.

- [ ] **Step 5: Run bridge and runner tests**

Run: `cd ui && uv run pytest tests/test_bridge.py tests/test_runner.py -v`

Expected: PASS. The permission and plan tests must prove the worker is blocked
before the answer and unblocked only by the matching request id.

- [ ] **Step 6: Commit the turn bridge**

```bash
git add ui/server/bridge.py ui/server/runner.py ui/tests/fake_llm.py ui/tests/test_bridge.py ui/tests/test_runner.py
git commit -m "ui: stream harness turns through event bridge"
```

### Task 8: Build the session manager and REST API

**Files:**
- Create: `ui/server/sessions.py`
- Create: `ui/server/app.py`
- Create: `ui/tests/test_app_rest.py`

**Interfaces:**
- Consumes: `MetadataStore`, `HarnessRuntime`, `LaunchAuth`
- Produces: `SessionManager`, `create_app(settings, llm_factory)`, authenticated REST routes

- [ ] **Step 1: Write failing REST tests**

```python
# ui/tests/test_app_rest.py
def test_create_list_load_rename_and_archive_session(client, workspace):
    created = client.post(
        "/api/sessions",
        json={
            "workspace": str(workspace),
            "mode": "default",
            "context_mode": "compaction",
            "title": "New chat",
        },
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    assert client.get("/api/sessions").json()[0]["session_id"] == session_id
    assert client.patch(f"/api/sessions/{session_id}", json={"title": "Retry work"}).status_code == 200
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get("/api/sessions").json() == []
```

Also test missing credentials, invalid workspace, unknown session ids, path
traversal in ids, folding-resume errors, health, config, transcript, safety,
and unauthenticated requests.

- [ ] **Step 2: Run REST tests and verify failure**

Run: `cd ui && uv run pytest tests/test_app_rest.py -v`

Expected: FAIL because the session manager and app factory do not exist.

- [ ] **Step 3: Implement `SessionManager`**

Generate ids as UTC timestamp plus random suffix, validate with
`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, and construct the file only as
`workspace / ".agent" / "sessions" / f"{session_id}.jsonl"`. Hold open
runtimes in a dictionary protected by an async lock. `close()` closes every
runtime and the metadata connection.

Scan only known workspaces from metadata plus the configured base workspace.
Ignore fold decision JSONL files. Load transcript messages only through
`SessionLog.load()` while holding the session lock.

- [ ] **Step 4: Implement the authenticated REST routes**

Provide `/api/health`, `/api/config`, `/api/sessions`, `/api/sessions/{id}`,
`/api/sessions/{id}/transcript`, and `/api/sessions/{id}/safety`. Creation
returns 201; archive returns 204. Missing Codex credentials return a typed 409
`credential_prerequisite` response containing the command `codex login`, never
token content.

Use FastAPI lifespan to initialize and close the manager. Apply auth through a
router dependency except to the one-use bootstrap route. `/api/health` also
requires the launch credential.

- [ ] **Step 5: Run REST tests**

Run: `cd ui && uv run pytest tests/test_app_rest.py -v`

Expected: PASS.

- [ ] **Step 6: Commit session REST support**

```bash
git add ui/server/sessions.py ui/server/app.py ui/tests/test_app_rest.py
git commit -m "ui: expose local session REST API"
```

### Task 9: Add the reconnecting WebSocket turn endpoint

**Files:**
- Modify: `ui/server/app.py`
- Modify: `ui/server/sessions.py`
- Create: `ui/tests/test_app_ws.py`

**Interfaces:**
- Consumes: validated `ClientEvent`, `TurnRunner`, session connection generation
- Produces: `WS /ws/sessions/{session_id}` with snapshot-first ordered events

- [ ] **Step 1: Write failing WebSocket tests**

```python
# ui/tests/test_app_ws.py
def test_websocket_sends_snapshot_before_turn_events(client, session_id):
    with client.websocket_connect(f"/ws/sessions/{session_id}") as ws:
        assert ws.receive_json()["type"] == "session_snapshot"
        ws.send_json({"type": "send_message", "text": "hello", "mode": "base"})
        assert ws.receive_json()["type"] == "turn_started"
        assert ws.receive_json()["type"] == "assistant_delta"
        done = ws.receive_json()
        assert done["type"] == "turn_completed"
        assert done["messages"][-1]["content"] == "hello back"
```

Add tests for duplicate connection supersession, stale generation events,
permission answer ids, plan approval and feedback, cancellation, queued
follow-up, invalid client frames, disconnect during permission, and rejecting a
second simultaneous turn.

- [ ] **Step 2: Run WebSocket tests and verify failure**

Run: `cd ui && uv run pytest tests/test_app_ws.py -v`

Expected: FAIL because the route does not exist.

- [ ] **Step 3: Implement connection ownership**

Increment the session generation on every accepted socket. Mark the previous
connection superseded, resolve its pending decisions conservatively, and stop
its sender. The new socket receives `session_snapshot` before the receiver may
start a new turn. Event sequence numbers restart at one within the new
generation.

Run sender and receiver coroutines in an `asyncio.TaskGroup`. Dispatch client
events by exact type. A turn runs with `asyncio.to_thread`; do not block the
event loop. Keep one queued follow-up in session state and launch it only after
the current turn emits a terminal event.

- [ ] **Step 4: Run WebSocket tests**

Run: `cd ui && uv run pytest tests/test_app_ws.py -v`

Expected: PASS, including no leaked worker or pending broker after disconnect.

- [ ] **Step 5: Commit WebSocket turns**

```bash
git add ui/server/app.py ui/server/sessions.py ui/tests/test_app_ws.py
git commit -m "ui: add reconnecting session WebSocket"
```

### Task 10: Add browser startup, static serving, and backend acceptance tests

**Files:**
- Create: `ui/server/static.py`
- Create: `ui/server/__main__.py`
- Modify: `ui/server/app.py`
- Modify: `ui/README.md`
- Create: `ui/tests/test_cli.py`
- Modify: `ui/tests/test_app_rest.py`
- Modify: `ui/tests/test_app_ws.py`

**Interfaces:**
- Consumes: built frontend directory `ui/frontend/dist`, launch secret on stdin or generated by CLI
- Produces: `uv run python -m server --workspace PATH --port 0`, one-time browser URL, static SPA fallback

- [ ] **Step 1: Write failing CLI and static tests**

```python
# ui/tests/test_cli.py
def test_ready_record_never_contains_launch_secret(tmp_path, run_server):
    process, ready = run_server(tmp_path, secret="not-for-stdout")
    assert ready["type"] == "server-ready"
    assert ready["host"] == "127.0.0.1"
    assert ready["port"] > 0
    assert "not-for-stdout" not in process.stdout_text
```

Add tests that `/bootstrap?token=...` sets the cookie and redirects, a second
bootstrap fails, unknown frontend routes return `index.html`, missing `dist`
returns a clear development response, and SIGTERM closes session locks.

- [ ] **Step 2: Run CLI tests and verify failure**

Run: `cd ui && uv run pytest tests/test_cli.py -v`

Expected: FAIL because the CLI and static module do not exist.

- [ ] **Step 3: Implement CLI and static serving**

`python -m server` accepts `--workspace`, `--host` fixed to loopback, `--port`
defaulting to zero, `--metadata-db`, and optional `--secret-stdin`. Browser mode
generates a 32-byte `secrets.token_urlsafe` secret and prints a complete
one-time bootstrap URL. Sidecar mode reads one JSON bootstrap line from stdin
and prints only this readiness record:

```json
{"type":"server-ready","host":"127.0.0.1","port":49152}
```

Resolve static assets from `ui/frontend/dist` during development and from a
PyInstaller resource root when frozen. Do not mask `/api` or `/ws` 404s with
the SPA fallback.

- [ ] **Step 4: Run the complete service suite**

Run: `cd ui && uv run pytest -v`

Expected: PASS for all service tests offline.

- [ ] **Step 5: Run the root harness suite**

Run: `uv run pytest tests`

Expected: 526 tests pass with no root source changes. After the owner-routed
pytest-scope change lands, `uv run pytest` must produce the same result.

- [ ] **Step 6: Commit the browser entrypoint**

```bash
git add ui/server/static.py ui/server/__main__.py ui/server/app.py ui/README.md ui/tests
git commit -m "ui: finish authenticated local service"
```

## Plan 1 Completion Gate

Before starting the web-client plan:

1. `cd ui && uv run pytest -v` passes offline.
2. The owner-routed root pytest scope is merged, and `uv run pytest` excludes
   the standalone UI suite while still passing all root tests.
3. A fake streamed turn emits snapshot → start → deltas/activity → complete.
4. Permission and plan-review tests prove matching-id blocking behavior.
5. Reconnect replaces ephemeral state with authoritative messages.
6. The merged harness exposes `on_stream_reset`; no private retry API is used.
7. `git status --short` is clean.
