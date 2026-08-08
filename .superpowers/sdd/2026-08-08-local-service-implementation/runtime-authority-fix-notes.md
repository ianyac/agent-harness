# Runtime/authority final-fix evidence

Base: `91de309`

## C1 and I3

Initial RED command:

```text
cd ui && uv run pytest -q \
  tests/test_app_rest.py::test_cross_process_lease_survives_public_lock_replacement \
  tests/test_app_rest.py::test_archive_cancellation_keeps_worker_owned_until_thread_finishes \
  tests/test_app_rest.py::test_archive_close_failure_is_non_reusable_and_retryable
```

Result: `3 failed`. The child printed `ACQUIRED` while the parent lease was
live; archive cancellation cleared the worker handle; partial close removed the
channel while retaining the runtime.

GREEN: the same command passed `3 passed`. The expanded coordination/retry
selection passed `11 passed`, including distinct-session concurrency,
symlink/non-regular rejection, coordination-release retry, and ownership held
through final stage cleanup.

## I4

RED: the active-turn transcript and partial-append recovery selection failed
`2 failed`: the transcript exposed the in-flight user message, and the failed
turn stayed in memory after the injected partial append.

GREEN: the same selection passed `2 passed`. A later turn and a freshly loaded
`SessionLog` contain only the later complete pair.

## I5

RED: metadata create plus the four sibling mutation-return probes failed
`5 failed` because each mutation committed before record construction. The
corrected manager-level ghost probe separately failed with a persisted row.

GREEN: the combined metadata and manager selection passed `6 passed`, with no
row, public JSONL, private stage, or cached runtime after construction failure.

## I6

RED: recursive protocol, malformed provider delta/reply/tool arguments, and
pre-poisoned resume selection failed `7 failed`; the provider cases completed
and the poisoned REST response raised during serialization. The malformed tool
result terminal probe separately failed `1 failed` by completing the turn.

GREEN: the original selection passed `7 passed`; the tool argument/result
selection passed `2 passed`. Unsafe strings now produce UTF-8-safe
`invalid_response` terminals or a typed `session_resume_error`, and no fresh
JSONL is poisoned.

## Focused integration

```text
cd ui && uv run pytest -W error -q \
  tests/test_protocol.py tests/test_bridge.py tests/test_runner.py \
  tests/test_runtime.py tests/test_metadata.py tests/test_app_rest.py \
  tests/test_app_ws.py
```

Result: `250 passed`.
