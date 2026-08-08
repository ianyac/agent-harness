# Second remediation report — C1 and I5

Date: 2026-08-09

Base commit: `c5fd2079025440ac22d3f29b825dd9144bd6f84e`

Implementation commits:

- `837c01c` — `ui: make session ownership replacement-resistant`
- `980f56f` — `ui: roll back metadata on interruptions`

## Outcome

The two findings reproduced by the final re-review are remediated within the
authorized local-service scope.

- C1 now retains a non-path, open-file-description authority for the entire
  secure lease. Replacing both user-writable lock pathnames cannot create a
  second ownership domain.
- I5 now rolls a metadata transaction back for any `BaseException`, re-raises
  the original object, leaves the connection out of a transaction, and permits
  the next mutation.

No root `harness/`, root `tests/`, root dependency/config, or `ui/frontend/`
file changed. The full warning-strict UI suite passes 389/389, and both ordinary
root discovery forms pass 545/545. This report does not claim root
warning-strict cleanliness.

## Root-cause analysis

### C1 — pathname replacement split both flock domains

Both pre-remediation cross-process locks had the same lifecycle:

1. open a user-writable pathname;
2. acquire `flock` on the opened inode;
3. compare the pathname identity with the opened descriptor once; and
4. retain only the descriptor for the rest of the lease.

Unlinking a locked pathname does not invalidate the parent's descriptor or
flock. It does make the next open resolve to a replacement inode. Once both the
public `<session>.lock` and hashed coordination entry were unlinked and
recreated, the child locked two fresh inodes and acquired a second full lease.
The one-time identity checks proved only acquisition-time consistency; there
was no authority common to the parent and child after replacement.

### I5 — the transaction exception boundary was too narrow

`MetadataStore._transaction()` began an immediate transaction but caught only
`Exception`. A custom `BaseException` raised after the SQL write, while the
method constructed its return record inside the context, bypassed both the
rollback handler and the normal commit path. SQLite therefore exposed the
uncommitted mutation on the live connection, kept
`connection.in_transaction == True`, and rejected the next `BEGIN IMMEDIATE`.

## C1 lock design

### Rejected designs

- A third lock file in another user-writable directory was rejected because it
  repeats the same pathname/inode split.
- Periodic pathname revalidation, vnode watching, and heartbeat files were
  rejected because detection races with a new claimant and does not give that
  claimant an authority shared with the unlinked holder.
- A loopback TCP self-connection was prototyped and rejected. Although its
  four-tuple is non-path authority, the finite port namespace introduces
  avoidable collision and local-network availability concerns.
- A process-scoped POSIX record lock was rejected because closing any other
  descriptor for the same carrier inode can release all such locks in the
  process.
- A whole-file lock on a stable carrier was rejected because it would serialize
  unrelated session IDs for the full runtime lifetime.

### Selected design

`_CoordinationLeaseClaim` keeps the existing hardened hashed lock entry for
defense in depth and diagnostics, and additionally holds an exclusive one-byte
Darwin open-file-description lock for the full claim:

- The carrier is `/dev/null`, opened relative to an `O_NOFOLLOW` descriptor for
  `/dev`.
- `/dev` must be a root-owned directory with no group/world write bit.
- `null` must be a root-owned, single-link character device whose opened and
  no-follow anchor identities match.
- The byte offset is a 63-bit SHA-256-derived value over the effective user ID,
  canonical workspace `st_dev`/`st_ino`, and validated session ID.
- `F_OFD_SETLK` binds ownership to the retained open file description. Closing
  an unrelated `/dev/null` descriptor cannot release it.
- The authority descriptor is closed only by coordination release, which is
  still the last step after public-lock, private-stage, and directory cleanup.
  Failed earlier cleanup therefore retains cross-process ownership for retry.
- Descriptor close and process exit release the kernel lock, so normal close
  and crash recovery need no stale-authority deletion protocol.

The Darwin `struct flock` argument uses the platform layout
`off_t l_start`, `off_t l_len`, `pid_t l_pid`, `short l_type`, and
`short l_whence` (`@qqihh`). Acquisition fails closed if the platform OFD
facility or the stable carrier is unavailable or unsafe.

### Security invariant

For one `(effective user, workspace device, workspace inode, session ID)` key,
at most one independent live open file description can own the exclusive byte
range on the validated root-owned carrier. Every full secure lease must hold
that authority until its cleanup finishes.

A same-user unlink/recreate of the public lock and hashed coordination entry
cannot modify or replace `/dev/null` through the root-owned, non-writable
`/dev` directory, cannot unlock another open file description, and cannot
change the deterministic byte offset. A child therefore reaches the same live
kernel range and receives `EAGAIN`/`BlockingIOError` rather than entering a new
ownership domain. After the parent closes the authority descriptor, that same
probe can acquire normally.

The 63-bit key projection has a theoretical collision. A collision, an
unrelated lock on the same carrier byte, or deliberate pre-locking can only
cause a fail-closed denial; it cannot permit two owners. Distinct tested session
IDs retain concurrent full leases.

## I5 transaction design

The transaction context now catches `BaseException`, calls `rollback()`, and
uses bare `raise`. Return-row lookup and construction remain inside each
transaction. The regression asserts object identity on the propagated custom
exception, so swallowing or translating it fails the test.

The coverage includes every mutation that returns a constructed record:

- `create_session`
- `rename_session`
- `set_session_mode`
- `touch_session`
- `archive_session`
- `upsert_discovered_session`
- `set_preference`

Each case performs real SQLite writes and reads. Only the post-write record
constructor is replaced; assertions inspect real row visibility, transaction
state, and a later real mutation.

## TDD evidence

### C1 RED

The existing public-path subprocess regression was expanded before production
changes. It holds a real full secure lease, resolves the live coordination
descriptor's pathname, unlinks and recreates both authoritative user-writable
paths with different inode identities, closes an unrelated `/dev/null`
descriptor, and runs the same child acquisition probe before and after parent
release.

Command:

```text
cd ui
uv run pytest -W error -q tests/test_app_rest.py::test_cross_process_lease_survives_every_lock_path_replacement
```

RED output at `c5fd207` plus the test change:

```text
F                                                                        [100%]
E   AssertionError: ('ACQUIRED\n', '')
E   assert 0 == 3
1 failed in 0.42s
```

The failure was the demonstrated bug: the child exited 0 and printed
`ACQUIRED` while the parent full lease was live.

### C1 GREEN

The identical command after the OFD authority change produced:

```text
.                                                                        [100%]
1 passed in 0.56s
```

Expanded warning-strict ownership, replacement, distinct-session, unsafe-entry,
and cleanup-retry selection:

```text
...........                                                              [100%]
11 passed, 111 deselected in 0.49s
```

### I5 RED

Command:

```text
cd ui
uv run pytest -W error -q \
  tests/test_metadata.py::test_create_session_record_construction_interruption_rolls_back_insert \
  tests/test_metadata.py::test_session_mutation_record_construction_interruption_is_transaction_atomic \
  tests/test_metadata.py::test_preference_record_construction_interruption_is_transaction_atomic
```

RED output before the production change:

```text
FFFFFFF                                                                  [100%]
create: assert 1 == 0
five session siblings: assert not connection.in_transaction (was True)
preference mutation: assert not connection.in_transaction (was True)
7 failed in 0.06s
```

### I5 GREEN

The identical command after changing the rollback boundary produced:

```text
.......                                                                  [100%]
7 passed in 0.03s
```

The full warning-strict metadata file then produced:

```text
..................                                                       [100%]
18 passed in 0.06s
```

## Verification

| Check | Result |
| --- | --- |
| Affected warning-strict focus: full metadata plus ownership/replacement/hardening/retry, manager ghost cleanup, and live-policy rollback | `31 passed in 0.56s` |
| Full UI: `cd ui && uv run pytest -W error -q` | `389 passed in 7.86s` |
| Root ordinary discovery: `uv run pytest -q` | `545 passed in 12.42s` |
| Explicit root scope: `uv run pytest tests -q` | `545 passed in 12.32s` |
| UI compilation: `cd ui && uv run python -m py_compile server/*.py tests/*.py` | exit 0 |
| Crash-release subprocess: child acquires and calls `os._exit(0)`, then parent reacquires | `crash_release=PASS child=ACQUIRED_BEFORE_CRASH` |
| FD/process-claim probe across 50 acquire/close cycles | `before=4`, `after=4`, `process_claims=0` |
| Async task probe after manager create/close | `pending=0` |
| `git diff --check c5fd207..HEAD` | pass |
| Forbidden scope diff: root `harness/`, root `tests/`, `pyproject.toml`, `ui/frontend/` | empty |

Root warning escalation was deliberately not used as a cleanliness claim. The
brief identifies it as untouched baseline debt, and this range has no root
source, test, or configuration change.

## Scope diff and self-review

Implementation changes are limited to:

- `ui/server/sessions.py`
- `ui/tests/test_app_rest.py`
- `ui/server/metadata.py`
- `ui/tests/test_metadata.py`
- this report

The complete `c5fd207..HEAD` implementation diff was read line by line. Mutation
checks performed during self-review:

- Removing the stable authority makes the replacement subprocess print
  `ACQUIRED` and fail.
- Replacing the OFD command with a process-scoped record lock makes the test's
  unrelated `/dev/null` close release authority and fail.
- Omitting the session ID from the authority key makes the distinct-session
  concurrency regression fail.
- Narrowing the transaction handler back to `Exception` makes all seven exact
  interruption regressions fail for visible state or a live transaction.
- Committing instead of rolling back is detected by equality against the
  pre-mutation real record.
- Swallowing or translating the interruption is detected by exception object
  identity assertions.

No unrelated refactor, frontend work, dependency change, root test change,
remote action, or merge action was performed.

## Remaining concerns

- The authority intentionally depends on Darwin's `F_OFD_SETLK` and validated
  `/dev/null` behavior. This service already relies on Darwin-specific
  descriptor-path support; unsupported or unsafe environments fail closed.
- The hash-to-byte projection and unrelated carrier use can theoretically
  cause a false ownership conflict. This is denial-only, not a second-owner
  path.
- Root warning-strict resource-finalizer debt remains outside this remediation
  and is not represented as green here.
