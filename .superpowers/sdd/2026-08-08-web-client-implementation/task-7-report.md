# Task 7 Report — Inline permission and plan review

## Outcome

Implemented typed, chronologically anchored permission and plan-review history,
calm inline decision cards, exact answer events, focus-safe recovery, and active
session routing through the existing Conversation/App boundary.

No Task 6 deferred Minor, inspector, Tauri, deployment, service, or harness
behavior was changed.

## State and authority

- `TranscriptState.timeline` now stores typed permission and plan-review items
  containing the full request and an optional authoritative resolution.
- `TranscriptState.permission` and `planReview` remain active socket-ownership
  pointers. A stale resolution updates only the historical item with the same
  request id and cannot clear a newer active request.
- Turn start and snapshots reset current-turn timeline state. Terminal events
  clear active blockers while retaining anchored decisions in the turn history.
- Decision items pass through the existing grouping path, split routine
  activity groups, and render at the originating event position.
- App binds the active Conversation callback to the captured stable session id.

## TDD evidence

The complete `test-driven-development` skill and `writing-good-tests.md`
reference were read before edits. Tests were written before each production
slice and assert visible behavior and exact typed boundary events.

Protocol RED:

```text
cd ui/frontend
npm test -- --run src/protocol/reducer.test.ts
```

Result: 1 failed file; 3 failed and 20 passed. Permission chronology expected
typed request/resolution items but received generic permission boundaries; plan
review expected a persistent resolved item but received a generic plan boundary;
terminal cancellation expected the unresolved anchored request to remain but
received only the generic boundary.

Protocol GREEN: the same command passed 23/23.

Decision-card and Conversation RED:

```text
cd ui/frontend
npm test -- --run src/features/permissions/PermissionCard.test.tsx \
  src/features/plan-review/PlanReviewCard.test.tsx \
  src/features/conversation/Conversation.test.tsx
```

Result: 3 failed files. Both card suites failed at the intended missing-module
boundary. Conversation ran 17 tests: 15 passed and the two new tests failed
because typed decision items rendered as empty assistant messages, with no
anchored controls or exact answer route.

App routing RED:

```text
cd ui/frontend
npm test -- --run src/App.test.tsx -t "routes inline decisions"
```

Result: 1 failed and 12 skipped because the active session exposed no inline
Allow control.

Focused GREEN:

```text
cd ui/frontend
npm test -- --run src/protocol \
  src/features/activity/groupActivities.test.ts \
  src/features/permissions/PermissionCard.test.tsx \
  src/features/plan-review/PlanReviewCard.test.tsx \
  src/features/conversation/Conversation.test.tsx \
  src/App.test.tsx
```

Result: 7 files passed, 105 tests passed, 0 failed.

## Permission behavior

- Shows action, safely pretty-printed JSON arguments with unmodified raw
  fallback, authoritative scope, and policy reason. Presentation parsing never
  changes the wire payload.
- Reads `safety.tools[action].network_egress` and explicitly labels network
  access even when the tool is read-only.
- Emits exact `yes`, `no`, and `always` answers for the matching request id.
  “Always” copy is explicitly scoped to the session.
- A ref-backed per-request lock suppresses duplicate clicks/shortcuts before
  awaiting. Rejection re-enables controls, retains the request, focuses the
  card, and exposes a readable alert.
- Mnemonics are visible and handled only by the focused card root; there is no
  global permission key listener.
- Authoritative resolutions persist inline and replace controls with explicit
  Allowed once, Denied, or Always allowed for this session copy.

## Plan-review behavior

- Reuses the existing `MarkdownContent` renderer, including safe raw-HTML,
  external-link, and local-path handling.
- Approval emits `{type:"answer_plan", request_id, approved:true}` with no
  feedback key.
- Revision uses a labelled optional textarea. Empty feedback is omitted;
  non-empty feedback is trimmed and sent only with `approved:false`; Cancel
  returns to review without sending.
- Duplicate suppression, rejection recovery, exact identity, and persistent
  approved/revision copy follow the same authority rules as permissions.

## Accessibility and responsive behavior

- Newly active cards capture the previous focused element, receive focus, and
  announce a concise request reason. Successful dispatch restores only a still
  connected origin; rejected dispatch keeps focus in the card.
- Controls are native buttons/textareas with visible focus rings, readable
  non-color state labels, and token-only light/dark styling.
- Desktop controls have a 32 px minimum target. Narrow-layout actions expand to
  44 px. Reduced-motion rules remove component transitions.
- Axe runs report zero violations for both fixtures. The jsdom-incompatible
  `color-contrast` rule is disabled to avoid its missing-canvas false diagnostic;
  actual colors remain the existing reviewed theme tokens and focus ring.

## Final verification

```text
cd ui/frontend
npm test -- --run
```

Result: 15 files passed, 192 tests passed, 0 failed, no warnings.

```text
npm run typecheck
```

Result: PASS (`tsc -b --pretty false`).

```text
npm run build
```

Result: PASS without warnings. Vite transformed 1,934 modules and retained the
real Markdown vendor split (`markdown` 165.72 kB; main 344.01 kB).

```text
git diff --check
```

Result: PASS with no whitespace errors.
