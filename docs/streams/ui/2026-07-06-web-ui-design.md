# Web UI for the Agent Harness — Design

**Date:** 2026-07-06
**Stream:** ui
**Status:** Approved in session; pending yc review of this document
**Baseline:** main @ `148edf2` (lesson 12)

## Goal

A browser UI for the harness: a chat client first, whose transcript pane
doubles as a transcript inspector. Type a message, watch the agent's text
stream in token by token, see every tool call and result inline, answer
permission prompts in the page — and open any transcript item to see the
actual message dict underneath, because the transcript is the state.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Purpose | Chat client + inspector, chat first | One rendering path serves both; inspection depth grows over time |
| Stack | FastAPI backend + React/Vite/TypeScript frontend | Backend must be Python (imports `harness`); industry-standard frontend; connects to the parked TS thread |
| Liveness | Token streaming from day one | Requires seam request 1 (below); UI runs degraded (whole messages) until it lands |
| Sessions | Persistent, reconstructed from harness-written logs | Requires seam request 2 (below); harness owns transcript persistence, UI reads it |
| Transport | One WebSocket per session, typed events | Bidirectional: deltas/tool events down, user input + permission answers up |
| Frontend state | `reduce(events)`, no state library | Live view and log-reconstructed view share one code path; the reducer is the inspector's foundation |
| Component libs | None (plain React + CSS) | Reviewability; matches the repo's no-framework ethos |

## Architecture

Three pieces. The React app in the browser; a FastAPI backend that imports
`harness` directly and plays the role `main.py` plays for the CLI; the
harness itself, untouched. The backend is the only harness consumer. All
harness activity becomes typed events pushed down the session's WebSocket;
the client sends exactly three message types up: `user_message`,
`permission_answer`, `cancel`.

The UI touches only public seams: `run_turn` (including its callbacks
`on_tool_call`, `asker`, `on_compact`), the `LLMClient` protocol, the
`Tool` registry it constructs itself, `PermissionPolicy`/`MODES`,
`build_system_prompt`/`Environment`, and `default_sandbox`. Tool results
need no seam: the backend builds the tool registry, so it wraps each
`Tool.execute` to observe results.

### Repo layout

```
ui/
  server/            # Python (FastAPI) — the harness driver
    app.py           # app factory, routes, WS endpoint
    events.py        # typed event schema (the system's shared vocabulary)
    runner.py        # TurnRunner: thread bridge around blocking run_turn
    store.py         # SessionStore protocol + in-memory stand-in until seam 2
    tests/
  frontend/          # Vite + React + TS (own package.json, own .gitignore)
    src/
```

### Event vocabulary

Defined once in `events.py`, mirrored as a TS discriminated union.

Server → client:

| Event | Payload | Source |
|---|---|---|
| `session_snapshot` | messages list (including the running turn's appends so far), turn-running flag, pending permission request if any, accumulated in-flight delta text | sent on connect/reconnect |
| `turn_started` | — | runner |
| `text_delta` | text chunk | seam 1 (`on_text_delta`) |
| `tool_call` | name, args | `on_tool_call` |
| `tool_result` | name, result text | UI-side `Tool.execute` wrapper |
| `permission_request` | id, tool name, args | `asker` bridge |
| `compaction` | count of messages summarized | `on_compact` |
| `turn_done` | this turn's messages slice (authoritative dicts) | runner |
| `turn_cancelled` | — | runner |
| `turn_error` | error message | runner |

There is deliberately no `assistant_message` event: no existing seam fires
per assistant reply mid-turn. Instead, deltas accumulate into an open
assistant bubble and **any non-delta event closes it**; at `turn_done` the
client reconciles its optimistic live items against the authoritative
message dicts in the payload (which is also what makes the inspector
truthful).

Client → server: `user_message` (text), `permission_answer` (id,
yes/no/always), `cancel`.

## Backend

**TurnRunner** owns one session's execution. `run_turn` is blocking, so each
turn runs on a worker thread:

- Callbacks (`on_text_delta`, `on_tool_call`, wrapped `execute`, `asker`,
  `on_compact`) run on the worker thread and push events into the session's
  queue via `loop.call_soon_threadsafe`; an async task drains the queue to
  the WebSocket.
- `asker` creates a `concurrent.futures.Future`, emits `permission_request`,
  and blocks until the WS handler resolves it with the browser's answer.
  "always" passes through unchanged — the harness's `session_allowlist`
  owns the semantics.
- Tool registry, `--workspace`, and `--mode` flags mirror `main.py`. The
  permission mode is fixed at server start in v1.
- One turn at a time per session; `user_message` during a running turn is
  rejected with `turn_error` (transcript untouched).
- **Cancellation:** `cancel` sets a flag; the next callback invocation on the
  worker thread raises `TurnCancelled`; the runner catches it, rolls
  `messages` back to the turn-start index (main.py's KeyboardInterrupt
  pattern), and emits `turn_cancelled`. Known limitation: cancellation
  latency is bounded by the longest gap between callbacks — sub-second
  while text streams, but a long bash call cannot be interrupted
  mid-execution.
- **Disconnect mid-turn:** the turn keeps running. A pending
  `permission_request` stays pending; reconnect gets a `session_snapshot`
  that includes it.
- Subagent activity (lesson 12 `agent` tool) renders flat in v1: one
  `tool_call`/`tool_result` pair like any other tool.

## Frontend

Vite + React + TypeScript. State is `useReducer` over the event stream.
The reducer handles `session_snapshot` by mapping message dicts through the
same transcript-item constructors the live events use — reconstruction and
live view are one code path, not two renderers.

```
┌──────────┬──────────────────────────────┬─────────────┐
│ sessions │  transcript                  │  inspector  │
│          │   ├ user bubble              │  (toggle)   │
│ + new    │   ├ tool card ▸ args/result  │  raw msg    │
│          │   ├ assistant (streams in)   │  dicts,     │
│          │   └ permission prompt        │  system     │
│ mode ●   │  [composer…            send] │  prompt     │
└──────────┴──────────────────────────────┴─────────────┘
```

- **Tool cards** inline in the transcript, collapsed to `name(args…)`,
  expandable to full args and result text.
- **Permission prompts** appear inline where the turn paused, with
  yes / no / always-for-this-tool buttons; answered prompts remain visible
  as a record of the decision.
- **Inspector pane** (toggleable): select any transcript item to see the
  underlying message dict; a second tab shows the current system prompt.
- **Compaction** renders as a divider in the transcript ("N messages
  compacted").
- Header shows permission mode and workspace path (read-only in v1).

## Seam requests (routed through yc to the harness stream)

Contract drafts live as separate mailbox notes; summaries:

1. **Token streaming** (`2026-07-06-seam-token-streaming.md`):
   `LLMClient.complete()` and `run_turn()` gain optional
   `on_text_delta: Callable[[str], None]` (default `None` = today's
   behavior). The Codex adapter already consumes SSE; it surfaces
   `response.output_text.delta` through the callback. Contract test:
   concatenated deltas == final `content`.
2. **Harness-owned session logs** (`2026-07-06-seam-session-store.md`):
   sessions with ids under a configurable root; a completed turn's messages
   appended durably (JSONL) **per turn, atomically** — a crashed or
   cancelled turn is simply absent, matching rollback semantics. Operations:
   create session, list sessions (id, created/updated), load messages.

**Definition of done for v1 includes both seams wired in.** During
development the UI runs degraded behind its own interfaces — assistant text
appears only at `turn_done` instead of streaming, sessions live in an
in-memory `SessionStore` — so all other work proceeds in parallel with the
seam PRs, but v1 doesn't ship until streaming and log-backed sessions work
end to end.

## Error handling

- **LLM failure mid-turn** (retries exhausted, auth): roll messages back to
  turn start — the in-memory transcript always matches the per-turn-atomic
  log — and emit `turn_error`. UI shows a dismissible banner; the user's
  input stays in the composer for retry.
- **WS drop:** client reconnects with backoff; `session_snapshot` on
  reconnect rebuilds state (no event-gap guessing).
- **Malformed client messages:** logged and ignored; never crash the socket.
- **Server restart mid-turn:** the turn dies with the process; the log holds
  only completed turns, so the session reloads cleanly.

## Testing (offline, per repo rule)

- **Backend** (`pytest`, `ui/server/tests/`): a scripted `FakeLLM`
  implementing the `LLMClient` protocol (UI-owned — `tests/` is harness
  lane) drives TurnRunner end-to-end: event order and payloads, permission
  round-trip (yes/no/always), cancellation rollback, error rollback, delta
  forwarding, reject-while-running. WS endpoint via FastAPI `TestClient`.
- **Frontend** (Vitest): reducer tests are the value-dense target — event
  sequences in, state shape out, replay-equals-live. Component tests only
  for the permission prompt flow. No browser-automation suite in v1.
- **Manual smoke** before PR: real adapter, one streamed reply, one
  permission prompt answered in the browser.

## Dependencies and cross-lane routing

| Change | Lane | Routing |
|---|---|---|
| `ui` dependency-group (fastapi, uvicorn, ws test deps) in root `pyproject.toml` + `uv.lock` | shared, no owner | ui PR carries the hunk; PR description flags it for yc's routing call |
| Seam 1 + 2 | harness | mailbox notes, yc routes |
| CI jobs for ui server + frontend | overseer (`.github/`) | requested via this doc; until then, local runs reported in PR descriptions. Root `uv run pytest` stays green and untouched |

## Out of scope for v1

Multi-client on one session; token counts/cost display; editing past
messages; rich tool-output rendering (markdown/images); auth (server binds
localhost only); mobile layout; nested subagent transcript rendering.
