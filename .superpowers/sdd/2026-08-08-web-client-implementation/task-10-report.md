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
- The Playwright config uses the normal bundled browser when its expected revision is installed. On this offline macOS ARM environment only, it detects the newest locally cached Playwright Chromium headless shell. Other platforms still need their normal Playwright browser installation.
- Node emits the existing non-fatal `localStorage is not available because --localstorage-file was not provided` warning in two unit-test workers; all 311 tests pass and no browser storage behavior is affected.

Self-review found no edits outside `ui/` and this report, no root `harness/` or `main.py` changes, no real credentials, no fixture capability in production code, no disabled axe rule, and no test-only production branch. Accessible roles/names used by the browser tests are user-facing interface semantics rather than private CSS selectors. `git diff --check`, frontend type/build/unit/browser verification, and all service tests pass.
