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
