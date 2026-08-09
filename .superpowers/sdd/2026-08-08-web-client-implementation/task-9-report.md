# Task 9 report — onboarding, settings, credentials, notifications, and recovery

## Scope

Implemented the Task 9 web-client slice only:

- real first-session creation through `POST /api/sessions` with explicit validated workspace, base mode, and one context mode;
- typed credential prerequisite retention and duplicate-safe retry;
- one validated local UI preference owner, internal Settings dialog, future-session defaults, live sidebar preference, and root appearance;
- stable recovery projections, current refresh/reconnect lifecycle, and narrow optional native capabilities;
- background permission/completion notifications with hydration, generation, active-session, preference, and payload guards;
- authenticated health gating before a platform-acquired client becomes connected.

No server API, Tauri host, deployment, Playwright, Task 10, controller ledger, or deferred-Minor work was changed.

## RED evidence

1. Preferences and Settings

   `npm test -- --run src/features/settings/preferences.test.ts src/features/settings/Settings.test.tsx`

   Result before product files: 2 failed suites, 0 tests. Vite could not resolve missing `./preferences` in both suites.

2. Onboarding

   `npm test -- --run src/features/onboarding/Onboarding.test.tsx`

   Result before product component: 1 failed suite, 0 tests. Vite could not resolve missing `./Onboarding`.

3. Explicit session-create authority

   `npm test -- --run src/features/sessions/useSessions.test.tsx`

   Result before hook change: 2 failed, 10 passed. Explicit values were replaced by `/work/acme` + `default` + `compaction`, and an invalid relative workspace dispatched once instead of zero times.

4. Recovery and connection

   `npm test -- --run src/components/RecoveryView.test.tsx src/components/ConnectionStatus.test.tsx`

   Result before product components: 2 failed suites, 0 tests. Both component imports were absent.

5. Native recovery capabilities

   `npm test -- --run src/platform/tauri.test.ts`

   Result before adapter extension: 1 failed. Only `open_logs` was invoked; `restart_service` and `quit` were absent.

6. Notifications

   `npm test -- --run src/features/settings/NotificationObserver.test.tsx`

   Result before observer: 1 failed suite, 0 tests. `./NotificationObserver` was absent.

7. App and live sidebar integration

   `npm test -- --run src/App.test.tsx src/features/sessions/SessionSidebar.test.tsx`

   Result before integration: 3 failed, 21 passed. App did not render first-run workspace input, sidebar/palette did not open internal Settings, and controlled collapse emitted zero callbacks.

8. Server category and turn projection

   `npm test -- --run src/components/RecoveryView.test.tsx`

   Result for the added real server category: 1 failed, 10 passed. `session_resume_error` used generic recovery copy.

   `npm test -- --run src/App.test.tsx -t "projects any transcript turn failure"`

   Result before App projection: 1 failed. A provider category rendered generic recovery instead of the stable turn-failure view.

9. Authenticated health

   `npm test -- --run src/App.test.tsx -t "checks authenticated health"`

   Result before health gating: 1 failed. The first authenticated request was `/api/sessions`, not `/api/health`.

10. App readiness and reconnect ownership

    `npm test -- --run src/App.test.tsx -t "derives zero-session|escalates a current|stale failed refresh"`

    Result before lifecycle integration: 2 failed, 1 passed. Initial zero-session loading did not show `Checking local service`, and current refresh failure did not render reconnecting status/banner/retry. The stale replacement-client case already passed through `useSessions` identity authority.

## GREEN and accessibility evidence

- Preferences/Settings: 2 files, 5 tests passed.
- Onboarding + real session hook: 2 files, 17 tests passed.
- Recovery/Connection/platform: 3 files, 14 tests passed before the server-category addition.
- Notifications: 1 file, 3 tests passed.
- App/sidebar integration: 2 files, 24 tests passed before lifecycle additions.
- App lifecycle targeted gate: 3 passed, 20 skipped.
- Final Task 9 focused command:

  `npm test -- --run src/features/onboarding src/features/settings src/components/RecoveryView.test.tsx src/components/ConnectionStatus.test.tsx src/features/sessions/useSessions.test.tsx src/features/sessions/SessionSidebar.test.tsx src/platform/tauri.test.ts src/App.test.tsx`

  Result: 10 files, 69 tests passed.

- Full frontend command:

  `npm test -- --run`

  Result: 24 files, 265 tests passed.

- TypeScript:

  `npm run typecheck`

  Result: passed with no diagnostics.

- Production build:

  `npm run build`

  Result: passed without warnings. The real split remains: main application `384.06 kB`, markdown vendor `165.72 kB`.

- Accessibility coverage includes axe-clean representative Onboarding, Settings, and Recovery views (with jsdom-incompatible color contrast disabled where required), labelled Radix dialog semantics, focus restoration, non-color connection icons/text, reduced-motion token inheritance, and 44 px primary recovery/settings/onboarding actions.

- `git diff --check` passed.

## Authority notes

- Preferences contain only appearance, future base/context defaults, notification toggles, and sidebar state. They contain no credential, token, transcript, or tool-result data.
- Existing active/running sessions receive no `set_session_mode` or transcript mutation when defaults change.
- Credential retry reuses the exact retained create request; workspace/default edits and client replacement invalidate stale completions.
- `Open logs`, `Restart service`, and `Quit` accept no frontend path or arguments and exist only when the adapter exposes them.
- Current refresh failure reuses `useSessions.refresh`; successful current refresh clears recovery, while stale client completions remain ignored by existing client/epoch authority.

## Fix round 1/5 — official review findings

All five Important findings from `task-9-review.md` are addressed.

### RED checkpoints

1. Authoritative completion notifications

   `npm test -- --run src/features/settings/NotificationObserver.test.tsx`

   Result before the fix: 2 failed, 3 passed. Both `turn_cancelled` and `turn_failed` produced the same unexpected `Work complete · Title background` notification.

2. Exact failed-turn retry

   `npm test -- --run src/components/RecoveryView.test.tsx src/App.test.tsx`

   Result before the fix: 2 failed, 37 passed. `session_resume_error` exposed no Retry button, and App flattened the real resume category to generic turn-stopped copy before it could offer exact replay.

3. Session-operation errors versus transport readiness

   `npm test -- --run src/App.test.tsx src/features/sessions/useSessions.test.tsx`

   Result before the fix: 2 failed, 37 passed. A typed rename failure had no scoped operation result, while a typed New chat failure exposed no operation alert and drove the false local-service connecting state.

4. First-run edit authority

   `npm test -- --run src/App.test.tsx`

   Result before the fix: 2 failed, 26 passed. Both stale-success and stale-failure integration cases retained a disabled `Starting…` button after workspace, permission-mode, and context-mode edits, so the newer request could not start.

5. Recoverable bootstrap acquisition

   `npm test -- --run src/App.test.tsx src/platform/tauri.test.ts`

   Result before the fix: 2 failed, 28 passed. App exposed only terminal `Local service disconnected`, and Tauri's second connection request rejected with the cached first acquisition failure.

### Implemented contracts

- The transcript reducer records exact completed/cancelled/failed terminal identity. Background completion notifications now require a newly observed authoritative `turn_completed`; cancellation and failure never announce success.
- App retains the exact session-scoped `send_message` and replays it only while client, dispatcher, session, generation, sequence, and turn identity still match. Pending retries are duplicate-suppressed, successful submission retires the action, and `session_resume_error` follows the real retry path.
- `useSessions` separates `refreshError` from typed create/rename/archive `operationError`. Only refresh readiness drives reconnect state; scoped mutation recovery preserves `ApiError.category` and retries the exact failed request.
- One opaque create token spans Onboarding and `useSessions`. Editing workspace/mode/context releases only that token's pending slot. A stale failure cannot replace newer state; a stale successful POST is never committed or activated and receives best-effort cleanup of only its exact returned session id.
- Bootstrap uses explicit attempt identity for quiet checking, reconnect escalation, and fresh acquisition plus authenticated-health Retry. Replaced attempts cannot commit, and the Tauri adapter clears a rejected connection promise instead of caching a terminal failure.
- Shared local-only onboarding copy now says “this computer”.

### Final verification after fix round 1

- Focused Task 9 command: 10 files, 81 tests passed.
- Full frontend: 24 files, 279 tests passed.
- `npm run typecheck`: passed with no diagnostics.
- `npm run build`: passed without warnings; main application `389.23 kB`, markdown vendor `165.72 kB`.
- `git diff --check`: passed.
- No browser or Playwright run was used.

## Fix round 2/5 — authoritative queued retry and durable stale-create cleanup

The two remaining Important findings from the fix-round-1 re-review are now
addressed. The narrow server identity seam described below was explicitly
authorized for this round and supersedes the original frontend-only scope note.

### RED checkpoints

1. Exact direct/queued turn binding

   `npm test -- --run src/App.test.tsx src/protocol/reducer.test.ts`

   Initial result: App had 2 failed and 29 passed; the queued-B failure retried
   direct turn A, and clearing B still left a Retry action. The reducer contract
   separately failed 1 of 29 because it exposed no authoritative active turn.
   A later rejection test failed 1 of 33 because the raw Retry exception was
   rendered, and the rejected queue-edit test failed because the prior queued
   candidate was not restored.

2. Stable service authority

   `.venv/bin/pytest -q tests/test_metadata.py::test_service_identity_is_stable_per_database_and_distinct_between_databases tests/test_app_rest.py::test_health_and_config_are_authenticated_and_describe_public_choices`

   Initial result: 2 failed. `MetadataStore` had no stable `service_id`, and the
   authenticated health payload exposed only `{status: "ok"}`.

3. Durable cleanup ledger

   `npm test -- --run src/features/sessions/useSessions.test.tsx`

   Initial result: 2 failed and 13 passed. The model had neither retained
   cleanup failure authority nor an exact manual cleanup retry.

### Implemented contracts

- Reducer state exposes the current authoritative `turn_started` identity and
  clears it at a terminal boundary. App keeps separate direct and queued
  candidates, replaces or clears queued candidates with the real queue
  lifecycle, restores the prior candidate when a queue edit is rejected, and
  binds a candidate only when its own authoritative turn starts.
- Failed-turn Retry requires exact session, client, dispatcher, generation, and
  turn-id agreement. It sends the bound submission once while pending;
  rejected retries restore the same action and every recovery action uses
  stable copy rather than raw host/provider exceptions.
- Each metadata database owns one non-secret UUID `service_id`, stable across
  reopening that database and distinct for another database. Authenticated
  `GET /api/health` exposes only `status` plus that identity; unauthenticated
  access remains rejected.
- The frontend keeps a versioned, bounded cleanup ledger keyed by `service_id`
  and exact session id. A superseded successful create is recorded before its
  exact DELETE is attempted. Failed cleanup remains suppressed from later list
  and selection commits across refresh and remount, and receives a safe,
  duplicate-suppressed `Retry cleanup` action until DELETE or same-authority
  404 confirms absence.
- Unrelated rename/archive operations cannot clear cleanup authority. Service
  replacement activates only the new service's ledger; a late result for A
  cannot hide or delete B, even when B has the same session id. Storage payloads
  contain no connection token, workspace, transcript, or tool content.

### Final verification after fix round 2

- Focused recovery/authority gate: 5 files, 95 tests passed.
- Full frontend: 25 files, 291 tests passed.
- Server metadata + REST gate: 144 tests passed.
- `npm run typecheck`: passed with no diagnostics.
- `npm run build`: passed without warnings; main application `394.57 kB`,
  markdown vendor `165.72 kB`.
- `git diff --check`: passed.
- No browser, Playwright, Tauri host, deployment, Task 10, controller-ledger, or
  deferred-Minor work was performed.
