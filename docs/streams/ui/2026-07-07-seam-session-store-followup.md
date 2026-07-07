# Follow-up: lesson 13's SessionLog looks like seam 2

**From:** ui stream
**Date:** 2026-07-07
**Re:** `2026-07-06-seam-session-store.md`

Lesson 13 (`harness/session.py`, merged as #8) ships `SessionLog` with
`load()`, `record_turn(messages)`, `record_compaction(cut, summary)` and a
pid-aware lock — per-turn durable JSONL with a load API, which covers the
seam-2 requirements as requested (per-turn atomicity, compaction-aware,
load-ready message lists).

Remaining gap vs. the request: session *enumeration* (list sessions +
created/updated metadata) — main.py appears to manage one log per session
path; the web UI needs to discover sessions in a directory. That may be
UI-side glue (scan the log dir) rather than a harness change; no new seam
request unless the harness stream prefers to own it.

Planned adoption (follow-up PR, not in web UI v1): replace
`ui/server/store.py`'s `InMemorySessionStore` with a `SessionLog`-backed
store behind the same surface; wire `record_turn` after each completed
turn. The lesson-13 subagent change (no asker; ask→deny inside subs) is
already adopted on the v1 branch (`ui: adapt to lesson 13`).
