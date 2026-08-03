# User-Invocable Skills — Slash Commands (Lesson 21)

**Date:** 2026-07-10
**Status:** Approved pending user review

**Context:** Extends the skill-tool arc (18–20). Skills are model-invoked via the
`skill` tool; this lesson adds the *other* door — the human types
`/skill-name args` at the prompt to invoke a skill directly. Teaches **dual
invocation** (model *and* user), the distinction raised back in the first skills
discussion.

## Goal

At the REPL, a line beginning with `/` invokes a skill by name: the skill is
expanded (args + `` !`cmd` `` + `${SKILL_DIR}` + fork/inject — the *same*
`skill_tool.execute` the model uses) and the result becomes the user's message
for the turn, so the model acts on it. A bare `/` or an unknown name lists the
available skills.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Expansion path | Reuse `tools["skill"].execute(name, args)` — the hook-wrapped tool | DRY: args, `` !`cmd` ``, `${SKILL_DIR}`, fork-vs-inject all already live there. A slash command is just "the user, not the model, picked the skill + args" |
| Result handling | The expanded result becomes the **user's message**; a normal `run_turn` runs | Matches Claude Code — the command's content becomes the prompt and the model acts. A fork skill's `execute` returns the subagent's answer, which the main model then continues from — uniform, no special-casing |
| Where it lives | In `main.py`'s REPL loop, **not** a registered tool | It's a *user* front door, not a model-callable tool; the model already has the `skill` tool |
| Parsing | `parse_slash(text) -> (name, args) \| None`: `name` = first token after `/`, `args` = the rest (one string); bare `/` → `None` | A small pure function, unit-tested; the REPL does resolution + IO |
| Which skills | All discovered skills, by name | No `user-invocable`/`disable-model-invocation` frontmatter gating this lesson (deferred, like Claude Code's controls) |
| Unknown name / bare `/` | Print the available skill names, spend no turn | Doubles as `/help` — a bare `/` or a typo lists what's callable |
| No skills | A `/…` line when no skill tool is registered prints "no skills available", no turn | Graceful when `skills == []` |

## Components

- **`harness/skills.py`**: add `parse_slash(text: str) -> tuple[str, str] | None`
  — pure. `'/name args'` → `("name", "args")`; `'/name'` → `("name", "")`; a bare
  `'/'` (or `'/   '`) → `None`; non-`/` input → `None`. `args` is the remainder
  after the first space, stripped.
- **`main.py`** (the REPL, ~lines 457-463): before `run_turn`, if `user_input`
  starts with `/`:
  ```
  parsed = parse_slash(user_input)
  if "skill" not in tools or parsed is None:
      print available skills (or "no skills available"); continue   # no turn
  name, args = parsed
  if name not in {s.name for s in skills}:
      print(f"unknown skill {name!r}; available: …"); continue       # no turn
  print(f"(running /{name})")
  user_input = tools["skill"].execute(name=name, args=args)          # expand → the turn input
  # falls through to the existing run_turn(messages, user_input, …)
  ```
- Optional startup hint when `skills` is non-empty: `(N skills — /name to run, / to list)`.

Unchanged: `skill_tool`/`skills.py` discovery + `execute`, the tool registry, `run_turn`.

## Data flow

```
You: /commit-style HEAD
  → parse_slash → ("commit-style", "HEAD")
  → tools["skill"].execute(name="commit-style", args="HEAD")
      → substitute_args + !`cmd` + ${SKILL_DIR}  (inject skill → the body)
  → user_input = "<expanded commit-style body>"
  → run_turn(messages, user_input, …)  → the model follows the instructions
```

A fork skill (`/research pdfs`) → `execute` runs the subagent → returns its
answer → that becomes the turn's user message → the main model continues from it.

## Error handling

- Non-`/` input → untouched (normal turn).
- Bare `/` → list available skills, no turn.
- Unknown skill name → error + available list, no turn.
- No skill tool registered (`skills == []`) → "no skills available", no turn.
- The skill's own errors (unknown-name inside `execute`, a failing `` !`cmd` ``)
  are already handled by `execute` and would ride into the turn as text — but the
  REPL's pre-check on `name` means `execute` is only called for a known skill.

## Testing

`tests/test_skills.py` — `parse_slash`:
- `'/commit-style HEAD'` → `("commit-style", "HEAD")`.
- `'/x'` → `("x", "")`.
- `'/x  a b  '` → `("x", "a b")` (remainder after the first space, stripped).
- `'/'` and `'/   '` → `None`.
- `'not a command'` and `''` → `None`.
- `'/write a poem'` → `("write", "a poem")` (name is the first token only).

The `main.py` REPL wiring is diff-reviewed (no test imports `main`), but the
parse logic it leans on is unit-tested, and a manual smoke drives it end-to-end.

## Out of scope (deferred)

- `user-invocable` / `disable-model-invocation` frontmatter gating.
- Slash-command argument autocomplete / a `/help` with descriptions.
- Non-skill slash commands (e.g. `/clear`, `/model`).
- Namespaced skills (`/plugin:skill`).
