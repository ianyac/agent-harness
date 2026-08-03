# Lesson 21 — User-Invocable Skills (Slash Commands) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A leading-`/` line at the REPL invokes a skill by name — `parse_slash` extracts `(name, args)`, and the REPL reuses `tools["skill"].execute(name, args)` so the expanded result becomes the user's turn.

**Architecture:** Two tasks. `skills.py` gains a pure `parse_slash`. `main.py`'s REPL gains a slash front-door before `run_turn`. Discovery and `skill_tool` are untouched.

**Tech Stack:** Python 3.14, `uv`, `pytest`, stdlib. No new dependencies.

## Global Constraints

- Python 3.14; `uv run pytest` offline; no new dependencies.
- Commit style `lesson 21: <what>` + `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Reuse `tools["skill"].execute` — do NOT duplicate arg/`` !`cmd` ``/`${SKILL_DIR}`/fork logic.
- The expanded result becomes the user's message; a normal `run_turn` runs. Only a **leading** `/` triggers it. Bare `/` or unknown name lists skills, spends no turn.
- Out of scope: `user-invocable` frontmatter gating, non-skill slash commands, namespacing.

---

### Task 1: `parse_slash` in `skills.py`

**Files:** Modify `harness/skills.py`; Test `tests/test_skills.py`

**Interfaces:**
- Produces: `parse_slash(text: str) -> tuple[str, str] | None` — `'/name args'` → `("name", "args")`; `'/name'` → `("name", "")`; a bare `'/'` (or non-`/` text) → `None`. `args` is the remainder after the first space, stripped.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py`:

```python
from harness.skills import parse_slash


def test_parse_slash_name_and_args():
    assert parse_slash("/commit-style HEAD") == ("commit-style", "HEAD")


def test_parse_slash_name_only():
    assert parse_slash("/x") == ("x", "")


def test_parse_slash_args_is_remainder_after_first_space():
    assert parse_slash("/x  a b  ") == ("x", "a b")


def test_parse_slash_name_is_the_first_token_only():
    assert parse_slash("/write a poem") == ("write", "a poem")


def test_parse_slash_bare_slash_is_none():
    assert parse_slash("/") is None
    assert parse_slash("/   ") is None


def test_parse_slash_non_slash_is_none():
    assert parse_slash("not a command") is None
    assert parse_slash("") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_skills.py -k parse_slash -v`
Expected: FAIL — `parse_slash` undefined.

- [ ] **Step 3: Implement**

In `harness/skills.py` (e.g. after `skills_section`):

```python
def parse_slash(text: str) -> tuple[str, str] | None:
    """Parse a slash command '/name args' into (name, args): the name is the
    first token after the '/', args the stripped remainder. A bare '/' (or any
    text not starting with '/') returns None — not a command."""
    if not text.startswith("/"):
        return None
    rest = text[1:].strip()
    if not rest:
        return None
    name, _, args = rest.partition(" ")
    return name, args.strip()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_skills.py -k parse_slash -v` → PASS (6 tests). Then `uv run pytest tests/test_skills.py` → all green.

- [ ] **Step 5: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 21: add parse_slash for /name args commands

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: REPL slash front-door in `main.py`

**Files:** Modify `main.py`

**Interfaces:**
- Consumes: `parse_slash` (Task 1); `tools["skill"]`, `skills` (already in scope in `main()`).
- Produces: nothing downstream (terminal).

No unit tests (no test imports `main`); verified by diff-read + `uv run pytest` green + `import main` clean + a manual smoke.

- [ ] **Step 1: Import**

Add `parse_slash` to the `harness.skills` import in `main.py`, e.g.:
```python
from harness.skills import (
    Skill,
    cmd_blocks,
    discover,
    has_cmd_blocks,
    parse_slash,
    skill_tool,
    skills_section,
)
```

- [ ] **Step 2: Insert the slash front-door**

In the REPL loop, immediately after the `input()` block and BEFORE the `try:` that calls `run_turn` (currently between `main.py:461` and `main.py:462`), insert:

```python
        if user_input.startswith("/"):
            names = sorted(s.name for s in skills)
            if "skill" not in tools or not names:
                print("(no skills available)")
                continue
            parsed = parse_slash(user_input)
            if parsed is None:  # a bare "/" — list what's callable
                print(f"(skills: {', '.join(names)})")
                continue
            name, args = parsed
            if name not in names:
                print(f"(unknown skill {name!r}; skills: {', '.join(names)})")
                continue
            print(f"(running /{name})")
            # reuse the model-facing tool: args, !`cmd`, ${SKILL_DIR}, fork/inject
            user_input = tools["skill"].execute(name=name, args=args)
```

The existing `run_turn(messages, user_input, …)` below then runs with the
expanded skill result as the user's message. (For a fork skill, `execute`
returns the subagent's answer, which becomes that message.)

Optional startup hint: where the session banner prints (near "(session: …)"),
add — only when `skills` is non-empty —
`print(f"({len(skills)} skills — /name to run one, / to list)")`.

- [ ] **Step 3: Verify**

Run: `uv run pytest` → whole suite green (no test imports `main`).
Run: `uv run python -c "import main"` → clean import.

- [ ] **Step 4: Manual smoke (optional, for the human)**

```bash
mkdir -p /tmp/l21/skills
printf -- '---\nname: greet\ndescription: greet someone\n---\nSay a warm hello to $1.\n' > /tmp/l21/skills/greet.md
uv run python main.py --workspace /tmp/l21   # (adjust the flag to main.py's actual arg)
# at the prompt:  /greet Ada   → the greet body ("Say a warm hello to Ada.") becomes the turn
#                 /            → lists: greet
#                 /nope        → unknown skill 'nope'; skills: greet
```

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "lesson 21: invoke skills from the REPL with /name args

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** `parse_slash` (Task 1) ✓; reuse `execute`, result → user turn (Task 2 Step 2) ✓; bare `/`/unknown/no-skills all list-or-error with no turn (Task 2) ✓; leading-`/` only (`startswith`) ✓; all skills invocable by name (`names` set) ✓; frontmatter gating deferred (Global Constraints) ✓.
- **Placeholder scan:** none — full code per step; the manual smoke is fully scripted (adjust only the workspace flag name).
- **Type consistency:** `parse_slash(text) -> tuple[str, str] | None` used identically in the test and the REPL (`parsed is None` guard, then `name, args = parsed`); `tools["skill"].execute(name=..., args=...)` matches `skill_tool`'s `execute(name, args="")` signature.
