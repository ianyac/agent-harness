# Workflow track — design

Status: ratified in practice 2026-07-05 (this doc records decisions already
enacted; the overseer session teaches, yc operates).

## Goal and operating principles

yc learns the development workflow (git, worktrees, PRs, CI, review,
multi-session coordination) by operating it on the agent-harness project.

- **Workflow as process, not lessons.** Each workflow element arrives when
  the project's own work creates the need for it, then becomes standing
  process.
- **Learner-operated.** yc runs every git/gh/GitHub operation. Sessions
  supply exact commands and explain them. Three lanes per runbook step:
  *you type* / *you click* / *Claude automates*.
- **Manual-first.** Nothing is delegated to a session until yc has done it
  by hand at least once.
- **Delegation requires verification.** Every delegated operation ends with
  a verification command yc runs himself; a session's report is a claim,
  not proof.

## Fleet topology

One shared git database, one checkout per stream, one session per checkout.

| Stream | Directory | Branch namespace |
|---|---|---|
| overseer (meta/teaching) | `agent-harness` (main checkout; hub for pull/merge/worktree ops; holds `main` between meta branches) | `meta/*` |
| harness (curriculum) | `agent-harness-lesson` (worktree) | `lesson-NN` |
| ui | `agent-harness-ui` (worktree) | `ui/*` |

Sessions are keyed to directories; a stream that moves directories gets a
fresh session seeded by a handoff note plus committed artifacts. Sessions
coordinate through artifacts and through yc (the router) — never through
shared mutable context.

## Rails (all enacted 2026-07-05)

- **Remote:** `github.com/ianyac/agent-harness`, public. Precondition for
  publishing was a full-history secret scan (clean; adapter credentials
  live in `~/.codex/auth.json`, outside the repo).
- **CI:** `.github/workflows/ci.yml` — `uv sync` + `uv run pytest` on every
  PR and every push to main, Ubuntu runner (macOS-only sandbox tests skip).
  Job name `test` is the required status check; renaming it breaks
  protection.
- **Ruleset `protect-main`:** PRs required; check `test` required;
  branches must be up to date before merging; force pushes and deletions
  blocked; bypass list empty (binds the admin too). Required approvals: 0 —
  GitHub forbids self-approval and all PRs are authored by yc's account;
  review happens as diff-reading + comments, and the blocking act is the
  merge decision itself.

## Governance

Ownership map (also in CLAUDE.md, which is loaded by every session):

| Stream | Writes |
|---|---|
| harness | `harness/`, `tests/`, `main.py`, `conftest.py`, `docs/superpowers/`, `docs/streams/harness/` |
| ui | `ui/`, `docs/streams/ui/` |
| overseer | `.github/`, `docs/workflow/`, `docs/streams/overseer/` |
| shared-by-PR | `CLAUDE.md` — any stream may edit on a branch; a CLAUDE.md change always travels in its own PR, never inside a feature PR |

- **Mailboxes:** `docs/streams/<stream>/` — each stream's writable outbox
  for handoff notes, seam proposals, contract drafts. Single writer per
  directory; universal read access.
- **Fences:** each checkout carries an untracked
  `.claude/settings.local.json` denying Edit/Write on the other streams'
  lanes and mailboxes. Local by design; recreate when recreating a
  worktree.
- **Layered enforcement:** CLAUDE.md states intent → permission denies gate
  the file tools → yc's PR diff review behind protected main is the
  deterministic backstop (catches Bash side-doors and everything else).
  Hooks (PreToolUse) deliberately deferred to Stage 6 — not for lack of
  value, but because the fences + PR-review backstop already cover the
  risk, and hooks teach best configured in Claude Code the same week the
  harness builds its own `hooks.py`: the same fence built twice, once from
  each side.

## Cross-stream communication

1. yc routes decisions between sessions (decisions are made in the session
   that owns the affected context).
2. Mailbox files for durable proposals and handoffs.
3. Planned: GitHub Issues for seam requests (session drafts body, yc files
   via `gh issue create`, owning session reads via `gh issue view`).
   The UI consumes harness code only through public seams (`run_turn`,
   `on_tool_call`, tool registry); missing seams are requested, never
   self-served.

## Stage mapping (workflow elements onto the curriculum)

- **W0 — rails (2026-07-05):** remote, CI, ruleset, public flip, worktree
  fleet, fences — done; first PR in flight (the one carrying this doc).
- **Stage 4 (L10–11):** the PR loop as routine — branch → PR →
  `/code-review` triage → GitHub diff review → merge → tag. Two full reps.
- **Stage 5 (L12–13):** parallelism — concurrent lesson/ui PRs, deliberate
  merge conflict + rebase practice; annotated tags + GitHub Releases cut by
  an Actions job on `lesson-*` tag push.
- **Stage 6 (L14–15):** both sides of extensibility — repo-local Claude
  Code hooks + a project skill (e.g. `/close-lesson`), Claude GitHub App
  (automated PR review, `@claude` cloud sessions) while the harness builds
  `hooks.py` and skills.

## Standing conventions

- Merge strategy default: **squash** (clean main, honest branch history
  preserved in the PR record). Revisit only if a multi-commit PR's
  structure is worth keeping.
- Commit style: `lesson N: <what>` for lesson work; imperative one-liners
  otherwise.
- `uv run pytest` green locally before any PR.
- History is append-only once pushed: fix forward or revert; rewriting
  published history is out (exceptions: leaked secrets, giant binaries —
  surgical `git-filter-repo`, never restart).

## Out of scope (named, not forgotten)

Deploy-style CD (nothing to deploy), CODEOWNERS/fork flows, ruleset bypass
rules, the parked TS speedrun line.
