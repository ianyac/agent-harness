# agent-harness

A teaching project: an agent harness built lesson by lesson in Python 3.14
(`uv`, `pytest`). Curriculum state lives in `docs/superpowers/` (specs, stage
plans) and git tags (`lesson-NN` = that lesson's frozen state).

## Active workstreams — ownership map

Three Claude sessions work this repo concurrently. Stay in your lane; edits
outside your paths are forbidden even if technically possible.

| Session | Working dir | Owns (may edit) |
|---|---|---|
| harness (teaching) | main checkout | `harness/`, `tests/`, `main.py`, `conftest.py` |
| ui | `../agent-harness-ui` worktree, `ui/*` branches | `ui/` only |
| overseer (meta) | main checkout | `docs/`, `.github/`, `CLAUDE.md` |

- The UI consumes the harness through its public seams only (`run_turn`,
  `on_tool_call`, the tool registry). If a needed seam doesn't exist, request
  it — via the human — from the harness session; never edit `harness/` from
  the ui session.
- Shared files not listed above (`pyproject.toml`, `uv.lock`, `.gitignore`):
  propose the change to the human, who routes it.

## Workflow rules (learner-operated)

yc is learning the dev workflow. Sessions do NOT run git/gh/GitHub operations
(branch, commit, push, PR, merge, tag) unprompted — supply the exact command,
explain it, and let yc run it. Automate only what yc has already done
manually and explicitly delegates — and every delegated operation must end
with a verification command yc runs himself (a session's report is a claim,
not proof).

- `main` is protected: changes land via branch → PR → green CI → merge.
- Branch names: `lesson-NN` (harness), `ui/<topic>` (ui), `meta/<topic>`
  (overseer).
- Commit style: `lesson N: <what changed>` for lesson work; imperative
  one-liners otherwise.
- Before any PR: `uv run pytest` green locally (default suite runs offline).
