# agent-harness

A teaching project: an agent harness built lesson by lesson in Python 3.14
(`uv`, `pytest`). Curriculum state lives in `docs/superpowers/` (specs, stage
plans) and git tags (`lesson-NN` = that lesson's frozen state). Workflow
governance lives in `docs/workflow/`.

## Active workstreams — ownership map

Three Claude sessions work this repo concurrently, one per checkout. Write
only inside your lane; read anywhere.

| Session | Working dir | Owns (may write) |
|---|---|---|
| harness (teaching) | `../agent-harness-lesson` worktree, `lesson-NN` branches | `harness/`, `tests/`, `main.py`, `conftest.py`, `docs/superpowers/`, `docs/streams/harness/` |
| ui | `../agent-harness-ui` worktree, `ui/*` branches | `ui/`, `docs/streams/ui/` |
| overseer (meta) | main checkout (hub), `meta/*` branches | `.github/`, `docs/workflow/`, `docs/streams/overseer/` |

- **CLAUDE.md is shared-by-PR:** any session may edit it on a branch, but a
  CLAUDE.md change always travels in its own PR — never inside a feature
  PR — so constitution changes get undiluted review.
- **Mailboxes:** `docs/streams/<stream>/` is that stream's writable outbox
  for handoff notes, seam proposals, and contract drafts. One writer per
  mailbox; everyone reads everything.
- The UI consumes the harness through its public seams only (`run_turn`,
  `on_tool_call`, the tool registry). Missing seams are requested — routed
  through the human — never self-served by editing `harness/`.
- Shared files with no owner (`pyproject.toml`, `uv.lock`, `.gitignore`):
  propose to the human, who routes the change.
- Each checkout carries an untracked `.claude/settings.local.json` denying
  writes outside its lane. Recreate it when recreating a worktree.

## Workflow rules (full delegation — v4, 2026-07-06)

yc does judgment only: design decisions, approvals (PR merge,
constitution changes), quizzes, reading code, reviewing PRs. Everything
procedural is the sessions' job within an assigned task:
branch/stage/commit/push/PR-creation, post-merge cleanup (branch
deletion, worktree hop, prune, stash), and tagging — after yc declares a
quiz passed, the lesson session tags main's squash commit `lesson-NN`
and pushes it. Cleanup belongs to the session that created the PR —
whoever opens the loop closes it. Each operation is reported with a
verification command yc may spot-check; the PR gate verifies in
aggregate (yc reads the full diff and CI verdict before every merge).

Operating rhythm (all sessions):
- Rebase your branch onto main at task start and again before any
  push or PR.
- Never rebase, merge, or pull a squash-merged branch — it is a husk;
  hop to the next branch off `origin/main` and delete it.
- Conflicts: resolve mechanical ones (imports, adjacent edits) and say
  so in the op report; escalate semantic ones (both streams changed
  what a seam means) to yc — that is a decision in conflict clothing.
- `--force-with-lease` only, only on your own PR branch, only after a
  mid-PR rebase. Never force-push anything shared.

Hard limits, unchanged: sessions never merge PRs, never run workflow
operations outside an assigned task, and surface anything irreversible
beyond the repo before acting.

- `main` is protected: changes land via branch → PR → green CI (`test`) →
  merge. Default merge strategy: squash (check the title box: `<PR title>
  (#N)`).
- Branch names: `lesson-NN` (harness), `ui/<topic>` (ui), `meta/<topic>`
  (overseer).
- Commit style: `lesson N: <what changed>` for lesson work; imperative
  one-liners otherwise.
- Before any PR: `uv run pytest` green locally (default suite runs offline).
