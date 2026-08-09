# Web client final-review fix report

## Scope and authority

- Baseline: `55a1e0a8dc2a3364d40715f365f0ea0a78066b47` on `ui/daily-driver-design`.
- Fix authority: `final-review-fix-brief.md`; only its three Important findings were addressed.
- Browser authority: Playwright project `bundled-chromium`, pinned offline headless-shell revision `1223`, reporting `Google Chrome for Testing 148.0.7778.96`.
- No external network was used. No `.github/`, root `harness/`, `main.py`, authentication, launch-secret, service protocol, or server authority code changed.

## Root-cause verification

1. `SessionController.connect()` already snapshots the authoritative non-null `turn_id` while a turn is running, but `transcriptReducer` unconditionally replaced `activeTurn` with `null`. `useSessionSockets.stop()` reads only `activeTurn`, so the visible Stop control sent nothing after reconnect. The same unconditional reset discarded a previously correlated submission id.
2. App generated submission ids and retained Retry candidates, but owned no renderable optimistic user content. Composer awaited the synchronous socket send and cleared its local draft, while Conversation rendered only authoritative messages and assistant timeline state. The submitted prompt therefore disappeared until completion and had no terminal-failure draft restoration.
3. RecoveryView rendered Locate for every workspace error and invoked `platform.chooseWorkspace()`. The browser adapter truthfully returns `null`, App supplied a no-op relocation callback, and the service exposes no relocation operation, so the browser action was dead.

## Strict RED evidence

All tests below were added or changed before product edits.

Command:

```text
cd ui/frontend
npm test -- --run src/protocol/reducer.test.ts src/api/useSessionSockets.test.tsx src/App.test.tsx src/components/RecoveryView.test.tsx --reporter=verbose
```

Observed product RED: `4 failed` files, `6 failed | 92 passed` tests.

- `transcriptReducer > hydrates the exact running snapshot turn and remaps known submission authority`: expected the generation-2 active turn with `submission-reconnecting`; received `null`.
- `transcriptReducer > hydrates a running snapshot turn without guessing submission authority`: expected the exact snapshot turn with `submissionId: null`; received `null`.
- `useSessionSockets > stops the exact turn hydrated from a running reconnect snapshot`: expected `{type: "cancel_turn", turn_id: "turn-from-snapshot"}`; sent events were `[]`.
- `App > projects a direct prompt immediately, keeps it across session switches, and reconciles completion once`: no accessible `User message` article existed after send.
- `App > restores an exact failed direct prompt`: expected `Restore this exact prompt`; the textbox remained empty.
- `RecoveryView > offers only honest archive recovery for a missing workspace`: expected no Locate button; one was present.

Focused browser RED:

```text
npm run e2e -- --grep "projects a direct prompt before streaming|running reconnect snapshot keeps exact Stop authority|missing workspace preserves"
```

Observed: `3 failed`.

- `projects a direct prompt before streaming and restores it after terminal failure`: user-message article not found.
- `running reconnect snapshot keeps exact Stop authority`: expected exact cancel event; outbound event was `undefined`.
- `missing workspace preserves its category and offers honest archive recovery`: expected Locate count `0`; received `1`.

## Implementation and authority decisions

### Running reconnect identity

- A running snapshot now hydrates `activeTurn` from its exact non-null `turn_id`.
- Submission authority is carried into the new generation only when the prior active turn has the exact same turn id. An initial running snapshot has `submissionId: null`; the client does not guess.
- App remaps retained direct, queued, bound, and retry candidates only through authoritative active-turn, queued-message, or terminal submission ids and the same client/dispatcher owner. Unmatched generation state is retired, preserving stale-submission and queue boundaries.

### Optimistic direct prompts and failure recovery

- App now owns one submission-keyed optimistic direct prompt per session. Conversation receives that stable projection, includes it in search/autoscroll accounting, and renders it before assistant timeline output across Conversation remounts and session switches.
- Queue messages are not projected as direct user messages. A retry replaces the failed attempt's projection with a fresh submission id.
- Exact completed/cancelled terminal identity or a newly authoritative matching user message removes the projection. The terminal check hides it in the same render as authoritative completion, preventing a duplicate frame.
- A failed exact submission remains retryable and supplies a one-time Composer restoration. Restoration writes only into an empty draft; a newer non-empty draft wins and is never overwritten.
- RecoveryView is keyed by authoritative terminal identity rather than temporary Retry availability, so rejected retry feedback survives parent authority rerenders.

### Honest workspace recovery

- RecoveryView no longer accepts or renders Locate and no longer imports the folder icon. Workspace errors retain the exact Archive action.
- The onboarding workspace chooser remains unchanged under `features/onboarding/`; native onboarding selection is still available where the platform adapter supports it.

## Changed files

- `.superpowers/sdd/2026-08-08-web-client-implementation/final-review-fix-report.md`
- `ui/frontend/src/protocol/reducer.ts`
- `ui/frontend/src/protocol/reducer.test.ts`
- `ui/frontend/src/api/useSessionSockets.test.tsx`
- `ui/frontend/src/App.tsx`
- `ui/frontend/src/App.test.tsx`
- `ui/frontend/src/features/conversation/Conversation.tsx`
- `ui/frontend/src/features/conversation/Composer.tsx`
- `ui/frontend/src/components/RecoveryView.tsx`
- `ui/frontend/src/components/RecoveryView.test.tsx`
- `ui/frontend/e2e/fixtures.ts`
- `ui/frontend/e2e/daily-turn.spec.ts`
- `ui/frontend/e2e/reconnect.spec.ts`

## GREEN and full verification evidence

Focused unit/component GREEN:

```text
npm test -- --run src/protocol/reducer.test.ts src/api/useSessionSockets.test.tsx src/App.test.tsx src/components/RecoveryView.test.tsx --reporter=dot
Test Files  4 passed (4)
Tests       99 passed (99)

npm run typecheck
exit 0
```

Focused browser GREEN:

```text
npm run e2e -- --grep "projects a direct prompt before streaming|running reconnect snapshot keeps exact Stop authority|missing workspace preserves"
3 passed (5.0s)
```

Full frontend unit/component suite:

```text
npm test -- --run --reporter=dot
Test Files  26 passed (26)
Tests       318 passed (318)
exit 0
```

The two pre-existing experimental Node localStorage warnings remain unchanged and are the explicitly deferred Minor from the progress ledger.

Typecheck and production build:

```text
npm run typecheck
exit 0

npm run build
1954 modules transformed
dist/index.html                     0.53 kB | gzip:   0.32 kB
dist/assets/index-BPEUHHmR.css     44.68 kB | gzip:   8.10 kB
dist/assets/index-gX8kAB2V.js       2.02 kB | gzip:   0.99 kB
dist/assets/markdown-BSINYBXa.js  165.72 kB | gzip:  50.46 kB
dist/assets/index-CYvyz2BY.js     413.20 kB | gzip: 128.91 kB
exit 0
```

Pinned-Chromium browser suite:

```text
npm run e2e
30 passed (12.8s)
exit 0
```

Visual/axe/responsive/motion/baseline integrity suite:

```text
npx playwright test e2e/visual.spec.ts
12 passed (8.4s)
exit 0
```

No PNG baseline changed, so there was no changed visual candidate to accept or inspect. All eight approved Darwin baselines passed byte-for-state comparison unchanged.

UI service suite:

```text
cd ui
uv run pytest -v
collected 398 items
398 passed in 8.19s
exit 0
```

Final patch hygiene and forbidden-path checks are recorded after the report in the commit-preparation section below.

## Commit preparation

The final patch passed whitespace validation and the plan's scope guards:

```text
git diff --check
exit 0

changed paths: 13
- 1 required final-review report
- 12 files under ui/frontend/

forbidden changed paths matching .github/, harness/, main.py, or ui/server/: none
unexpected changed paths outside ui/frontend/ and the required report: none
```

The report is intentionally ignored by `.superpowers/sdd/.gitignore`; only this exact required artifact is force-added during commit preparation. No other ignored or untracked artifact is included.

## Residual risks

- The snapshot wire contract has no submission id for the active turn. A client that first connects in the middle of a turn can safely Stop by exact turn id, but cannot invent Retry authority for that pre-existing turn. Previously observed exact turn/submission authority is preserved across reconnect.
- If an exact terminal event is missed, optimistic snapshot reconciliation uses the newly authoritative user-message suffix and submitted text. Normal completion is stronger: exact terminal submission identity removes the projection synchronously. A future protocol-level message/submission correlation field could eliminate the remaining content-based fallback, but adding it here would break the reviewed exact-shape compatibility boundary.
- True workspace relocation remains unavailable until the service provides a relocation operation. Archive is the only honest recovery action; onboarding selection is unaffected.
- The deferred Minors listed in the fix brief, including Node localStorage warnings, remain out of scope and unchanged.
