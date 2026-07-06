# Seam change: `run_turn` may rewrite the messages list (lesson 11)

With lesson 11, `run_turn` gained optional compaction:
`compact_threshold`, `keep_recent`, `on_compact`, `breadcrumbs`.

**Contract change when compaction is enabled** (`compact_threshold` set):
the messages list is rewritten **in place** (`messages[:] = compacted`)
whenever the token estimate crosses the threshold — checked before every
model call in the turn, so it can happen mid-turn. Consequences:

- Indices saved before `run_turn` are NOT valid afterwards. The old
  cancel pattern — save `start = len(messages)`, on interrupt
  `del messages[start:]` — can truncate inside the rewritten region and
  leave a dangling assistant `tool_calls` message, poisoning every later
  request.
- Roll back **structurally** instead: pop messages until the list ends
  with a plain assistant message (no `tool_calls`). That boundary — "just
  after a completed exchange" — survives compaction by construction.
  `main.py`'s REPL loop shows the reference implementation.
- `on_compact(n)` fires with the number of messages replaced, only when a
  compaction actually happened.

With `compact_threshold=None` (the default), nothing changes: `run_turn`
is append-only exactly as before. Existing consumers are unaffected until
they opt in.
