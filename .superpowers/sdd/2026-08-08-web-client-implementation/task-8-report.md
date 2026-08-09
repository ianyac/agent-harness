# Task 8 report — on-demand harness inspector

## Scope

Implemented Task 8 only: retained context history, the on-demand inspector and
its state hook, safety/context projections, chronological timeline, activity
detail/copy, responsive Radix drawer/sheet behavior, resizing, session-local
pinning, App routing, tests, and styles.

## TDD evidence

### Protocol/context RED

Command:

```text
cd ui/frontend && npm test -- --run src/protocol/reducer.test.ts src/features/activity/groupActivities.test.ts src/features/conversation/Conversation.test.tsx
```

Result before production edits: 3 files failed; 5 failed / 49 passed (54).
The missing behavior was exact: `latestContext` was `undefined`, snapshot state
did not contain `latestContext: null`, the reducer discarded context markers,
grouping collapsed work across the marker, and Conversation rendered one
activity group instead of two.

### Inspector/hook/App RED

Commands:

```text
npm test -- --run src/features/inspector/useInspector.test.tsx src/features/inspector/ActivityInspector.test.tsx --reporter=dot
npm test -- --run src/features/inspector src/App.test.tsx
```

Results before inspector production edits:

- inspector command: 2 failed suites, no tests collected, with exact missing
  imports for `./useInspector` and `./ActivityInspector`;
- inspector + App command: 3 files failed; App ran 15 tests with 2 expected
  behavioral failures / 13 passed because neither selected activity nor the
  exact shortcut could produce `dialog[name="Activity inspector"]`.

### GREEN/refinement evidence

- protocol/grouping/Conversation: 3 files, 54/54 passed;
- `useInspector`: first pass 3/4; the residual was a test-fixture mistake that
  treated a still-connected, naturally focused element as though the hook had
  restored it. The corrected disconnected-origin fixture passed 4/4;
- ActivityInspector: first pass 4/8. Product copy was made explicitly human
  readable (`Deny`, `Compaction`, `Complete`) rather than depending on CSS
  capitalization. The pointer fixture was changed to bubbling primary-button
  pointer-named `MouseEvent`s with real `clientX`, retaining the genuine pointer
  path. Rerun passed 8/8;
- App: first pass 14/15. Radix outside interaction was dismissing and unpinning
  session A before the sidebar switch completed. Outside interaction now leaves
  the secondary surface under explicit close authority. Rerun passed 15/15;
- a final bounded RED caught omitted plan-revision feedback and a live pointer
  listener surviving inspector unmount (2 failed / 7 passed); the timeline now
  preserves the feedback and pointer cleanup is lifecycle-safe. Rerun passed 9/9;
- combined focused gate: 6 files, 82/82 passed.

## Accessibility and interaction evidence

- Axe ran without disabled rules on representative wide overview,
  wide selected/error, and narrow modal fixtures: zero violations in all three.
- Exact non-repeating `Command+Shift+I`, selected-card and header origins,
  connected-only focus restoration, modal/non-modal labeling, live copy success
  and failure, cyclic/missing parent depth, 0 ms duration, malformed authoritative
  metadata, pointer/keyboard width clamps, and per-session pin switch/return are
  covered by component/App tests.
- The separator reports 320/640 bounds and the current width. CSS retains token
  focus styling, readable error text, reduced-motion/increased-contrast handling,
  32 px desktop controls, and 44 px narrow controls.

## Final verification

```text
cd ui/frontend && npm test -- --run
```

Passed: 17 files, 220/220 tests.

```text
cd ui/frontend && npm run typecheck
```

Passed with no diagnostics.

```text
cd ui/frontend && npm run build
```

Passed without warnings. The real vendor split remains:

- main application JS: 362.82 kB (114.41 kB gzip)
- markdown vendor JS: 165.72 kB (50.46 kB gzip)
- CSS: 37.74 kB (7.01 kB gzip)

`git diff --check` passed. No Playwright/browser, Tauri, deployment, Task 9,
controller ledger, or deferred-Minor work was performed.
