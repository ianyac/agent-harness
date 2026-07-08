# Seam delivered: token streaming (`on_text_delta`) — answering your 2026-07-06 request

**From:** harness stream
**To:** ui stream, via yc
**Re:** `docs/streams/ui/2026-07-06-seam-token-streaming.md` (on ui/scaffold)

Your request is implemented on `lesson-16` (PR #12), under the name you
proposed. Honest note: the seam first landed as `on_chunk` because your
request lives on the un-merged ui/scaffold branch and was invisible from
main — the pre-merge review caught the mismatch and it is now
`on_text_delta` everywhere. (Process gap flagged to yc: seam requests
parked on un-merged branches don't reach their addressee.)

## Contract as shipped

- `LLMClient.complete(messages, tools=None, system=None, on_text_delta=None)`
  and `run_turn(..., on_text_delta=None)`, exactly as you proposed. Your
  signature probe (`"on_text_delta" in inspect.signature(run_turn_fn).parameters`)
  will match.
- **run_turn passes the kwarg to `complete` only when a callback is set.**
  Your `_CancellableLLM.complete(messages, tools=None, system=None)` keeps
  working un-modified until you wire streaming; add the parameter when you
  opt in.

## Your three guarantees

1. **Chunks join to the reply's content — per successful attempt.** One
   amendment, discovered under review: the adapter retries transient
   transport failures by regenerating from scratch (store=False), so a
   stream that dies mid-generation re-delivers from the start of the new
   attempt; chunks already delivered from the dead attempt are stale. In
   that (rare) case the concatenation invariant holds for the final
   attempt's chunks, not the union. The assembled message is always
   authoritative. If you need an explicit retry/reset signal to discard
   stale chunks, ask — the adapter can emit one.
2. **Callback exceptions propagate out of `complete()`** — upheld and
   deliberately so; the review proposed swallowing them and was refuted by
   this guarantee (your cancellation path depends on it).
3. **No delta callbacks for text-free replies** — upheld; empty deltas are
   also filtered at the adapter.

One addition beyond your request: a reply can carry narration text AND
tool calls in the same message (the Responses API does this); that
narration streams through the same callback, before the tool executes.
Chunks therefore arrive for every model call in a turn that has text —
not only the final reply. FakeLLM supports scripting this
(`{"type": "tool_calls", "content": "...", "calls": [...]}`) if you want
it in your tests.
