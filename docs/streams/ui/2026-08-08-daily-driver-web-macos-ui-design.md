# Agent Harness Daily-Driver UI — Web and macOS

**Date:** 2026-08-08
**Status:** Approved design; awaiting written-spec review
**Stream:** UI
**Target:** Local web application and one-click macOS application

## Purpose

Build a polished daily-driver interface for the agent harness. The product
should feel like a calm, capable conversation tool rather than a debugger.
Harness internals remain available, but tool calls, permissions, subagents,
context handling, and safety details appear only when they help the user make a
decision or understand the result.

The same React application ships in two forms:

- a local web UI served by the Python application service; and
- a Tauri macOS app that bundles, launches, monitors, and stops that service.

The first release is local-only. Remote connections, accounts, and team
collaboration are deliberately excluded.

## Product decisions

| Decision | Choice | Rationale |
|---|---|---|
| Primary use | Polished daily driver | Optimize for repeated real work, not for demonstrating every harness mechanism. |
| Visual direction | Focused conversation | Keep the transcript dominant; move operational detail into compact activity cards and an on-demand inspector. |
| Surfaces | Local web and Tauri macOS | Serve browser users and provide a one-click native experience without maintaining two product UIs. |
| Frontend | One React + TypeScript application | Interaction, accessibility, and visual behavior stay identical across surfaces. |
| macOS runtime | Bundled Python sidecar | No separate Python or CLI installation is required. |
| Desktop host | Tauri 2 | Supplies native lifecycle, menus, dialogs, notifications, and a signed application bundle. |
| Python packaging | PyInstaller external binary | Produces a self-contained sidecar that Tauri can bundle per target architecture. |
| Backend | FastAPI service over loopback | REST fits metadata and setup; WebSocket fits streaming turns and permission round-trips. |
| Deployment | Local-only in v1 | Avoid remote identity, tenancy, remote workspace access, and server administration until the local product is excellent. |
| macOS distribution | Direct-download, signed, and notarized | Arbitrary workspace access and a managed child process do not fit a Mac App Store sandbox in v1. |
| Transcript authority | Existing harness session artifacts | The UI never creates a second source of truth for messages or folding state. |
| Harness integration | Public seams only | The UI consumes `run_turn`, callbacks, tools, permissions, sessions, folding, and subagents without owning a forked agent loop. |

Tauri documents Python API servers packaged with PyInstaller as a normal
sidecar use case, and PyInstaller supports the project's Python 3.14 runtime:
[Tauri sidecars](https://v2.tauri.app/develop/sidecar/) and
[PyInstaller changelog](https://pyinstaller.org/en/latest/CHANGES.html).

## Scope

### In scope for v1

1. First-run workspace selection and a clear local-only trust explanation.
2. Create, rename, search, resume, and archive sessions.
3. Stream assistant text and reconcile it with the authoritative completed turn.
4. Render Markdown, code blocks, diffs, and copyable command output.
5. Show tool and subagent work as compact inline activity cards.
6. Open a resizable inspector for full arguments, output, timing, and policy reasoning.
7. Resolve permission requests inline with Allow, Deny, and Always.
8. Expose `default`, `acceptAll`, and `readOnly` startup modes without offering
   runtime-only plan mode as a session base mode.
9. Enter plan mode per turn through the composer mode control or `/plan`.
10. Support the harness's built-in filesystem, bash, agent, non-command skill,
    folding, and web tools when their underlying capability is available.
11. Stop an active turn at the earliest safe boundary and retry a failed or
    cancelled turn.
12. Preserve an editable draft while the agent works and queue at most one
    follow-up message behind the active turn.
13. Resume after browser disconnects and recover once from a sidecar crash.
14. Surface context mode, context usage, sandbox state, and tool permissions
    in the inspector without making them permanent dashboard furniture.
15. Follow system light/dark appearance, keyboard navigation, reduced motion,
    and screen-reader requirements.
16. Ship the Python service as a signed Tauri sidecar for one-click macOS use.

### Deferred

- Remote access, accounts, synchronization, and collaboration.
- Mobile or tablet layouts beyond a functional narrow browser fallback.
- Hooks and MCP configuration or command-approval flows. Existing workspace
  configurations are reported as unavailable in v1 rather than silently
  ignored or executed.
- Automatic application updates.
- Multiple simultaneous turns in one session.
- More than one queued follow-up message.
- Plugin marketplace or skill-management UI.
- A standalone observability dashboard.

## Experience principles

### Conversation is primary

Assistant prose, code, and user messages occupy the visual center. Routine
reads, searches, and tests collapse into a single activity summary such as
“Worked for 34s · 5 actions · 18 tests passed.” The transcript remains readable
even when the agent performs many operations.

### State is explicit but quiet

The interface always communicates whether the agent is ready, streaming,
acting, waiting for permission, stopping, retrying, or complete. It uses short
status text and restrained motion instead of persistent spinners or noisy logs.

### Decisions stay in context

A permission request appears immediately after the activity that caused it.
The card explains the requested action, scope, and policy reason. Focus moves to
the card, and returns to the composer after the decision.

### Detail is one gesture away

Selecting an activity opens a right-side inspector. The drawer is resizable and
may be pinned for the current session, but starts closed. Closing it returns to
the same scroll position and focused transcript item.

### Recovery preserves trust

The product never pretends that an incomplete turn succeeded. It rolls back to
the last valid harness message boundary, preserves the user's draft, and offers
a concrete next action such as Retry, Reconnect, or Reopen session.

## Information architecture

### Session sidebar

The left sidebar contains:

- product identity and connection state;
- a primary New chat action;
- sessions grouped by recency;
- session search through the command palette;
- workspace identity on every session row;
- archive and rename actions in a contextual menu; and
- Settings at the bottom.

At wide desktop widths it is 224 px. It can collapse to a 56 px rail and does
so automatically below 1,000 px. The collapsed rail retains New chat, search,
recent session access, connection state, and settings.

### Conversation header

The header shows the workspace, current branch when detectable, permission
mode, and an Activity button. It does not show token or sandbox metrics until
the user opens Activity.

### Transcript

The transcript has a maximum readable width of 820 px. User messages use a
compact dark bubble; assistant messages use the page background and an avatar
anchor. Code and diffs can exceed the prose measure through horizontal scroll
or an expanded artifact view.

Tool calls are grouped into one activity card until one of these events occurs:

- the agent produces user-facing prose;
- permission is required;
- a tool fails;
- a subagent starts or completes; or
- the turn completes.

This grouping avoids a wall of near-identical tool rows while keeping event
order recoverable in the inspector.

### Composer

The composer remains visible and editable while work is in progress. It
contains:

- a multi-line text area;
- context attachment control;
- per-turn mode control, including plan mode;
- command and skill discovery;
- Send while idle and Stop while active; and
- a queued-state affordance when one follow-up is waiting.

`Command+Enter` sends. `Escape` first closes transient UI, then focuses Stop
when a turn is active. Destructive or permission-granting shortcuts work only
while their target control visibly holds focus.

### Activity inspector

The inspector opens from an activity card or the header. It contains:

- chronological model, tool, subagent, permission, compaction, and folding events;
- structured arguments and complete, copyable results;
- duration and success/failure state;
- permission decision and reason;
- context usage and active context-management mode;
- active sandbox backend and write boundary; and
- registered tools with computed allow/ask/deny decisions.

The inspector is a secondary surface, not a second navigation system. It does
not own session creation, message sending, or settings.

### Settings and first run

First run asks the user to choose a workspace, explains that processing and
session files stay local, and verifies application-service readiness. The
default permission mode is `default`.

Settings covers appearance, default mode, default context-management choice
(standard compaction or recoverable folding, never both), notification
behavior, keyboard shortcuts, and local data locations. Workspace
trust is explicit. V1 never executes hook, MCP, or skill shell commands merely
because a workspace contains configuration files. Skills that contain shell
command blocks remain discoverable, but those blocks are rendered as not run
until a future approval flow exists.

The service uses the harness's existing Codex credential file at
`~/.codex/auth.json`. First run checks for it without copying its tokens into UI
storage. If it is absent or invalid, the app presents a stable sign-in
prerequisite screen with the exact `codex login` instruction and a Retry check;
implementing an authentication provider inside the UI is outside v1.

## Visual system

The interface uses a warm neutral palette rather than terminal black or bright
developer-tool blue:

- page: `#FAFAF7` light and `#171813` dark;
- sidebar: `#EEEEE8` light and `#20211C` dark;
- primary ink: `#292B24` light and `#F2F2EC` dark;
- secondary ink: `#6E7067` light and `#A9ABA1` dark;
- operational success: `#5B8D68`;
- permission attention: `#C58B35`;
- failure: `#B95D55`; and
- focus/accent: an accessible green-gray derived from the system appearance.

Use the system UI stack led by `-apple-system` on macOS and the platform system
font in browsers. Code and tool details use the platform monospace stack. Body
text is 14 px with a 1.55 line height. Corners range from 8 px for controls to
16 px for conversation surfaces; shadows are reserved for floating layers and
the composer.

Motion is 120–180 ms and communicates state or spatial continuity. Streaming
text itself does not animate. Reduced-motion mode removes drawer transitions,
pulses, and scroll interpolation.

## Platform architecture

```text
┌──────────────────────┐       ┌────────────────────────┐
│ Local browser        │──────▶│                        │
│ React application    │ HTTP  │ Local Python service   │
└──────────────────────┘ + WS  │                        │
                               │ Session manager        │
┌──────────────────────┐       │ Turn/thread bridge     │
│ Tauri macOS app      │──────▶│ Permission bridge      │
│ same React build     │       │ Safety snapshot        │
│ native lifecycle     │       │ Harness adapter        │
└──────────────────────┘       └───────────┬────────────┘
                                           │ public seams
                               ┌───────────▼────────────┐
                               │ agent-harness          │
                               │ sessions + fold state  │
                               │ workspace + sandbox    │
                               └────────────────────────┘
```

### Shared frontend

The React application owns presentation and ephemeral interaction state. A
single reducer folds protocol events into the visible transcript and activity
timeline. The reducer never edits the authoritative harness messages sent in a
turn-completed snapshot.

The browser and Tauri builds differ only at a small platform adapter:

- browser workspace selection uses a validated absolute-path field plus recent
  workspaces, while macOS uses a native Tauri folder dialog;
- native menus and notifications are no-ops in the browser; and
- sidecar status comes from the web health endpoint or the Tauri host.

### Local Python service

The service is a standalone project under `ui/`. It imports the harness from
the repository during development and packages the harness modules into the
sidecar for release.

Its responsibilities are:

- construct the same public harness components as `main.py`;
- hold one runtime object and lock per open session;
- run synchronous `run_turn()` work in a worker thread;
- bridge callbacks into typed WebSocket events through an async queue;
- block a worker on a permission request until the browser answers or the
  request is denied by disconnect/timeout;
- wrap registered tools to capture results without adding a harness-private
  dependency;
- load and record authoritative session artifacts;
- expose computed permission and sandbox state; and
- reject concurrent turns for the same session.

### Tauri host

The Tauri host performs the minimum native work:

1. Generate a random per-launch secret.
2. Spawn the target-specific PyInstaller sidecar through Tauri's external
   binary mechanism.
3. Let the sidecar bind `127.0.0.1` on an OS-assigned port.
4. Send the secret to the child over its inherited stdin pipe, then read one
   readiness record containing the port from sidecar stdout.
5. Give the webview the origin and secret through a narrow Tauri command; keep
   both in memory.
6. Reveal the main window only after the health check succeeds.
7. Monitor the child, restart it once after an unexpected exit, and restore the
   active session.
8. Terminate and await the child during normal application shutdown.

The application builds a sidecar for each macOS target architecture rather than
depending on a system Python. Code signing and notarization cover both the
Tauri bundle and its nested sidecar.

### Browser startup

The local web command starts the same Python service and prints a one-time URL.
Opening that URL exchanges a bootstrap secret for an HttpOnly, SameSite cookie
and redirects to a clean local URL. The service binds only to loopback.

## Data ownership

The following existing harness files remain authoritative:

- `.agent/sessions/<id>.jsonl` for messages;
- `<id>.context-mode` for context-management ownership;
- `<id>.folds.sqlite3` for the folding ledger; and
- `<id>.fold-decisions.jsonl` for folding decisions.

The UI service stores a small metadata database in the platform user data
directory. Session index rows are rebuildable from known workspace session
directories; user preferences and window state are local conveniences. The
database contains:

- session id and workspace path;
- display title and archive timestamp;
- created, last-opened, and last-message timestamps;
- selected permission mode and context configuration;
- last active session per window; and
- user preferences and window state.

It does not duplicate message content, tool results, fold content, credentials,
or workspace files. Missing index rows are rebuilt by scanning known workspace
session directories.

## Turn protocol and data flow

One WebSocket exists for each open or running session, with one current
connection generation per session. Every connection receives a snapshot before
incremental events. Every event includes a session id, connection generation,
turn id when relevant, and monotonically increasing sequence number. The client
discards events from superseded connections or older sequence numbers.

### Client to service

- `send_message {text, mode}` — start a turn when idle.
- `queue_message {text, mode}` — store one follow-up behind the active turn.
- `cancel_turn {turn_id}` — request cancellation at the next safe boundary.
- `answer_permission {request_id, answer}` — `yes`, `no`, or `always`.
- `set_session_mode {mode}` — change the constructible base permission mode.
- `clear_queued_message` — return the queued follow-up text to the editable draft.

### Service to client

- `session_snapshot` — authoritative messages, runtime state, and safety state.
- `turn_started` — establishes the turn boundary and active mode.
- `assistant_delta` — append streamed text for the current model attempt.
- `stream_reset` — discard stale deltas when a provider retry starts.
- `activity_started` / `activity_completed` — tool or subagent lifecycle.
- `permission_requested` / `permission_resolved` — blocking gate round-trip.
- `context_updated` — compaction, folding, or token-usage change.
- `turn_stopping` — cancellation accepted but waiting for a safe boundary.
- `turn_completed` — authoritative messages and final assistant content.
- `turn_cancelled` — rollback completed; session is ready.
- `turn_failed` — rollback completed with a user-facing error category.
- `safety_updated` — mode, allowlist, tools, or sandbox state changed.

### Normal turn

1. The client optimistically appends the user message and sends `send_message`.
2. The service records the message boundary, emits `turn_started`, and runs
   `run_turn()` in the session's worker lane.
3. Text callbacks emit `assistant_delta`. Tool wrappers emit activity events.
4. If the permission gate asks, the worker blocks on a bounded queue while the
   inline card receives focus. No terminal prompt is involved.
5. `run_turn()` completes. The service records the authoritative messages and
   emits `turn_completed`.
6. The client replaces ephemeral turn content with the completed snapshot,
   focuses the composer, and starts the queued follow-up if one exists.

## Interaction state machine

```text
ready → streaming ↔ acting → waiting_permission → acting → complete → ready
  │         │          │              │
  │         └──────────┴──── cancel_requested ──▶ stopping ──▶ ready
  └───────────────────────── failure/reconnect ──────────────▶ ready
```

- The composer remains editable in all states.
- Send becomes Stop while a turn is active.
- A follow-up may be queued once; further sends edit that queued message.
- Cancellation is immediate during model streaming. During a blocking tool it
  becomes `stopping` and completes at the first boundary the harness can safely
  observe.
- A permission request blocks only the active turn. The draft and application
  navigation remain available.
- Changing sessions does not cancel a turn; its sidebar row continues to show
  state. The app notifies the user when the background turn needs permission or
  completes.

## Error handling and recovery

### Provider retry

On a transient provider failure, show “Retrying connection” inside the current
activity. Emit `stream_reset` before the next attempt so text from the abandoned
attempt cannot remain visible. The final assembled response is authoritative.

### Tool error

Keep the real result in the activity timeline, mark the failing action, and let
the harness continue reasoning from that result. Do not convert a tool failure
into an application-wide error banner.

### Turn failure

Rollback to the last completed message boundary, emit `turn_failed`, preserve
the draft, and offer Retry. Persist only the rolled-back valid transcript.

### Permission without a live client

If no current WebSocket owns the session, deny the permission request. A stale
or mismatched `request_id` cannot unblock another request.

### Browser disconnect

The active worker may complete, but permission requests default to denial.
Reconnection begins with `session_snapshot`, which replaces the ephemeral
client transcript before new incremental events are accepted.

### Sidecar crash

Tauri restarts the sidecar once, reconnects, and reopens the last session from
authoritative files. A second crash presents a stable recovery screen with
Restart service, Open logs, and Quit. The app never enters an infinite restart
loop.

### Invalid or missing workspace

Do not silently choose another directory. Keep the session visible but disabled,
explain the missing path, and offer Locate workspace or Archive session.

## Security model

- Bind the service to `127.0.0.1` only.
- Authenticate every REST and WebSocket request with a per-launch secret.
- Permit only the expected local web and Tauri origins.
- Exchange browser bootstrap secrets for an HttpOnly cookie, then remove them
  from the visible URL.
- Keep the Tauri secret in Rust/webview memory, not local storage or logs.
- Apply the existing permission policy and sandbox to every harness tool.
- Display the real sandbox backend and write boundary; never label `NoSandbox`
  as protected.
- Label network-capable read-only tools as network egress in permission and
  inspection copy.
- Treat hook, MCP, and skill shell commands as disabled in v1 unless a future
  explicit approval flow is designed. Non-command skills remain available.
- Redact known credentials from UI logs and diagnostics without altering the
  authoritative harness redaction rules.

## Accessibility and keyboard behavior

- All functionality is reachable without a pointer.
- Focus order follows sidebar → header → transcript → composer → inspector.
- New inline permission cards receive focus and announce their reason through a
  live region; focus returns to its previous logical target after resolution.
- Streaming text does not repeatedly trigger screen-reader announcements. A
  concise completion announcement fires at turn end.
- Color never carries allow/ask/deny or success/failure meaning by itself.
- Visible focus contrast meets WCAG AA in light and dark appearances.
- Respect system reduced-motion and increased-contrast preferences.
- Minimum interactive target is 32 px on desktop, with 44 px for primary
  actions in the narrow browser layout.

Primary shortcuts:

| Shortcut | Action |
|---|---|
| `Command+N` | New chat |
| `Command+K` | Open command palette |
| `Command+Enter` | Send while the composer is focused |
| `Escape` | Close transient UI, then focus Stop when active |
| `Command+Shift+I` | Toggle activity inspector |
| `Command+F` | Search the current conversation |

Permission shortcuts are scoped to the focused permission card and are shown
next to their buttons; there are no hidden global permission shortcuts.

## Repository boundaries

All implementation lives in the UI lane:

```text
ui/
├── frontend/              # React, TypeScript, Vite, design system
│   └── src/
│       ├── components/
│       ├── features/
│       ├── platform/
│       └── protocol/
├── server/                # FastAPI service and harness adapter
├── desktop/               # Tauri host
│   └── src-tauri/
├── packaging/             # PyInstaller spec and sidecar build scripts
├── tests/                 # Python integration and protocol tests
├── pyproject.toml         # standalone Python project
└── package.json           # frontend and desktop workspace
```

The UI does not modify `harness/` or `main.py` to self-serve a missing seam. A
required harness change is written as a contract request in
`docs/streams/ui/` and routed through the human. Root dependency and CI changes
are likewise routed to their owning stream.

## Testing strategy

### Python service

- A scripted fake `LLMClient` drives complete turns without network access.
- Contract tests pin each WebSocket event schema and ordering rule.
- Integration tests cover streaming, tool grouping, permission blocking,
  Always allowlisting, cancellation, rollback, reconnect, session resume,
  context-mode persistence, and safety snapshots.
- Tests prove one active turn per session and deny stale permission answers.

### React application

- Reducer tests cover every event type, retry reset, stale generation, duplicate
  sequence, authoritative snapshot replacement, and queued follow-up behavior.
- Component tests cover composer state, permission focus, activity grouping,
  inspector disclosure, error recovery, and session navigation.
- Accessibility tests cover names, roles, focus restoration, live regions,
  contrast tokens, and reduced motion.
- Visual regression covers the primary screen, streaming, permission, tool
  failure, inspector, reconnect, first run, light, and dark appearance.

### End to end

- Browser tests run a fake turn through REST and WebSocket from first message to
  completion.
- Disconnect and reconnect tests prove the completed snapshot self-heals stale
  UI state.
- Permission tests prove the turn blocks until the matching visible request is
  answered.
- Keyboard tests cover new chat, send, stop, command palette, inspector, and
  permission resolution.

### Tauri and packaging

- A packaged smoke test starts the sidecar, waits for readiness, opens the
  window, creates a session, and shuts down without leaving a child process.
- Crash recovery is tested for one successful restart and one stable failure.
- Release verification checks the nested sidecar architecture, code signature,
  notarization, and absence of a dependency on system Python.

## Acceptance criteria

The v1 design is complete when all of the following are true:

1. A macOS user with existing Codex credentials can open the signed app, select
   a workspace, and send a message without installing or starting any runtime
   dependency.
2. A browser user can start the local service, open its one-time URL, and use
   the same product experience.
3. Assistant text streams smoothly and reconciles exactly to the completed
   harness message.
4. Routine agent activity does not dominate the transcript, while every action
   and complete result remains available in the inspector.
5. Permission requests never depend on terminal input and clearly explain the
   action, scope, and reason.
6. The user can keep typing, queue one follow-up, stop safely, and retry without
   losing the draft.
7. Browser reconnect and one sidecar restart recover from authoritative session
   files without duplicating a turn.
8. Permission, sandbox, network-egress, and context-management state are honest
   and traceable.
9. Keyboard-only and reduced-motion workflows can complete the full core turn.
10. The root harness test suite remains green and the UI test suites pass
    offline with fake model responses.

## Design artifacts

The approved brainstorming companion contains:

- three interaction directions, with Focused conversation selected;
- the approved core daily-driver screen;
- the approved web/Tauri platform architecture; and
- the approved interaction and recovery state design.

These mockups are exploratory references. This document is authoritative when
copy, behavior, or architecture differs from a visual artifact.
