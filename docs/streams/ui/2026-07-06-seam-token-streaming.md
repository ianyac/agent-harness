# Seam request: token streaming (`on_text_delta`)

**From:** ui stream
**To:** harness stream, via yc
**Date:** 2026-07-06
**Needed for:** web UI v1 (streams assistant text into the page as it
generates; see `2026-07-06-web-ui-design.md`)

## What the UI needs

A way to observe assistant text incrementally during `run_turn`, instead of
only receiving the complete message.

## Proposed contract (shape is yours to redesign)

- `LLMClient.complete(messages, tools=None, system=None, on_text_delta=None)`
  — new optional parameter, `Callable[[str], None]`. Called zero or more
  times with text chunks as they arrive; `None` (default) preserves today's
  behavior exactly.
- `run_turn(..., on_text_delta=None)` threads the callback through to every
  `complete` call it makes.
- The Codex adapter already consumes the SSE stream internally
  (`_attempt` iterates `response.output_item.done` events); it would
  additionally forward `response.output_text.delta` payloads to the
  callback.

## Contract guarantees the UI relies on

1. Concatenating all chunks passed to `on_text_delta` for one `complete`
   call equals that reply's final `content` (when content is non-None).
   This is the contract-test assertion.
2. Callback exceptions propagate out of `complete` (the UI uses this for
   turn cancellation, same as it does with `on_tool_call`).
3. No delta callbacks for replies with no text content (pure tool-call
   iterations may emit nothing).

## Non-requirements

Reasoning/thinking deltas, tool-call argument deltas, and any buffering
policy are explicitly not needed — text only, chunk size at the adapter's
convenience.
