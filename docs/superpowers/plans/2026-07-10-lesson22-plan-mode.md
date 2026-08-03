# Lesson 22 — Plan Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/plan [task]` slash command runs a per-turn plan gate — mutating tools denied, read-only allowed. The model calls `exit_plan_mode(plan)`; on the human's approval the policy restores to `base_mode` and the model acts in the same turn. Plan mode never survives a turn.

**Architecture:** Three tasks. `permissions.py` gains the `"plan"` mode + a `base_mode`. A new `exit_plan_mode` tool + a `PLAN_MODE` prompt section. `main.py` wires the `/plan` built-in, a top-of-loop reset, the tool, and the dynamic prompt.

**Tech Stack:** Python 3.14, `uv`, `pytest`, stdlib. No new dependencies.

## Global Constraints

- Python 3.14; `uv run pytest` offline; no new dependencies.
- Commit style `lesson 22: <what>` + `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Plan mode is **per-turn** (non-sticky); restore target is `policy.base_mode` (captured at construction). `exit_plan_mode` no-ops outside plan mode and is `read_only` + `spawns_subagents` (top-level only).
- Out of scope: persistent plan mode across turns, a rendered plan artifact, other built-in slash commands.

---

### Task 1: `permissions.py` — the `plan` mode + `base_mode`

**Files:** Modify `harness/permissions.py`; Test `tests/test_permissions.py`

**Interfaces:**
- Produces: `MODES` includes `"plan"`. `PermissionPolicy(mode)` sets `self.base_mode = mode`. `decide` returns `"deny"` for a non-read-only tool in `"plan"` mode (read-only still `"allow"`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_permissions.py` (use the file's existing `Tool` construction style; a minimal read-only and a minimal mutating tool):

```python
from harness.permissions import MODES, PermissionPolicy
from harness.tools.base import Tool


def _tool(name, read_only):
    return Tool(name=name, description="d",
                parameters={"type": "object", "properties": {}},
                execute=lambda: "x", read_only=read_only)


def test_plan_is_a_valid_mode_and_base_mode_is_recorded():
    assert "plan" in MODES
    p = PermissionPolicy("plan")
    assert p.mode == "plan" and p.base_mode == "plan"
    assert PermissionPolicy("default").base_mode == "default"


def test_plan_mode_denies_mutating_tools_and_allows_read_only():
    p = PermissionPolicy("plan")
    assert p.decide(_tool("write_file", read_only=False)) == "deny"
    assert p.decide(_tool("read_file", read_only=True)) == "allow"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_permissions.py -k "plan" -v`
Expected: FAIL — `"plan"` not in `MODES` (ValueError in the constructor) / no `base_mode`.

- [ ] **Step 3: Implement**

In `harness/permissions.py`:
```python
MODES = ("default", "acceptAll", "readOnly", "plan")
```
In `__init__`, after `self.mode = mode`, add:
```python
        self.base_mode = mode  # the mode to restore to when leaving plan mode
```
In `decide`, change the readOnly case to also cover plan:
```python
            case "readOnly" | "plan":
                return "deny"
```

- [ ] **Step 4: Run to verify pass + no regression**

Run: `uv run pytest tests/test_permissions.py -v` → PASS (new tests + every existing permissions test; `base_mode` is additive, the `plan` case only adds a mode).

- [ ] **Step 5: Commit**

```bash
git add harness/permissions.py tests/test_permissions.py
git commit -m "lesson 22: add the plan permission mode and base_mode

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `exit_plan_mode` tool + `PLAN_MODE` prompt

**Files:** Create `harness/tools/plan.py`; Modify `harness/prompts.py`; Test `tests/test_plan.py`

**Interfaces:**
- Consumes: `PermissionPolicy.base_mode` (Task 1).
- Produces: `exit_plan_mode_tool(policy, approve) -> Tool` — `approve: Callable[[str], tuple[bool, str]]`. `harness.prompts.PLAN_MODE: str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plan.py`:

```python
from harness.permissions import PermissionPolicy
from harness.tools.plan import exit_plan_mode_tool


def test_approved_plan_restores_base_mode():
    policy = PermissionPolicy("default")
    policy.mode = "plan"
    tool = exit_plan_mode_tool(policy, approve=lambda plan: (True, ""))
    result = tool.execute(plan="1. do the thing")
    assert policy.mode == "default"  # restored to base
    assert "approved" in result.lower()


def test_rejected_plan_stays_in_plan_and_returns_feedback():
    policy = PermissionPolicy("default")
    policy.mode = "plan"
    tool = exit_plan_mode_tool(policy, approve=lambda plan: (False, "also update tests"))
    result = tool.execute(plan="p")
    assert policy.mode == "plan"
    assert "also update tests" in result


def test_exit_plan_mode_is_a_noop_outside_plan_mode():
    policy = PermissionPolicy("default")  # not in plan
    calls = []
    tool = exit_plan_mode_tool(policy, approve=lambda plan: calls.append(plan) or (True, ""))
    result = tool.execute(plan="p")
    assert calls == []                 # approve never consulted
    assert policy.mode == "default"
    assert "not in plan mode" in result.lower()


def test_exit_plan_mode_flags():
    tool = exit_plan_mode_tool(PermissionPolicy("default"), approve=lambda p: (True, ""))
    assert tool.read_only is True and tool.spawns_subagents is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_plan.py -v`
Expected: FAIL — `harness.tools.plan` does not exist.

- [ ] **Step 3: Implement**

Create `harness/tools/plan.py`:
```python
from typing import Callable

from harness.permissions import PermissionPolicy
from harness.tools.base import Tool


def exit_plan_mode_tool(
    policy: PermissionPolicy,
    approve: Callable[[str], tuple[bool, str]],
) -> Tool:
    """Present a plan and, on the human's approval, leave plan mode. `approve`
    is given the plan text and returns (approved, feedback). Approval restores
    policy.base_mode so the model may act in the same turn; rejection returns
    the feedback and stays in plan mode. A no-op outside plan mode."""

    def execute(plan: str) -> str:
        if policy.mode != "plan":
            return "Not in plan mode — this tool only applies while planning."
        approved, feedback = approve(plan)
        if approved:
            policy.mode = policy.base_mode
            return "Plan approved. Proceeding — you may now take the actions above."
        return f"Plan not approved; stay read-only and revise. Feedback: {feedback or '(none)'}"

    return Tool(
        name="exit_plan_mode",
        description=(
            "Call this once you have finished investigating and have a complete "
            "plan. Pass the plan as `plan`. The user reviews it; if they approve "
            "you leave plan mode and may act. Until then you are read-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "Your complete plan for the user to review."}
            },
            "required": ["plan"],
        },
        execute=execute,
        read_only=True,          # presenting a plan is read-only; the flip is user-consented
        spawns_subagents=True,   # top-level only — a subagent must not exit plan mode
    )
```

In `harness/prompts.py`, add a module-level constant (near the top, after imports):
```python
PLAN_MODE = (
    "Plan mode: you are investigating and proposing, not acting. Use only "
    "read-only tools (read files, list directories, search) — do NOT modify "
    "files, run mutating commands, or take any action; those are denied. When "
    "you have a complete plan, call exit_plan_mode with it and wait for the "
    "user to approve before doing anything."
)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_plan.py -v` → PASS (4 tests). Then `uv run pytest` → whole suite green.

- [ ] **Step 5: Commit**

```bash
git add harness/tools/plan.py harness/prompts.py tests/test_plan.py
git commit -m "lesson 22: exit_plan_mode tool and the PLAN_MODE prompt section

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `main.py` — wire `/plan`, the reset, the tool, the dynamic prompt

**Files:** Modify `main.py`

**Interfaces:**
- Consumes: `PermissionPolicy.base_mode`, `exit_plan_mode_tool`, `PLAN_MODE`, `parse_slash`, `policy`, `tools`, `skills`.
- Produces: nothing downstream (terminal).

No unit tests (no test imports `main`); verified by diff-read + `uv run pytest` green + `import main` clean + a manual smoke.

- [ ] **Step 1: Imports**

`main.py`: add `PLAN_MODE` to the `harness.prompts` import (`from harness.prompts import Environment, PLAN_MODE, build_system_prompt`); add `from harness.tools.plan import exit_plan_mode_tool`.

- [ ] **Step 2: Register `exit_plan_mode` + `approve_plan`**

Near the skill wiring (before `with_hooks(tools, …)`), add the tool unconditionally, and define `approve_plan`:
```python
    def approve_plan(plan: str) -> tuple[bool, str]:
        print("Proposed plan:\n" + plan)
        if not sys.stdin.isatty():
            return False, ""  # no human to consent
        try:
            answer = input("approve this plan? [y]es / [n]o: ").strip().lower()
        except EOFError:
            return False, ""
        if answer in ("y", "yes"):
            return True, ""
        feedback = input("feedback for the revision (optional): ").strip()
        return False, feedback

    tools["exit_plan_mode"] = exit_plan_mode_tool(policy, approve_plan)
```
(`sys` is already imported in `main.py`.)

- [ ] **Step 3: Top-of-loop reset (the per-turn invariant)**

Before the REPL `while True:`, add `plan_armed = False`. As the FIRST statements inside the loop (before the `input()` try):
```python
    while True:
        policy.mode = "plan" if plan_armed else policy.base_mode
        plan_armed = False
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
```

- [ ] **Step 4: Restructure the slash block — built-ins before skills**

Replace the current slash block (`main.py:465-480`, `if user_input.startswith("/"):` … through the `execute` line) with:
```python
        if user_input.startswith("/"):
            parsed = parse_slash(user_input)
            names = sorted(s.name for s in skills)
            if parsed is None:  # a bare "/" — list what's callable
                print(f"(commands: /plan; skills: {', '.join(names) or 'none'})")
                continue
            name, args = parsed
            if name == "plan":  # built-in — resolves before skill names, works with 0 skills
                if args:
                    policy.mode = "plan"       # run this turn in plan mode
                    user_input = args
                else:
                    plan_armed = True          # arm the next turn
                    print("(plan mode armed — your next message runs in plan mode)")
                    continue
            elif "skill" not in tools or name not in names:
                print(f"(unknown skill {name!r}; skills: {', '.join(names) or 'none'})")
                continue
            else:
                print(f"(running /{name})")
                user_input = tools["skill"].execute(name=name, args=args)
```

- [ ] **Step 5: Make the system prompt include `PLAN_MODE` in a plan turn**

In the `run_turn(...)` call, change the `system=` argument (currently `system=current_system_prompt(workspace, context_sections),`) to:
```python
                system=current_system_prompt(
                    workspace,
                    context_sections + ([PLAN_MODE] if policy.mode == "plan" else []),
                ),
```

- [ ] **Step 6: Verify**

Run: `uv run pytest` → whole suite green (no test imports `main`).
Run: `uv run python -c "import main"` → clean import.

- [ ] **Step 7: Manual smoke (optional, for the human)**

```bash
uv run python main.py --workspace /tmp   # (adjust the flag to main.py's actual arg)
# You: /plan add a hello function to notes.txt
#   → model explores read-only; a write is DENIED; it calls exit_plan_mode; you approve;
#     it then writes (default-mode prompt) — all one turn.
# You: (next message)  → runs in normal mode (plan mode did not persist)
# You: /plan            → "(plan mode armed …)"; your next message runs in plan mode
```

- [ ] **Step 8: Commit**

```bash
git add main.py
git commit -m "lesson 22: wire the /plan slash command and plan-mode prompt

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Reviewer notes (diff-read, Task 3)

- The top-of-loop `policy.mode = "plan" if plan_armed else policy.base_mode` is the per-turn invariant: a `/plan`-with-no-args `continue`s (skipping the turn) after setting `plan_armed`, so the NEXT iteration's top puts the mode in plan; every other turn starts at `base_mode`. `exit_plan_mode` restoring `base_mode` mid-turn lets the model act in the same turn.
- `/plan` is checked before skill resolution and independently of `"skill" not in tools`, so it works with zero skills. A skill literally named `plan` is shadowed (acceptable; noted).
- `exit_plan_mode` is registered unconditionally (any turn may become a plan turn) but no-ops when `policy.mode != "plan"`; the prompt only invokes it during a plan turn.

## Self-Review

- **Spec coverage:** `plan` mode + `base_mode` + deny (Task 1) ✓; `exit_plan_mode` restore/reject/no-op + flags (Task 2) ✓; `PLAN_MODE` section (Task 2) ✓; `/plan [task]` built-in + arm-next + per-turn reset (Task 3 Steps 3-4) ✓; dynamic prompt (Task 3 Step 5) ✓; `approve_plan` non-tty → not approved (Task 3 Step 2) ✓.
- **Placeholder scan:** none — full code per step; the smoke is scripted (adjust only the workspace flag name).
- **Type consistency:** `approve: Callable[[str], tuple[bool, str]]` identical in the tool signature, the tests' fakes, and `approve_plan`; `exit_plan_mode_tool(policy, approve)` call matches in Task 3; `policy.base_mode` written in `__init__` and read in the tool + the loop.
