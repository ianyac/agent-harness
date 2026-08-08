# Request: public stream-reset callback

## Requested public seam

Please add an optional `on_stream_reset: Callable[[], None] | None` callback
to the public signatures of `LLMClient.complete(...)`,
`CodexAdapter.complete(...)`, and `run_turn(...)`.

The callback is a retry-boundary notification for a consumer that has already
rendered streamed text. `CodexAdapter` must invoke it immediately before a
retry attempt begins, and only when the abandoned attempt delivered at least
one non-empty text delta.

Callback exceptions must propagate; this must match the existing cancellation
behavior of `on_text_delta` callback exceptions. In particular, the callback
is not best-effort and must not be swallowed.

No reset must fire for any of the following:

- the first attempt;
- a retry when the abandoned attempt delivered no non-empty text;
- compaction;
- a new tool or model iteration within a single `run_turn(...)` call.

`FakeLLM` must be able to script a reset so downstream protocol tests can stay
offline.

Omitting `on_stream_reset` must preserve every existing caller and existing
behavior.

## Harness-owner test expectations

Please cover the public forwarding path from `run_turn(...)` through
`LLMClient.complete(...)` to `CodexAdapter.complete(...)`; firing immediately
before the qualifying retry; all no-reset cases above; propagation of callback
exceptions; and compatibility when the callback is omitted. Add an offline
`FakeLLM` scripted-reset test for downstream protocol consumers.

## UI acceptance gate

The UI bridge work may proceed only when the public `run_turn(...)` signature
exposes the requested callback:

```python
import inspect

from harness.loop import run_turn

assert "on_stream_reset" in inspect.signature(run_turn).parameters
```

The UI will consume this public seam only. It will not subclass
`CodexAdapter`, call `_attempt`, or duplicate retry logic.
