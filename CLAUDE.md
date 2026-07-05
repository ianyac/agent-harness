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

## Workflow rules (standing delegation — v3, 2026-07-06)

yc focuses on decisions, learning, quizzes, and review. Within a task yc
has assigned, sessions run branch/stage/commit/push/PR-creation and
post-merge cleanup (branch deletion, rebase, worktree hop, stash)
themselves, reporting each operation with a verification command yc may
spot-check. Per-operation verification is relaxed because the PR gate
verifies in aggregate: yc reads the full diff and the CI verdict before
every merge.

Retained by yc, always: PR review, the merge itself, quizzes and tags
(tag follows quiz, on main's squash commit), approval of constitution
changes, anything irreversible beyond the repo. Sessions never merge,
never force-push, never run workflow operations outside an assigned task.
Operations yc has not yet performed manually still arrive as explained
runbooks first (manual-first applies to novel ops only).

- `main` is protected: changes land via branch → PR → green CI (`test`) →
  merge. Default merge strategy: squash (check the title box: `<PR title>
  (#N)`).
- Branch names: `lesson-NN` (harness), `ui/<topic>` (ui), `meta/<topic>`
  (overseer).
- Commit style: `lesson N: <what changed>` for lesson work; imperative
  one-liners otherwise.
- Before any PR: `uv run pytest` green locally (default suite runs offline).
