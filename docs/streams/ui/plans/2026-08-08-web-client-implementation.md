# Focused Web Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the polished focused-conversation React client that drives the local harness service in a browser and serves unchanged inside Tauri.

**Architecture:** A typed protocol layer feeds a single transcript reducer. Feature folders own sessions, conversation, activity, permissions, plan review, and the inspector; a narrow platform adapter is the only browser/Tauri branch. CSS tokens and accessible unstyled primitives create the approved calm daily-driver interface.

**Tech Stack:** React, TypeScript, Vite, Vitest, Testing Library, Playwright, Radix UI primitives, Lucide React, React Markdown, remark-gfm, plain CSS modules

## Global Constraints

- Begin only after the local-service completion gate passes.
- Use one frontend build for browser and Tauri; platform differences live under `src/platform/`.
- Preserve the focused-conversation hierarchy: 224 px collapsible session sidebar, 820 px transcript measure, inspector closed by default.
- Keep activity compact inline and complete in the inspector.
- Keep the composer editable while a turn runs and allow exactly one queued follow-up.
- Permission and plan-review controls are inline, focus-managed, and never global modals.
- Support system light/dark, reduced motion, increased contrast, keyboard-only use, and screen readers.
- Use Lucide icons; do not substitute emoji, text symbols, custom SVG drawings, or CSS artwork for product icons.
- Keep network and model behavior out of component tests; use protocol fixtures and the fake local service.
- Every implementation task follows red → green → focused commit.

## File Structure

```text
ui/frontend/
├── index.html
├── package.json
├── package-lock.json
├── tsconfig.json
├── vite.config.ts
├── playwright.config.ts
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── app.css
    ├── test/setup.ts
    ├── styles/tokens.css
    ├── styles/global.css
    ├── protocol/types.ts
    ├── protocol/parse.ts
    ├── protocol/reducer.ts
    ├── protocol/fixtures.ts
    ├── platform/types.ts
    ├── platform/browser.ts
    ├── platform/tauri.ts
    ├── platform/index.ts
    ├── api/http.ts
    ├── api/sessionSocket.ts
    ├── features/sessions/
    ├── features/conversation/
    ├── features/activity/
    ├── features/permissions/
    ├── features/plan-review/
    ├── features/inspector/
    ├── features/onboarding/
    ├── features/settings/
    └── components/
```

`ui/frontend/e2e/` contains Playwright journeys. Co-locate unit tests beside
the module they cover using `*.test.ts` and `*.test.tsx`.

---

### Task 1: Scaffold the frontend and approved visual tokens

**Files:**
- Create: `ui/frontend/package.json`
- Create: `ui/frontend/package-lock.json`
- Create: `ui/frontend/index.html`
- Create: `ui/frontend/tsconfig.json`
- Create: `ui/frontend/vite.config.ts`
- Create: `ui/frontend/src/main.tsx`
- Create: `ui/frontend/src/App.tsx`
- Create: `ui/frontend/src/test/setup.ts`
- Create: `ui/frontend/src/styles/tokens.css`
- Create: `ui/frontend/src/styles/global.css`
- Create: `ui/frontend/src/app.css`
- Create: `ui/frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: browser root element and Vite environment
- Produces: `App`, design tokens, `npm run test`, `npm run build`, `npm run e2e`

- [ ] **Step 1: Create the package manifest and failing app-shell test**

Use scripts `dev`, `build`, `test`, `test:watch`, `e2e`, and `typecheck`. Runtime
dependencies are `react`, `react-dom`, `react-markdown`, `remark-gfm`,
`lucide-react`, `@tauri-apps/api`, and Radix dialog, dropdown-menu, popover,
scroll-area, and tooltip packages. Development dependencies are Vite, the React plugin,
TypeScript, Vitest, jsdom, Testing Library, jest-dom, user-event, axe-core, and
Playwright.

```tsx
// ui/frontend/src/App.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("App", () => {
  it("renders the focused product shell", () => {
    render(<App />);
    expect(screen.getByRole("navigation", { name: "Sessions" })).toBeVisible();
    expect(screen.getByRole("main")).toBeVisible();
  });
});
```

- [ ] **Step 2: Install dependencies and verify the shell test fails**

Run: `cd ui/frontend && npm install && npm test -- --run src/App.test.tsx`

Expected: FAIL because `App` does not render the named regions.

- [ ] **Step 3: Implement the minimal semantic shell and visual tokens**

Define the approved colors, 8/10/12/16 px radii, 224/56 px sidebar widths,
820 px transcript measure, 120–180 ms motion, system font stacks, focus ring,
light/dark color schemes, reduced-motion overrides, and increased-contrast
overrides as CSS custom properties.

`App` renders `<nav aria-label="Sessions">`, `<header>`, `<main>`, and an
inspector `<aside>` that is absent while closed. Use `min-height: 100dvh`; do
not lock layout to `100vh`.

- [ ] **Step 4: Run unit tests, typecheck, and production build**

Run: `cd ui/frontend && npm test -- --run && npm run typecheck && npm run build`

Expected: all commands exit 0 and Vite emits `dist/`.

- [ ] **Step 5: Commit the frontend scaffold**

```bash
git add ui/frontend
git commit -m "ui: scaffold focused React client"
```

### Task 2: Mirror the protocol and implement the transcript reducer

**Files:**
- Create: `ui/frontend/src/protocol/types.ts`
- Create: `ui/frontend/src/protocol/parse.ts`
- Create: `ui/frontend/src/protocol/reducer.ts`
- Create: `ui/frontend/src/protocol/fixtures.ts`
- Create: `ui/frontend/src/protocol/reducer.test.ts`
- Create: `ui/frontend/src/protocol/parse.test.ts`

**Interfaces:**
- Consumes: local-service JSON events
- Produces: `ServerEvent`, `ClientEvent`, `TranscriptState`, `transcriptReducer`, `parseServerEvent`

- [ ] **Step 1: Write failing reducer tests**

```ts
// ui/frontend/src/protocol/reducer.test.ts
import { describe, expect, it } from "vitest";

import { emptyTranscript, transcriptReducer } from "./reducer";
import { event } from "./fixtures";

describe("transcriptReducer", () => {
  it("resets stale streamed text before a provider retry", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { turn_id: "t1" }));
    state = transcriptReducer(state, event("assistant_delta", { turn_id: "t1", text: "stale" }));
    state = transcriptReducer(state, event("stream_reset", { turn_id: "t1" }));
    state = transcriptReducer(state, event("assistant_delta", { turn_id: "t1", text: "fresh" }));
    expect(state.streamingText).toBe("fresh");
  });

  it("replaces ephemera with authoritative completed messages", () => {
    const state = transcriptReducer(
      emptyTranscript(),
      event("turn_completed", {
        turn_id: "t1",
        messages: [{ role: "assistant", content: "authoritative" }],
      }),
    );
    expect(state.messages).toEqual([{ role: "assistant", content: "authoritative" }]);
    expect(state.streamingText).toBe("");
  });
});
```

Also test stale generation rejection, duplicate sequence rejection, activity
parentage, permission and plan-review state, queue state, cancellation, failure,
safety updates, and snapshot replacement.

- [ ] **Step 2: Run protocol tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/protocol`

Expected: FAIL because the protocol modules do not exist.

- [ ] **Step 3: Implement exact discriminated unions and reducer invariants**

Mirror every field from `ui/server/protocol.py`. `parseServerEvent` validates
plain objects, required envelope fields, allowed discriminants, and bounded
primitive types before casting. It returns `{ok: false, error}` instead of
throwing into React.

```ts
export type TranscriptState = {
  generation: number;
  lastSequence: number;
  messages: HarnessMessage[];
  streamingText: string;
  activities: Record<string, ActivityItem>;
  activityOrder: string[];
  permission: PermissionRequest | null;
  planReview: PlanReviewRequest | null;
  running: boolean;
  stopping: boolean;
  queued: QueuedMessage | null;
  safety: SafetySnapshot | null;
  error: RecoverableError | null;
};
```

Reject an event unless its generation equals the current snapshot generation
and its sequence is greater than `lastSequence`. A newer snapshot resets both.

- [ ] **Step 4: Run protocol tests and typecheck**

Run: `cd ui/frontend && npm test -- --run src/protocol && npm run typecheck`

Expected: PASS.

- [ ] **Step 5: Commit protocol state**

```bash
git add ui/frontend/src/protocol
git commit -m "ui: reduce typed session events"
```

### Task 3: Add HTTP, WebSocket, and platform adapters

**Files:**
- Create: `ui/frontend/src/platform/types.ts`
- Create: `ui/frontend/src/platform/browser.ts`
- Create: `ui/frontend/src/platform/tauri.ts`
- Create: `ui/frontend/src/platform/index.ts`
- Create: `ui/frontend/src/api/http.ts`
- Create: `ui/frontend/src/api/sessionSocket.ts`
- Create: `ui/frontend/src/api/sessionSocket.test.ts`

**Interfaces:**
- Consumes: local service REST/WS, optional `window.__TAURI_INTERNALS__`
- Produces: `PlatformAdapter`, `ApiClient`, `SessionSocket`

- [ ] **Step 1: Write failing reconnection tests with a fake socket**

```ts
// ui/frontend/src/api/sessionSocket.test.ts
it("ignores events from a socket closed before a replacement connects", () => {
  const first = sockets.connect("s1");
  const second = sockets.connect("s1");
  first.receive(snapshot({ generation: 1 }));
  second.receive(snapshot({ generation: 2 }));
  first.receive(delta({ generation: 1, sequence: 2, text: "stale" }));
  expect(store.getState().streamingText).toBe("");
});
```

Also test exponential reconnect capped at 10 seconds, no reconnect after
explicit close, outbound buffering only until the snapshot arrives, and
protocol errors surfaced as recoverable UI errors.

- [ ] **Step 2: Run adapter tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/api`

Expected: FAIL because the adapters do not exist.

- [ ] **Step 3: Implement the platform boundary**

```ts
export interface PlatformAdapter {
  kind: "browser" | "tauri";
  getServiceConnection(): Promise<{ baseUrl: string; token?: string }>;
  chooseWorkspace(): Promise<string | null>;
  notify(input: { title: string; body: string }): Promise<void>;
  revealPath(path: string): Promise<void>;
}
```

Browser `chooseWorkspace` returns `null`; the onboarding UI uses a validated
path field and recents. Tauri methods invoke only the narrow commands defined
in Plan 3. Detect Tauri once in `platform/index.ts`; no feature component may
read Tauri globals.

`ApiClient` adds bearer auth only when the adapter returns a token; browser
requests rely on the HttpOnly cookie. `SessionSocket` converts `http/https` to
`ws/wss`, calls `new WebSocket(url, ["harness-ui", token])` in Tauri, and owns
reconnect timers and disposal.

- [ ] **Step 4: Run API tests and typecheck**

Run: `cd ui/frontend && npm test -- --run src/api src/platform && npm run typecheck`

Expected: PASS.

- [ ] **Step 5: Commit client transport**

```bash
git add ui/frontend/src/api ui/frontend/src/platform
git commit -m "ui: connect shared client to local service"
```

### Task 4: Build session navigation and the command palette

**Files:**
- Create: `ui/frontend/src/features/sessions/SessionSidebar.tsx`
- Create: `ui/frontend/src/features/sessions/SessionRow.tsx`
- Create: `ui/frontend/src/features/sessions/ConversationHeader.tsx`
- Create: `ui/frontend/src/features/sessions/useSessions.ts`
- Create: `ui/frontend/src/features/sessions/sessionSidebar.module.css`
- Create: `ui/frontend/src/components/CommandPalette.tsx`
- Create: `ui/frontend/src/components/CommandPalette.test.tsx`
- Modify: `ui/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `ApiClient`, active session id, runtime state per session
- Produces: session create/select/rename/archive/search, base-mode change, and `Command+N`/`Command+K`

- [ ] **Step 1: Write failing navigation tests**

Test that sessions group into Today, Yesterday, and Earlier; workspace appears
in each accessible row name; archived sessions disappear; `Command+N` creates;
`Command+K` opens search; active background turns retain their status; and the
sidebar collapses without removing New chat or search. Test that the header
shows workspace/branch/base mode, sends `set_session_mode`, and requires an
explicit confirmation before switching to `acceptAll`.

- [ ] **Step 2: Run navigation tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/features/sessions src/components/CommandPalette.test.tsx`

Expected: FAIL because the components do not exist.

- [ ] **Step 3: Implement session navigation**

Use semantic `<nav aria-label="Sessions">` and buttons, not clickable `<div>`
elements. Put rename/archive in Radix DropdownMenu. Preserve active-session
selection by id, never list index. Store manual sidebar collapse in settings;
apply automatic narrow collapse through CSS without overwriting that preference.

The command palette searches session title and workspace, and offers New chat,
Open settings, Toggle activity, and per-session navigation. Every result has a
stable command id and visible shortcut where one exists.

`ConversationHeader` exposes `default`, `acceptAll`, and `readOnly` only. Its
`acceptAll` confirmation says that mutating tools will run without per-call
prompts for this session. Plan mode does not appear in this base-mode menu.

- [ ] **Step 4: Run navigation tests and build**

Run: `cd ui/frontend && npm test -- --run src/features/sessions src/components/CommandPalette.test.tsx && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit session navigation**

```bash
git add ui/frontend/src/features/sessions ui/frontend/src/components/CommandPalette.tsx ui/frontend/src/components/CommandPalette.test.tsx ui/frontend/src/App.tsx
git commit -m "ui: add focused session navigation"
```

### Task 5: Render conversation, Markdown, diffs, and activity summaries

**Files:**
- Create: `ui/frontend/src/features/conversation/Conversation.tsx`
- Create: `ui/frontend/src/features/conversation/Message.tsx`
- Create: `ui/frontend/src/features/conversation/MarkdownContent.tsx`
- Create: `ui/frontend/src/features/conversation/CodeBlock.tsx`
- Create: `ui/frontend/src/features/conversation/conversation.module.css`
- Create: `ui/frontend/src/features/conversation/Conversation.test.tsx`
- Create: `ui/frontend/src/features/conversation/ConversationSearch.tsx`
- Create: `ui/frontend/src/features/conversation/ConversationSearch.test.tsx`
- Create: `ui/frontend/src/features/activity/ActivityCard.tsx`
- Create: `ui/frontend/src/features/activity/groupActivities.ts`
- Create: `ui/frontend/src/features/activity/groupActivities.test.ts`

**Interfaces:**
- Consumes: `TranscriptState.messages`, streamed text, ordered activities
- Produces: readable transcript and clickable grouped activity summaries

- [ ] **Step 1: Write failing transcript and grouping tests**

Test Markdown links, code copy, diff line classes, no raw HTML execution,
streaming append, authoritative replacement, user/assistant alignment,
auto-scroll only when already near the bottom, and a New messages affordance
when scrolled away. Test that `Command+F` opens conversation search, moves among
matches without rewriting message content, and returns focus to the matched
message when closed.

Test that routine adjacent reads group together, while user-facing narration,
permission, plan review, subagent boundaries, errors, and turn completion end a
group.

```ts
expect(groupActivities([
  activity("read_file", "complete"),
  activity("list_dir", "complete"),
  activity("bash", "error"),
])).toHaveLength(2);
```

- [ ] **Step 2: Run conversation tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/features/conversation src/features/activity`

Expected: FAIL because the components do not exist.

- [ ] **Step 3: Implement safe content and compact activity rendering**

Use `react-markdown` with `remark-gfm` and omit raw-HTML plugins. External
links get `target="_blank"` and `rel="noreferrer"`; local file paths call the
platform adapter only from an explicit Reveal action. Render fenced `diff`
blocks line-by-line with additions, removals, and hunk headers; all other code
uses a copyable `<pre><code>`.

Activity cards show status, elapsed time, action count, and summarized test
results. The full result is never truncated in state; only the card preview is
visually clamped. Clicking calls `openInspector(activityId)`.

- [ ] **Step 4: Run conversation tests and build**

Run: `cd ui/frontend && npm test -- --run src/features/conversation src/features/activity && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit conversation rendering**

```bash
git add ui/frontend/src/features/conversation ui/frontend/src/features/activity
git commit -m "ui: render calm conversation activity"
```

### Task 6: Implement the editable composer, modes, queue, and stop states

**Files:**
- Create: `ui/frontend/src/features/conversation/Composer.tsx`
- Create: `ui/frontend/src/features/conversation/useDraft.ts`
- Create: `ui/frontend/src/features/conversation/composer.module.css`
- Create: `ui/frontend/src/features/conversation/Composer.test.tsx`
- Modify: `ui/frontend/src/App.tsx`

**Interfaces:**
- Consumes: `TranscriptState.running`, `stopping`, `queued`, active session id
- Produces: send, queue, clear queue, stop, retry, base/plan mode messages

- [ ] **Step 1: Write failing composer tests**

```tsx
it("keeps the draft editable and queues one follow-up while running", async () => {
  render(<Composer running queued={null} onQueue={onQueue} />);
  await user.type(screen.getByRole("textbox"), "next request");
  await user.keyboard("{Meta>}{Enter}{/Meta}");
  expect(onQueue).toHaveBeenCalledWith("next request", "base");
  expect(screen.getByRole("textbox")).toHaveValue("");
});
```

Also test IME composition, blank input, `Command+Enter`, Stop label, stopping
label, queue edit/clear, draft isolation by session id, retry preserving text,
plan mode selection, slash command suggestions, Escape's staged close/Stop
focus behavior, and focus after completion.

- [ ] **Step 2: Run composer tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/features/conversation/Composer.test.tsx`

Expected: FAIL because the composer does not exist.

- [ ] **Step 3: Implement composer state**

Persist drafts in memory keyed by session id, with a debounced local preference
backup that contains draft text only. Do not send during `compositionstart`.
When idle, send `send_message`; when running, send `queue_message`; when one
message is queued, edits update that queue rather than create another.

The mode control emits `"base"` or `"plan"`. It never changes the session's
base permission mode. Send becomes a square Stop control while active and
announces “Stopping after current action” when `turn_stopping` arrives.

- [ ] **Step 4: Run composer tests and typecheck**

Run: `cd ui/frontend && npm test -- --run src/features/conversation/Composer.test.tsx && npm run typecheck`

Expected: PASS.

- [ ] **Step 5: Commit composer interactions**

```bash
git add ui/frontend/src/features/conversation ui/frontend/src/App.tsx
git commit -m "ui: add queued daily-driver composer"
```

### Task 7: Add inline permission and plan-review cards

**Files:**
- Create: `ui/frontend/src/features/permissions/PermissionCard.tsx`
- Create: `ui/frontend/src/features/permissions/PermissionCard.test.tsx`
- Create: `ui/frontend/src/features/permissions/permissionCard.module.css`
- Create: `ui/frontend/src/features/plan-review/PlanReviewCard.tsx`
- Create: `ui/frontend/src/features/plan-review/PlanReviewCard.test.tsx`
- Create: `ui/frontend/src/features/plan-review/planReviewCard.module.css`
- Modify: `ui/frontend/src/features/conversation/Conversation.tsx`

**Interfaces:**
- Consumes: active permission/plan request and socket answer methods
- Produces: focused inline decision cards and safe answer payloads

- [ ] **Step 1: Write failing decision-card tests**

Test visible action, formatted arguments, workspace/network scope, policy
reason, Allow/Deny/Always, disabled duplicate submission, matching request id,
focus on arrival, focus restoration, live-region announcement, and keyboard
shortcuts only while the card contains focus.

Test plan Markdown, Approve plan, Revise with optional feedback, and feedback
returned only on revision.

- [ ] **Step 2: Run decision tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/features/permissions src/features/plan-review`

Expected: FAIL because the cards do not exist.

- [ ] **Step 3: Implement inline decisions**

Render cards at their transcript event anchor. Use native buttons and a
labelled feedback textarea. `Always` copy includes “for this session.” Network
egress receives explicit text even when the tool is read-only. Once answered,
replace controls with the resolved decision rather than removing the card.

Store the previously focused element before moving focus; restore only if it is
still connected. Avoid a global `keydown` listener: attach shortcuts to the
focused card root.

- [ ] **Step 4: Run decision tests and accessibility scan**

Run: `cd ui/frontend && npm test -- --run src/features/permissions src/features/plan-review`

Expected: PASS with zero axe violations in both card fixtures.

- [ ] **Step 5: Commit inline decisions**

```bash
git add ui/frontend/src/features/permissions ui/frontend/src/features/plan-review ui/frontend/src/features/conversation/Conversation.tsx
git commit -m "ui: review permissions and plans inline"
```

### Task 8: Build the on-demand activity inspector

**Files:**
- Create: `ui/frontend/src/features/inspector/ActivityInspector.tsx`
- Create: `ui/frontend/src/features/inspector/Timeline.tsx`
- Create: `ui/frontend/src/features/inspector/SafetySummary.tsx`
- Create: `ui/frontend/src/features/inspector/ContextSummary.tsx`
- Create: `ui/frontend/src/features/inspector/useInspector.ts`
- Create: `ui/frontend/src/features/inspector/inspector.module.css`
- Create: `ui/frontend/src/features/inspector/ActivityInspector.test.tsx`
- Modify: `ui/frontend/src/App.tsx`

**Interfaces:**
- Consumes: activity timeline, safety snapshot, context state, selected activity id
- Produces: closed-by-default resizable right drawer and pinned-session preference

- [ ] **Step 1: Write failing inspector tests**

Test closed default, open from activity, open from header, complete arguments
and result, copy action, actor/subagent nesting, duration, permission reason,
network egress label, real sandbox backend, context mode, resize bounds,
session-local pin, close focus restoration, and `Command+Shift+I`.

- [ ] **Step 2: Run inspector tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/features/inspector`

Expected: FAIL because the inspector does not exist.

- [ ] **Step 3: Implement the drawer**

Use a non-modal Radix Dialog on wide layouts and a modal sheet below 800 px.
Clamp width between 320 and 640 px. Persist width globally and pinned state per
session. Use a tree list for parent/child activity relationships and native
`<details>` for large arguments/results. Preserve full text; use CSS containment
and scroll regions rather than string truncation.

- [ ] **Step 4: Run inspector tests and build**

Run: `cd ui/frontend && npm test -- --run src/features/inspector && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit the inspector**

```bash
git add ui/frontend/src/features/inspector ui/frontend/src/App.tsx
git commit -m "ui: add on-demand harness inspector"
```

### Task 9: Add onboarding, settings, credential prerequisites, and recovery

**Files:**
- Create: `ui/frontend/src/features/onboarding/Onboarding.tsx`
- Create: `ui/frontend/src/features/onboarding/CredentialPrerequisite.tsx`
- Create: `ui/frontend/src/features/onboarding/Onboarding.test.tsx`
- Create: `ui/frontend/src/features/settings/Settings.tsx`
- Create: `ui/frontend/src/features/settings/Settings.test.tsx`
- Create: `ui/frontend/src/components/RecoveryView.tsx`
- Create: `ui/frontend/src/components/ConnectionStatus.tsx`
- Create: `ui/frontend/src/components/RecoveryView.test.tsx`
- Modify: `ui/frontend/src/App.tsx`

**Interfaces:**
- Consumes: config/health responses, `PlatformAdapter`, recoverable error categories
- Produces: first-run workspace flow, preferences, reconnect/crash/missing-workspace recovery

- [ ] **Step 1: Write failing onboarding and recovery tests**

Test browser absolute-path entry, Tauri Choose folder, invalid path error,
local-only explanation, default permission/context choices, `codex login`
prerequisite copy, retry credential check, reconnect progress, missing workspace
Locate/Archive, and second sidecar failure actions Restart service/Open logs/Quit.

- [ ] **Step 2: Run onboarding tests and verify failure**

Run: `cd ui/frontend && npm test -- --run src/features/onboarding src/features/settings src/components/RecoveryView.test.tsx`

Expected: FAIL because the components do not exist.

- [ ] **Step 3: Implement onboarding and settings**

First run has three explicit states: service check, credential prerequisite,
and workspace selection. Do not expose tokens. Browser workspaces use a path
field with server validation; Tauri uses `chooseWorkspace()` and displays the
returned canonical path.

Settings writes appearance, default base mode, compaction/folding choice,
notification behavior, sidebar preference, shortcuts reference, and local data
locations. Changing defaults never mutates an already-running turn.

Map each server error category to one stable recovery view. Reconnect uses
quiet inline status for the first 10 seconds, then a visible recovery banner.
When a non-visible session requests permission or completes, call
`PlatformAdapter.notify` only if the corresponding preference is enabled.

- [ ] **Step 4: Run onboarding tests and build**

Run: `cd ui/frontend && npm test -- --run src/features/onboarding src/features/settings src/components && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit onboarding and recovery**

```bash
git add ui/frontend/src/features/onboarding ui/frontend/src/features/settings ui/frontend/src/components ui/frontend/src/App.tsx
git commit -m "ui: add local onboarding and recovery"
```

### Task 10: Verify accessibility, responsive behavior, and browser journeys

**Files:**
- Create: `ui/frontend/playwright.config.ts`
- Create: `ui/frontend/e2e/first-run.spec.ts`
- Create: `ui/frontend/e2e/daily-turn.spec.ts`
- Create: `ui/frontend/e2e/permission-plan.spec.ts`
- Create: `ui/frontend/e2e/reconnect.spec.ts`
- Create: `ui/frontend/e2e/keyboard.spec.ts`
- Create: `ui/frontend/e2e/visual.spec.ts`
- Modify: `ui/frontend/src/styles/global.css`
- Modify: `ui/frontend/src/app.css`
- Modify: `ui/README.md`

**Interfaces:**
- Consumes: fake local service fixtures and built React app
- Produces: browser acceptance suite and approved visual baselines

- [ ] **Step 1: Add failing end-to-end journeys**

Cover first run; new/resumed session; streamed reply; grouped tools; inline
permission; plan approve/revise; queue; stop; retry reset; inspector; reconnect;
missing workspace; sidebar collapse; light/dark; reduced motion; and complete
keyboard navigation. Use stable roles and visible copy, not CSS selectors.

- [ ] **Step 2: Run the journeys and record actual failures**

Run: `cd ui/frontend && npm run e2e`

Expected: FAIL only for uncovered integration or visual mismatches; record each
failing test name in the task log before editing.

- [ ] **Step 3: Fix integration and responsive gaps**

Verify desktop widths 1440, 1100, and 900 px plus a 720 px functional narrow
layout. Keep the composer visible, avoid horizontal page scroll, switch the
inspector to a sheet below 800 px, and retain 44 px primary actions in the
narrow layout. Fix focus, names, or contrast at the responsible component; do
not suppress accessibility assertions.

- [ ] **Step 4: Run the complete frontend and service checks**

Run: `cd ui/frontend && npm test -- --run && npm run typecheck && npm run build && npm run e2e`

Expected: all commands exit 0.

Run: `cd ui && uv run pytest -v`

Expected: all local-service tests pass offline.

- [ ] **Step 5: Commit browser acceptance**

```bash
git add ui/frontend ui/README.md
git commit -m "ui: verify polished browser experience"
```

## Plan 2 Completion Gate

Before starting Tauri delivery:

1. Unit, component, accessibility, typecheck, build, and Playwright suites pass.
2. The same built frontend runs against the local service without Tauri globals.
3. Core journeys work at 1440, 1100, 900, and 720 px.
4. Permission and plan review are keyboard-complete and focus-safe.
5. Stream reset, reconnect, and authoritative completion remove stale content.
6. Inspector starts closed and exposes full detail without duplicating state.
7. `git status --short` is clean.
