# Seam request: harness-owned session transcript logs

**From:** ui stream
**To:** harness stream, via yc
**Date:** 2026-07-06
**Needed for:** web UI v1 persistent sessions, reconstructed from harness
logs (see `2026-07-06-web-ui-design.md`). Possibly a natural fit for the
curriculum's planned "persistent sessions" lesson.

## What the UI needs

The harness owns durable transcripts; the UI reconstructs its session list
and history by reading them. Requirements from the consumer side — the
design (module shape, file format details, lesson framing) is yours:

1. **Sessions have ids**, stored under a configurable root directory.
2. **Durability is per completed turn, atomic.** When `run_turn` returns
   normally, that turn's messages are appended to the log; a crashed or
   cancelled turn leaves no trace. This matches main.py's
   rollback-on-interrupt semantics and makes reconstruction unambiguous —
   the UI never has to repair a half-written turn.
3. **Operations:** create a session; list sessions (id, created/updated
   timestamps at minimum); load a session's messages list, ready to hand
   back to `run_turn`.
4. **Format:** JSONL of message dicts is ideal (append-friendly,
   human-inspectable) but any format behind a load API works.
5. **Compaction-aware:** after in-place compaction mutates `messages`, a
   reload must yield the compacted transcript, not the pre-compaction one
   (i.e. the log tracks the list `run_turn` actually maintains).

## Interim plan

Until this lands, the UI backend runs an in-memory `SessionStore` behind
the same interface it will use for the harness-backed version; sessions are
lost on restart. Nothing else in the UI changes when the seam arrives.
