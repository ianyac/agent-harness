# agent-harness

A teaching agent harness built lesson by lesson in Python 3.14.

## Running

```bash
uv sync
uv run python main.py --workspace /path/to/project
```

Sessions are persisted under `<workspace>/.agent/sessions/` and can be resumed
with `--continue` or `--resume ID`.

## Recoverable context folding

Enable the folding projection instead of whole-history compaction:

```bash
uv run python main.py --workspace /path/to/project --fold-context
```

Tool results are labeled with stable handles such as `m17.r0` (and
`m17.r0.c2` for an ingestion-time chunk). The agent can call `fold` after a
line of work closes, recording the conclusion that replaces the evidence, and
call `unfold` to restore the full span at the context tail. Large batches apply
at a checkpoint when their marked share reaches 15%; smaller batches apply at
the next user-turn boundary. The full transcript remains the immutable shadow
record throughout.

Each session keeps its folding ledger and content-free decision log beside the
transcript as `<session>.folds.sqlite3` and `<session>.fold-decisions.jsonl`.
Resuming the session reuses those files, so its folded footprint and verdicts
survive process restarts.

`--fold-context` and `--compact-threshold` are intentionally mutually
exclusive. Two context managers rewriting the same message array would make
neither one's state reconstructable.
