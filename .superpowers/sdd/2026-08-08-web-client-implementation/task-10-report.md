# Web Task 10 report

## Scope and authority

- Baseline: clean `ae3271c546641e19105ab5e3bfb07453a45eeca9` on `ui/daily-driver-design`.
- Product authority: the existing UI and `docs/streams/ui/2026-08-08-daily-driver-web-macos-ui-design.md`; no redesign.
- Browser approval: the user explicitly approved Playwright's bundled Chromium.
- Fixture policy: deterministic loopback-only dummy capabilities; no real credentials, provider detail, or external network.

## RED/GREEN evidence

### RED 1 — production session WebSocket ownership

Before any production wiring change, added the smallest built-app Playwright fixture and ran:

```text
cd ui/frontend
npm run e2e -- --grep "built app opens the active session WebSocket"
```

After correcting two harness-only preconditions (serving relative assets beneath the capability path, then targeting the actual accessible session-row name), the expected product RED was observed:

```text
1 failed
[bundled-chromium] › e2e/daily-turn.spec.ts:3:1 › built app opens the active session WebSocket
Expected: 1
Received: 0
Timeout 3000ms exceeded while waiting on the predicate
```

The browser had already removed the fragment, completed authenticated health/config/session REST requests, and rendered `Fixture session, Workspace /fixtures/workspace`. The missing observable connection therefore isolates the preflight finding: production `App` has no `SessionSocket` owner.

Environment note: Playwright 1.62.1's expected revision 1234 was not cached. Browser execution used the newest locally installed Playwright-bundled Chromium headless-shell revision 1223 through `launchOptions.executablePath`; no download or external network was used.

GREEN: the same command passed `1 passed (4.4s)` after production socket ownership was added. Focused compatibility checks also passed: `69 passed (69)` across `App.test.tsx` and `sessionSocket.test.ts`, with typecheck exit 0.

### RED 2 — truthful terminal announcement

Command: `npm run e2e -- e2e/daily-turn.spec.ts`.

```text
[bundled-chromium] › queues, edits, clears, and stops without a false completion announcement
Expected: not "Response complete"
Received: "Response complete"
```

The authoritative terminal was `turn_cancelled`; this isolates the running-to-idle announcement branch, which did not distinguish completed from cancelled/failed.

### RED 3 — fast failed-turn retry authority

Same command:

```text
[bundled-chromium] › retries the exact failed turn with a fresh submission id
locator.click: Test timeout ... waiting for getByRole('button', { name: 'Retry' })
```

The fixture emitted valid consecutive `turn_started` and `turn_failed` envelopes. React batched the external socket state updates, so App never rendered the intermediate active turn and did not bind the retained submission to the terminal. The terminal needs to retain the reducer-observed submission identity so retry authority survives batching.

The same run also exposed one test-only strict-locator ambiguity for `fixture result` (activity preview and inspector result); it requires a scoped locator, not a product change.

GREEN: focused reducer/App/Conversation checks passed `99 passed (99)`. The daily-turn browser rerun passed the stream/activity/inspector, queue/edit/clear/stop, and announcement cases; the retry case required one final rerender trigger after the effect established terminal authority, then its focused rerun passed `1 passed (4.5s)`.

### RED 4 — permission focus waits for authority

Command: `npm run e2e -- e2e/permission-plan.spec.ts`.

```text
[bundled-chromium] › permission uses the exact request id and restores focus only after authoritative resolution
expect(getByRole('group', { name: 'Permission decision' })).toBeFocused()
Received: inactive
```

The exact outbound request ID and answer were already correct. The synchronous socket send caused the component to restore its origin before `permission_resolved`.

### RED 5 — plan focus waits for authority

Same command:

```text
[bundled-chromium] › plan approval waits for authority and revision sends scoped feedback
expect(getByRole('group', { name: 'Plan review' })).toBeFocused()
Received: inactive
```

As with permission, the exact answer was sent but focus left the card before the authoritative resolution envelope.

GREEN: after keeping focus on each decision card until its matching resolution, `npx playwright test e2e/permission-plan.spec.ts` passed `2 passed (5.0s)`, including exact permission/plan IDs and revision feedback.

### RED 6 — socket reconnect lifecycle projection

Command: `npx playwright test e2e/reconnect.spec.ts`.

```text
[bundled-chromium] › projects quiet socket reconnect, escalates at ten seconds, and self-heals on retry
getByRole('status', { name: 'Local service reconnecting' }): element(s) not found
```

The socket scheduled its internal retry but published no lifecycle to App.

### RED 7 — missing-workspace category preservation

Same command:

```text
[bundled-chromium] › missing workspace preserves its category and offers honest archive recovery
getByRole('heading', { name: 'Workspace unavailable' }): element(s) not found
```

App collapsed `missing_workspace` into generic `turn_failure`. The same run's generation-replacement/stale-event case passed, proving the existing reducer/socket authority already removed stale stream state.

GREEN: typecheck and the 69 focused socket/App tests passed; the reconnect browser file then passed `3 passed (5.1s)` for quiet/escalated/manual reconnect, generation replacement, and archive recovery.

### RED 8 — target sizes at required widths

Command: `npx playwright test e2e/visual.spec.ts` after narrowing one ambiguous test locator.

```text
responsive shell remains functional at 1440px / 1100px / 900px
Expected mode height >= 32; Received 28

responsive shell remains functional at 720px
Expected Send height >= 44; Received 36
```

This confirms the preflight CSS findings in the built browser at all required viewports.

### RED 9 — light appearance small-text contrast

Same unsuppressed axe run:

```text
color-contrast (serious), target .workspace
contrast 4.27:1 at 11px; expected 4.5:1
```

No axe rule was disabled. The responsible light-theme small-ink token needs a slight darkening; the dark theme remains unchanged.

GREEN: the responsive/browser rerun passed all four widths, target sizes, page overflow, composer visibility, sidebar rail, and inspector drawer/sheet assertions. The focused wide-light/narrow-dark axe rerun passed with zero violations.

### RED 10 — inspector landmark uniqueness

The representative increased-contrast permission/error/inspector axe slice failed only after opening the inspector:

```text
landmark-no-duplicate-banner (moderate): Document has more than one banner landmark
landmark-unique (moderate): landmark must have a unique label
target: .conversation-header
```

The inspector's internal `<header>` was promoted to a document banner alongside the conversation header. It is visual structure inside a dialog, not a document landmark.

## Verification evidence

Fresh verification was run against the final implementation tree after constraining Vitest to unit-test files (so it does not attempt to execute Playwright specs):

```text
cd ui/frontend
npm test -- --run
Test Files  25 passed (25)
Tests       311 passed (311)

npm run typecheck
exit 0

npm run build
1954 modules transformed
dist/assets/index-DZZ0URNL.js  410.16 kB (128.03 kB gzip)
exit 0

npm run e2e
25 passed (11.8s)
```

The service regression suite and patch hygiene also passed:

```text
cd ui
uv run pytest -q
398 passed in 8.59s

git diff --check
exit 0, no output
```

The browser suite uses Playwright `1.62.1`, project `bundled-chromium`, and the locally cached Playwright Chromium binary reporting `Google Chrome for Testing 148.0.7778.96`. The suite authenticates every fixture REST and WebSocket request with a fixed low-value dummy capability, rejects external HTTP requests, and does not weaken production authentication.

The completed implementation owns one production `SessionSocket` per listed session; sends, streams, groups activity, opens inspector detail, queues/edits/clears/stops, retains failed-submission retry authority across batched events, exposes reconnect lifecycle, and preserves typed missing-workspace recovery. Unit tests were updated where focus authority intentionally changed: permission and plan cards keep focus until their matching authoritative resolution instead of treating a synchronous outbound send as completion.

## Browser matrix and visual baselines

All browser cases ran in the built application with a deterministic loopback REST/WebSocket authority and external network denied. The 25-test matrix covers:

- Daily turn send, streaming, activity grouping, completion, inspector detail, queue/edit/clear, stop, truthful announcements, and exact failed-turn retry with a fresh submission ID.
- First-run quiet service recovery at ten seconds, absolute workspace validation, and exact credential-prerequisite retry.
- Primary keyboard shortcuts, sidebar-before-header-before-composer tab order, focus-origin restoration, and scoped destructive keys.
- Permission and plan decisions with exact IDs, authoritative focus timing, and scoped revision feedback.
- Quiet then escalated socket reconnect, manual retry/self-heal, generation replacement, stale-event rejection, typed missing-workspace archive recovery, and retained cleanup retry.
- Responsive behavior at `1440x900`, `1100x900`, `900x900`, and `720x900`, including no page scroll, visible composer, sidebar rail, inspector drawer/sheet behavior, and required target sizes.
- Unsuppressed axe checks in wide light, narrow dark, permission, error, inspector, first-run, and increased-contrast states; reduced-motion computed-style checks.

The first visual run intentionally failed for missing approved snapshots. Each generated candidate was then opened and visually inspected before acceptance, and the focused visual rerun passed `3 passed`. The final full browser run passed all eight deterministic baselines:

- `primary-1440-light-bundled-chromium-darwin.png` — full light shell, balanced sidebar/conversation/inspector-free composition.
- `streaming-1100-bundled-chromium-darwin.png` — active response and grouped activity remain readable without page overflow.
- `permission-900-bundled-chromium-darwin.png` — decision card hierarchy and composer remain usable at the compact breakpoint.
- `tool-failure-inspector-1440-bundled-chromium-darwin.png` — error state and inspector detail coexist without landmark or clipping regressions.
- `primary-720-dark-bundled-chromium-darwin.png` — dark narrow shell, collapsed rail, and composer preserve hierarchy and targets.
- `reconnect-1100-bundled-chromium-darwin.png` — reconnect status is visible and not conveyed by color alone.
- `first-run-720-bundled-chromium-darwin.png` — narrow onboarding/recovery layout preserves readable path and action controls.
- `reduced-motion-900-bundled-chromium-darwin.png` — compact shell retains the same stable hierarchy with transitions removed.

## Residual concerns

- The current service contract has no workspace-relocation REST operation, and the browser platform chooser cannot return a native filesystem path. The web client therefore preserves the `missing_workspace` category and offers an honest, production-capable **Archive session** recovery; it does not present a fake **Locate** action. A true browser relocation flow remains blocked on a service API (or an explicitly supported browser path-selection contract).
- Superseded in fix round 1: the initial config selected the newest local macOS ARM headless shell when the package revision was unavailable. The final config instead pins and validates the exact reviewed offline artifact described below.
- Node emits the existing non-fatal `localStorage is not available because --localstorage-file was not provided` warning in two unit-test workers; all 311 tests pass and no browser storage behavior is affected.

Self-review found no edits outside `ui/` and this report, no root `harness/` or `main.py` changes, no real credentials, no fixture capability in production code, no disabled axe rule, and no test-only production branch. Accessible roles/names used by the browser tests are user-facing interface semantics rather than private CSS selectors. `git diff --check`, frontend type/build/unit/browser verification, and all service tests pass.

## Fix round 1 — independent review

### Root-cause analysis and RED/GREEN evidence

#### 1. Session-list reconciliation destroyed retained socket authority

Root cause: the ID-reconciliation effect returned the component/connection cleanup. React therefore disposed and cleared the entire socket map before every `sessionKey` rerun. Retained sessions were recreated from `emptyTranscript()`, and removed-session keys could no longer be found in the cleared map for state pruning.

The focused hook test starts a real `SessionSocket` through a controlled WebSocket boundary, streams text, adds and reorders a second session, removes it, and finally unmounts. Before the fix:

```text
npm test -- --run src/api/useSessionSockets.test.tsx
1 failed
expected [ FakeWebSocket{…}, FakeWebSocket{…} ] to deeply equal [ FakeWebSocket{…} ]
```

This proved the retained session had been recreated and its original socket closed. Cleanup is now owned by a connection/unmount-only effect; ID reconciliation separately adds missing sockets, disposes removed sockets, and prunes transcript/lifecycle records directly by wanted ID. Focused GREEN:

```text
npm test -- --run src/api/useSessionSockets.test.tsx src/api/sessionSocket.test.ts src/App.test.tsx
Test Files 3 passed (3)
Tests      70 passed (70)
```

The test additionally proves a pure reorder creates no socket and that unmount still disposes the retained socket exactly once.

#### 2. Browser selection was nondeterministic

Root cause: the config enumerated every cached headless-shell directory, sorted the directory names lexicographically, and chose the first executable. The package-pinned Playwright revision was neither preferred nor validated, while the accepted baselines were actually generated with cached revision 1223.

The offline suite now explicitly pins one reviewed artifact instead of scanning: Playwright headless-shell revision `1223`, validated at config load against `Google Chrome for Testing 148.0.7778.96`. The browser test independently asserts `browser.version() === "148.0.7778.96"`. Missing or mismatched artifacts fail configuration with an explicit error. The eight existing baselines were retained unchanged and the full visual suite passed; no snapshots were regenerated.

#### 3. WebSocket fixture did not enforce authentication

Root cause: the routed WebSocket callback counted readiness and sent a snapshot without inspecting handshake protocols. A first exploratory check from `about:blank` surfaced a Chromium pre-open `1006`, which did not exercise the loopback route. The final test first establishes the loopback page origin, then opens missing and incorrect protocol pairs. An explicit mutation removing the gate reproduced the original defect:

```text
npx playwright test e2e/daily-turn.spec.ts --grep "rejects missing and incorrect"
1 failed
Expected: not "opened"
Received: "opened"
```

The fixture now accepts readiness only when `WebSocketRoute.protocols()` equals exactly `["harness-ui", apiCapability]`; rejected attempts never increment the authenticated connection count. Restored focused GREEN: `1 passed (4.5s)`. The same test then opens the production client and proves the exact valid pair reaches readiness.

#### 4. Keyboard traversal and visible decision focus were under-proven

Root cause for the product defect: permission and plan cards were focused programmatically after a pointer-originated send, but their ring was limited to `:focus-visible`. Chromium correctly focused the cards without matching that pseudo-class, so the user had no visible focus evidence. The browser RED for both cards was:

```text
Expected: true
Received: false
```

The cards now render the same approved focus ring for their programmatic `:focus` state. Permission coverage moves focus outside the card and proves `A` has no decision effect, returns by keyboard to a visibly focused card-owned control, then proves `A` sends the exact request ID and `yes`. Plan approval, revision entry, feedback submission, and authoritative revision resolution are all keyboard-driven; focus remains on the card until resolution and restores to the composer afterward. Focused GREEN: `2 passed (5.0s)`.

The primary tab test now proves visible focus and order across sidebar → header → transcript activity → composer → inspector, activates transcript/inspector controls by keyboard, returns focus after Escape, and verifies `A`/`D`/`S` remain ordinary composer input. Focused GREEN: `2 passed (4.9s)`.

#### 5. Responsive and motion assertions omitted required invariants

New direct assertions verify the 820 px readable transcript measure, visible sidebar connection status in full and rail layouts, every visible desktop interactive target at least 32×32 px, narrow New chat/Send/inspector-close targets at least 44 px, and inspector focus return at every viewport. The narrow modal sheet exposed a real timing defect:

```text
responsive shell remains functional at 720px
Expected: focused
Received: inactive
```

Root cause: `useInspector.close()` restored the origin synchronously while Radix's modal focus scope was still mounted, allowing the scope to reclaim focus. Restoration now runs in the next microtask, after the close state commits. Focused GREEN: the 720 px browser case passed and all four `useInspector` unit tests remained green.

Reduced-motion coverage now checks every rendered element's transition duration, animation duration, animation name (including pulses), and computed `scroll-behavior`, not transitions alone. The combined responsive/motion slice passed `5 passed`; the complete visual file passed `11 passed`, including all unchanged approved baselines.

#### 6. Queue clear and completion announcement behavior were not independently observed

Root cause: the only queued affordance was labelled Edit and used clear as an implementation step, so there was no user-facing discard action. Focused product RED:

```text
npm test -- --run src/features/conversation/Composer.test.tsx -t "distinct clear control"
1 failed
Unable to find an accessible element with the role "button" and name "Clear queued follow-up"
```

The queued affordance now exposes separate Edit (clear then restore text/mode) and Clear (discard without restoring) controls. Retry authority retains which intent was requested. Composer GREEN: `34 passed (34)`. The browser journey invokes Clear and proves an empty draft, then separately invokes Edit and proves the queued text is restored.

The successful-turn browser case installs a MutationObserver on the real conversation live region before the terminal envelope. It observes exactly `["Response complete"]`, sends a subsequent authoritative safety update, waits through two animation frames, and proves the sequence remains exactly one announcement. Cancellation still proves no completion announcement. Daily-turn GREEN: `6 passed (5.5s)`.

### Fix-round full regression

Fresh final commands against the complete fix tree:

```text
cd ui/frontend
npm test -- --run
Test Files  26 passed (26)
Tests       313 passed (313)

npm run typecheck
exit 0

npm run build
1954 modules transformed
exit 0

npm run e2e
27 passed (12.1s)

cd ui
uv run pytest -q
398 passed in 9.75s
```

The browser matrix remains Playwright `1.62.1`, project `bundled-chromium`, pinned offline revision `1223`, `Google Chrome for Testing 148.0.7778.96`. It covers `1440x900`, `1100x900`, `900x900`, and `720x900`; unsuppressed axe slices; reduced motion/increased contrast; and all eight accepted Darwin PNG baselines without modification.

### Fix-round scope and self-review

- Product changes are limited to socket reconciliation, distinct queue intent, decision-card focus visibility, and post-modal focus restoration.
- Harness changes are limited to exact WebSocket protocol rejection, deterministic browser validation, and the requested behavioral assertions.
- No root `harness/` or `main.py` file changed; no external network, real credential, production auth bypass, axe suppression, or private selector was added.
- The explicit browser artifact makes runs reproducible on this approved offline macOS ARM lane; a missing revision fails loudly rather than selecting another cache entry.
- The pre-existing Node `localStorage` warning remains deferred exactly as requested and is not changed in this fix round.
- `git diff --check` and final scope/status checks are required immediately before the fix commit.

## Fix round 2 — unclipped sidebar identity

### Root cause and strict RED/GREEN evidence

The full 224 px sidebar left 204 px after outer padding. Its identity used one non-wrapping flex row containing the 20 px brand icon, two gaps, **Agent Harness**, and the icon-plus-text connection status. The status could shrink, but its text still painted past the identity boundary and was then clipped by the sidebar's `overflow: hidden`, leaving **Connected** truncated in every full-sidebar baseline.

A Playwright regression test now measures the rendered identity geometry at `1440x900`, verifies the accessible status named **Local service connected**, verifies visible **Connected** text, and requires both the text box and status scroll width to remain within the identity. Before the production change:

```text
npx playwright test e2e/visual.spec.ts --grep "full sidebar identity"
Expected: <= 213
Received: 229.859375
1 failed
```

The identity now uses an intentional two-row grid: the Bot icon spans the two rows, **Agent Harness** remains the visible product name, and the existing Lucide check plus **Connected** status occupies its own named row. The compact/collapsed layout retains its icon-only rail and right-aligned connection mark. Focused GREEN:

```text
npx playwright test e2e/visual.spec.ts --grep "full sidebar identity|responsive shell remains functional at 1440px"
2 passed

npx playwright test e2e/visual.spec.ts --grep "full sidebar identity|responsive shell|axe violations"
8 passed
```

This covers every responsive width (`1440`, `1100`, `900`, and `720`), the full and rail status presentations, visible/non-color status semantics, accessible naming, required target sizes, and unsuppressed axe checks.

### Visual review and baseline scope

The first post-fix visual run identified the expected full-sidebar changes and also revealed a 50-pixel compact-rail alignment drift. Explicitly right-aligning the collapsed status restored the compact presentation; the `permission-900`, `primary-720-dark`, `first-run-720`, and `reduced-motion-900` baselines then passed unchanged.

The old and new images for all remaining candidates were opened together at original resolution. Review confirmed a coherent two-line identity with the complete status and no unrelated content, inspector, conversation, composer, target, or rail movement. Only these four full-sidebar baselines were regenerated through the pinned Playwright project:

- `primary-1440-light-bundled-chromium-darwin.png`
- `streaming-1100-bundled-chromium-darwin.png`
- `tool-failure-inspector-1440-bundled-chromium-darwin.png`
- `reconnect-1100-bundled-chromium-darwin.png`

Fresh visual verification passed all responsive, axe, motion, clipping, and approved-image cases:

```text
npx playwright test e2e/visual.spec.ts
12 passed (8.5s)
```

### Fix-round-2 full regression and scope

```text
cd ui/frontend
npm test -- --run
Test Files  26 passed (26)
Tests       313 passed (313)

npm run typecheck
exit 0

npm run build
1954 modules transformed
exit 0

npm run e2e
28 passed (12.5s)

cd ui
uv run pytest -q
398 passed in 9.73s
```

The browser remains Playwright `1.62.1`, project `bundled-chromium`, pinned offline revision `1223`, `Google Chrome for Testing 148.0.7778.96`. Production scope is limited to sidebar identity layout CSS; harness scope is one user-facing geometry regression assertion. No authentication, external network, axe suppression, private selector, root `harness/`, or `main.py` change was introduced. The pre-existing Node `localStorage` warning remains unchanged.
