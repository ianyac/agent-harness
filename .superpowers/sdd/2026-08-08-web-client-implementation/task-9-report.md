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
