# Task 6 Report — Daily-driver composer

## Outcome

Implemented the editable daily-driver composer and integrated it with the
reviewed session and conversation foundations. The composer stays editable
while a turn runs, supports one controlled queued follow-up, keeps per-turn
`base` / `plan` mode separate from session permission mode, exposes Stop
through an explicit parent callback, and keeps drafts isolated by stable
session id.

No permission cards, plan-review cards, inspector UI, settings UI, recovery
surface, network lifecycle, or model behavior was added.

## Files

Created:

- `ui/frontend/src/features/conversation/Composer.tsx`
- `ui/frontend/src/features/conversation/useDraft.ts`
- `ui/frontend/src/features/conversation/composer.module.css`
- `ui/frontend/src/features/conversation/Composer.test.tsx`
- `.superpowers/sdd/2026-08-08-web-client-implementation/task-6-report.md`

Modified:

- `ui/frontend/src/App.tsx`
- `ui/frontend/src/App.test.tsx`
- `ui/frontend/src/app.css`
- `ui/frontend/src/features/conversation/Conversation.tsx`
- `ui/frontend/src/features/conversation/ConversationSearch.test.tsx`

## TDD evidence

The test reference `writing-good-tests.md` was read before test edits. Tests
assert user-visible behavior and typed boundary events; component tests do not
create sockets, make network calls, or simulate model behavior.

Initial RED:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx
```

Result: 1 failed suite, 0 collected tests. Vite could not resolve the missing
`./Composer` module, which was the intended first failure.

App integration RED:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx src/App.test.tsx
```

Result: 2 failed files. The new App integration test failed because the
`Message` textbox did not exist; the Composer suite still failed at the
missing-module boundary. The two pre-existing App tests passed.

Escape ownership RED added during self-review:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "closes suggestions"
```

Result: 1 failed, 15 skipped. Escape from the focused Plan-mode control did
not focus Stop, proving Escape ownership was still too narrowly attached to
the textarea.

Focused GREEN:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx src/features/conversation/ConversationSearch.test.tsx src/App.test.tsx
```

Result: 3 files passed, 26 tests passed, 0 failed, no warnings.

The focused coverage includes idle send, running queue, queued-message update,
clear-to-draft, blank input, multiline input, Command+Enter, IME composition,
failed-callback retry, per-turn mode, slash suggestions and keyboard selection,
staged Escape behavior, square Stop and stopping announcement, stable-session
draft isolation, debounced text-only restore, completion focus, honest disabled
attachment control, explicit search ownership, typed App routing, and real
Conversation rendering.

## State machine and event decisions

- Idle non-blank submission emits `send_message { text, mode }`.
- Running non-blank submission emits `queue_message { text, mode }`.
- If `TranscriptState.queued` is already present, the queue affordance stays
  singular and the next submission updates that same server-owned slot by
  emitting another `queue_message` rather than creating client-side queue
  entries.
- Clearing the queued follow-up emits `clear_queued_message`, then restores the
  controlled queued text and mode into the editable draft after callback
  success.
- Text is cleared only after the parent callback succeeds. A synchronous or
  asynchronous rejection preserves the draft and exposes Retry with the exact
  original typed event.
- `base` and `plan` exist only in composer state and outbound turn events. The
  composer never emits `set_session_mode`.
- Active-turn Stop is a separate `onStop` callback. App binds it as
  `onStopSession(sessionId)`. The component and App never fabricate a turn id
  and never construct `cancel_turn`; the future socket-owning parent must use
  its authoritative current-turn identity.
- `turn_stopping` is consumed through controlled `stopping` state. Stop becomes
  disabled with the accessible label `Stopping turn`, and the polite live
  region announces `Stopping after current action`.
- A running-to-idle transition focuses the active session's draft textbox.

## Accessibility and interaction decisions

- The textarea remains enabled in all idle/running/stopping states.
- Ordinary Enter remains multiline; Command+Enter is declared with
  `aria-keyshortcuts` and sends or queues only outside IME composition.
- Slash suggestions use listbox/option semantics, active-descendant state, and
  ArrowUp/ArrowDown/Enter keyboard handling while focus stays in the textarea.
- Escape is owned by the composer region: it first dismisses slash suggestions,
  then focuses the visible Stop control from any focused composer control while
  active.
- The active action uses Lucide icons. Stop is a visually square control;
  no emoji, text-symbol icon, custom SVG, or CSS product artwork was added.
- The context attachment control is visibly disabled and named `Attach context
  (coming later)`; no misleading live attachment action is exposed.
- All desktop controls meet the 32 px target minimum. Send and Stop increase to
  44 px in the narrow layout. Existing light/dark tokens are reused, global
  reduced-motion behavior applies, and increased contrast removes the composer
  shadow and strengthens its border.
- Statuses use text and accessible names in addition to color.

## App and Conversation integration

App now accepts a narrow controlled boundary:

```ts
transcriptBySession: Readonly<Record<string, TranscriptState>>
onSessionEvent(sessionId, event)
onStopSession(sessionId)
```

Only the active session's controlled `TranscriptState` is passed to the real
Task 5 `Conversation` and Task 6 `Composer`. App does not create, connect,
subscribe, dispose, or otherwise own a `SessionSocket` in this task.

The deferred Task 5 Command+F Minor is resolved. `Conversation` now defaults to
not owning the global shortcut; ownership must be explicitly granted. App
grants it to its single active Conversation instance. The App regression opens
exactly one conversation search from composer focus, preserves the draft, and
still routes the typed composer event to the active stable session id. A
standalone regression proves a non-owner neither prevents Command+F nor moves
focus from another text-entry surface.

## Draft persistence schema

Drafts are first written synchronously to an in-memory map keyed by stable
session id. A 300 ms debounced convenience backup uses:

```text
key:   agent-harness:draft:<encoded stable session id>
value: {"text":"<draft text>"}
```

The value has exactly one field: draft text. Mode, credentials, tokens,
messages, transcript state, activities, tool data, and safety state are not
persisted. Storage and debounce duration are injectable for deterministic
tests. Malformed or unavailable storage is ignored while the in-memory draft
remains usable.

## Self-review

- Confirmed no `cancel_turn` is created without authoritative turn identity.
- Confirmed queue state is controlled by `TranscriptState.queued` and no second
  client queue is introduced.
- Confirmed callback rejection cannot clear or replace newer draft text.
- Confirmed a session switch cannot leak one session's draft or per-turn mode
  into another.
- Confirmed the persistence payload is text-only.
- Confirmed Command+F ownership is opt-in and only the active App instance is
  opted in.
- Confirmed all new graphical controls use Lucide and the attachment action is
  truthful.
- Confirmed unrelated Task 5 Minors and later-task surfaces were not changed.

## Verification

```text
cd ui/frontend
npm test -- --run
```

Result: 13 files passed, 137 tests passed, 0 failed.

```text
cd ui/frontend
npm run typecheck
```

Result: PASS (`tsc -b --pretty false`).

```text
cd ui/frontend
npm run build
```

Result: PASS; Vite transformed 1,930 modules and produced the production
bundle.

```text
git diff --check
```

Result: PASS with no whitespace errors.

## Concerns

None. The real socket-owning parent still needs to implement
`onStopSession(sessionId)` using its authoritative active turn id; that is an
intentional boundary, not missing behavior inside this task.

## Fix round 1/5 — Important review findings

This round resolves only the four Important findings in `task-6-review.md`.
The three Minor findings (`aria-expanded`, 32 px mode targets, and idle/stopping
Escape behavior) remain intentionally unchanged.

### Focused RED evidence

Queue-clear authority:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "queue clear|delayed clear|rejected queue"
```

Result before implementation: 3 failed, 16 skipped. A delayed clear overwrote
new text/mode, an old-session clear contaminated the newly selected session,
and a rejected clear had no reconciliation/retry path.

Delivery authority:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "deduplicates|newer failed|same-text|prior-session"
```

Result before implementation: 5 failed, 19 skipped. Duplicate Command+Enter
dispatched twice; older success hid the newer Retry; same-text re-edit was
cleared; prior-session success cleared the new session; and prior-session
rejection exposed an error in the new session.

IME suggestion selection:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "slash suggestion with composition"
```

Result before implementation: 1 failed, 24 skipped. Composition Enter selected
the slash command and removed `/pl` instead of leaving composition in control.

Feedback contrast:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "theme-aware error feedback"
```

Result before implementation: 1 failed, 25 skipped because no theme-aware
`--color-danger-text` token existed.

### Authority and reconciliation implementation

- Both delivery and queue-clear operations carry the originating stable
  `sessionId`, session epoch, edit revision, and a monotonic operation id.
  Completion effects require both current-operation identity and matching edit
  authority. Stale success/rejection is ignored.
- The current operation is registered synchronously before calling `onEvent`,
  so repeated Command+Enter and repeated clear actions for the same edit are
  deduplicated at the action boundary without disabling the textarea.
- A successful delayed clear restores queued text/mode only if its authority is
  unchanged. Otherwise it preserves newer input and exposes a per-session
  `Queue reconciliation` action that appends the cleared text without replacing
  the current draft or mode.
- A failed clear stays associated with its originating session and offers an
  exact retry. Switching sessions neither applies nor displays that result in
  the newly selected session.
- Delivery retry stores the exact typed event on the authoritative operation.
  Reverse-order completion, same-text re-edit, and session reuse cannot clear
  or mis-report a newer draft.
- Every Enter action is gated before slash selection or delivery by native IME
  state, the composition ref, and one immediate post-composition Enter guard.

### Contrast evidence

Visible 12 px feedback now uses normal-text-safe theme tokens. The test reads
the shipped token stylesheet and computes WCAG sRGB contrast ratios:

```text
error light:       #9f463f on #ffffff = 6.14:1
error dark:        #d87970 on #24251f = 5.07:1
neutral light:     #6e7067 on #ffffff = 5.03:1
neutral dark:      #a9aba1 on #24251f = 6.64:1
more-contrast light (ink):             14.33:1
more-contrast dark (ink):              13.75:1
```

Failure feedback retains its named live status and uses `data-tone="error"`;
non-error reconciliation feedback uses the existing readable muted-ink token.

### Final GREEN verification

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx src/features/conversation/ConversationSearch.test.tsx src/App.test.tsx
```

Result: 3 files passed, 36 tests passed, 0 failed (Composer 26, App 3,
ConversationSearch 7).

```text
cd ui/frontend
npm test -- --run
```

Result: 13 files passed, 147 tests passed, 0 failed.

```text
cd ui/frontend
npm run typecheck
```

Result: PASS (`tsc -b --pretty false`).

```text
cd ui/frontend
npm run build
```

Result: PASS; Vite transformed 1,930 modules and built the production bundle.
An initial fix-round build exposed a new 500.34 kB main-chunk advisory (the
fresh pre-fix Task 6 build was 497.44 kB). The Vite config now applies a real
Rollup split for the existing `react-markdown`/`remark-gfm` dependency graph;
it does not raise or silence the warning threshold. The final build has a
333.80 kB main chunk and a 165.72 kB markdown chunk, with no warning.

```text
git diff --check
```

Result: PASS with no whitespace errors.

## Fix round 2/5 — App-boundary authority and queue replacement

### Re-review scope

This round addresses the real `App` session-switch boundary: deferred queue
clears and deliveries must retain their originating session authority while
the sidebar selects another session. It also closes the failed-clear retry
case where the controlled queued slot has since changed or disappeared. The
closed IME, contrast, config-split, and deferred minor findings were not
changed.

### RED evidence

The new tests exercise two sessions through the real `App`, `useSessions`,
sidebar, and `Composer`, with deferred operations settled while session B is
active:

```text
cd ui/frontend
npm test -- --run src/App.test.tsx -t "session A|queue clear" --reporter=dot
```

Before the App-boundary fix: 5 failed, 1 passed, 3 skipped. Queue-clear
success/reconciliation/rejection and delivery success/rejection state were
lost on the keyed Composer remount. A same-text A re-edit also lost its stale
delivery protection across A/B/A/B selection.

After correcting the test interaction and completing the implementation, the
same suite was mutation-checked by temporarily restoring the old keyed
Composer remount. It again produced 5 failures, 1 pass, and 3 skips, including
the delivery-rejection status case. This demonstrates that the App-boundary
assertions depend on the production authority lifetime rather than passing
through a false-positive interaction.

Failed-clear replacement RED:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "failed clear when controlled queue|retries a failed clear exactly once" --reporter=dot
```

Before the replacement guard: 1 failed, 1 passed, 26 skipped. Retry dispatched
a second unqualified clear even though the controlled queue no longer matched
the failed operation.

A text-only guard was then mutation-checked against the expanded text/mode/null
table. The mode-only case failed (1 failed, 2 passed, 27 skipped), proving the
comparison must include both exact text and exact mode and must reject a null
queue.

### Implementation

- `App` now keeps one Composer instance across sidebar selection and owns a
  stable per-session draft-memory map. Component-local async operation state is
  no longer destroyed by a session-key remount.
- Composer edit authority, mode, pending operations, delivery errors, and queue
  reconciliations are retained per session. Deferred completion always looks
  up and updates the operation's originating session; session B is isolated
  even when it contains identical text.
- `useDraft` captures the target session before a state update and exposes a
  targeted session setter, preventing a completion from being retargeted by a
  concurrent prop switch.
- A failed-clear retry dispatches exactly once only while the current
  controlled queue is non-null and exactly matches the captured text and mode.
  If it changed or disappeared, retry is non-destructive: it does not dispatch,
  preserves the replacement, and converts the original text into an explicit
  Append/Dismiss reconciliation.

### Final GREEN verification

```text
cd ui/frontend
npm test -- --run src/App.test.tsx -t "session A|queue clear" --reporter=dot
```

Result: 1 file passed, 6 tests passed, 3 skipped, 0 failed.

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx src/App.test.tsx src/features/conversation/ConversationSearch.test.tsx --reporter=dot
```

Result: 3 files passed, 46 tests passed, 0 failed (Composer 30, App 9,
ConversationSearch 7).

```text
cd ui/frontend
npm test -- --run --reporter=dot
```

Result: 13 files passed, 157 tests passed, 0 failed.

```text
cd ui/frontend
npm run typecheck
```

Result: PASS (`tsc -b --pretty false`).

```text
cd ui/frontend
npm run build
```

Result: PASS; Vite transformed 1,930 modules and built in 1.01 seconds. The
main chunk is 334.56 kB (106.96 kB gzip), the markdown chunk is 165.72 kB
(50.46 kB gzip), and the build emitted no warnings.

```text
git diff --check
```

Result: PASS with no whitespace errors. The scope audit contains only the Task
6 report and App/Composer/useDraft production and test files listed above.

## Fix round 3/5 — Remaining stable-owner lifecycle state

### Re-review scope and root causes

This round resolves only the Important stable-Composer lifecycle findings in
`task-6-rereview-round2.md` plus the requested clear-focus architecture audit.
The deferred Minors and Task 7 remain unchanged.

- `useDraft` previously backed up only the active session through an effect;
  targeted inactive writes stopped at the in-memory map.
- Stop failure used one boolean, so an A rejection could appear in B, and an
  older rejection could overwrite a later successful attempt.
- Running history used one boolean without session identity, making running A
  to idle B look like an in-place turn completion.
- Delayed clear success compared the async render closure's old `sessionId`,
  which still equaled A after B became active and therefore focused B's current
  textarea ref.

### RED and slice-level GREEN evidence

Inactive-session persistence:

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx -t "inactive session's" --reporter=dot
```

RED: 2 failed, 30 skipped. After inactive A delivery success, a fresh-memory
remount restored the stale sent text; after inactive A clear success, it
restored empty text instead of the returned queued follow-up.

```text
npm test -- --run src/features/conversation/Composer.test.tsx -t "inactive session's|debounces a text-only backup" --reporter=dot
```

GREEN: 3 passed, 29 skipped, including the pre-existing active backup case.

Stop authority:

```text
npm test -- --run src/App.test.tsx src/features/conversation/Composer.test.tsx -t "Stop failure|older Stop" --reporter=dot
```

RED: 2 failed, 41 skipped. B announced A's deferred rejection, and an older
rejection overwrote the result of a later successful Stop attempt.

```text
npm test -- --run src/App.test.tsx src/features/conversation/Composer.test.tsx -t "Stop failure|older Stop|square Stop" --reporter=dot
```

GREEN: 3 passed, 40 skipped, including the existing Stop/stopping behavior.

Running-transition focus:

```text
npm test -- --run src/App.test.tsx -t "switching from running session A" --reporter=dot
```

RED: 1 failed, 10 skipped. Selecting idle B moved focus from B's sidebar
button to B's textarea because the global history reported a false completion.

```text
npm test -- --run src/App.test.tsx src/features/conversation/Composer.test.tsx -t "switching from running session A|running turn completes" --reporter=dot
```

GREEN: 2 passed, 42 skipped. The real App switch retains sidebar focus while
an in-place running-to-idle transition still focuses the active textarea.

Clear-success focus:

```text
npm test -- --run src/App.test.tsx src/features/conversation/Composer.test.tsx -t "clear succeeds after switching|returns its text" --reporter=dot
```

RED: 1 failed, 1 passed, 43 skipped. Delayed A clear success focused B's
textarea; the active-A clear characterization already passed.

After the current-active identity fix, the same command passed 2 tests with 43
skipped. Active A retains its intended focus behavior, while inactive A cannot
move focus in B.

### Implementation

- `useDraft` owns a map of backup timers keyed by stable session id. Active and
  targeted setters update that session's memory and schedule the same debounced
  writer. A new write replaces only that session's timer; switching sessions
  leaves other pending timers intact. Timers write the encoded storage key and
  exact JSON `{text}` payload under storage-error tolerance, remove themselves
  only while current, and all timers are cleared on hook cleanup or backup
  configuration replacement.
- Stop attempts have monotonic operation ids and current-operation identity per
  session. Only the latest completion may change that session's error Set, and
  the live region derives its message from the active session's entry.
- Running history is keyed per session and paired with previous-active session
  identity. Focus requires a true-to-false transition observed without an
  intervening active-session switch.
- Delayed clear success reads a mutable current-active-session ref after the
  await and focuses only when that identity still matches the originating
  operation.

### Final GREEN verification

```text
cd ui/frontend
npm test -- --run src/features/conversation/Composer.test.tsx src/App.test.tsx src/features/conversation/ConversationSearch.test.tsx --reporter=dot
```

Result: 3 files passed, 52 tests passed, 0 failed (Composer 33, App 12,
ConversationSearch 7).

```text
npm test -- --run --reporter=dot
```

Result: 13 files passed, 163 tests passed, 0 failed.

```text
npm run typecheck
```

Result: PASS (`tsc -b --pretty false`).

```text
npm run build
```

Result: PASS; Vite transformed 1,930 modules and built in 1.01 seconds. The
main chunk is 335.28 kB (107.16 kB gzip), the markdown chunk is 165.72 kB
(50.46 kB gzip), and the build emitted no warnings.

```text
git diff --check
```

Result: PASS with no whitespace errors. The pre-report scope audit contained
only App/Composer tests and Composer/useDraft product files; this report is the
only documentation file added to the round's diff.
