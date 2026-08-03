# Plan Mode (Lesson 22)

**Date:** 2026-07-10
**Status:** Approved pending user review

**Context:** A **per-turn** planning gate, entered with a `/plan` slash command
(building on lesson 21). In a plan turn the model investigates read-only and
proposes a plan; on the human's approval it acts. Plan mode never persists past
the turn — every other turn runs in the session's normal mode.

## Goal

`/plan [task]` runs a turn in plan mode: mutating tools are **denied**, read-only
allowed. The model calls `exit_plan_mode(plan)`; on **approve** the policy
restores to the session's base mode and the model acts *in the same turn*; on
**reject** the human's feedback returns so it revises. After the turn, the mode
always restores to base — the next turn starts normal.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Entry | `/plan [task]` — a **built-in** slash command | Reuses lesson 21's slash dispatch; a mid-session, per-invocation gate (not a startup `--mode`). Built-ins resolve BEFORE skill-name resolution, so `/plan` works with zero skills |
| Scope | **Per-turn (non-sticky)** | `/plan <task>` runs that task now in plan mode; `/plan` alone arms the *next* turn; after any plan turn the mode restores to base — plan mode never spans two turns |
| Restore target | `policy.base_mode` — the session's normal mode, captured at construction | "Flip back to the state before plan": since plan never persists, the state-before is always the base mode. If you were in `acceptAll`, you return to `acceptAll` |
| Blocking mutation | `decide` treats `"plan"` like `"readOnly"` — non-read-only → `"deny"`, read-only → `"allow"` | Plan mode *is* read-only for tool execution; the difference is the gate tool + prompt |
| The plan gate | `exit_plan_mode(plan)` tool, `read_only=True` + `spawns_subagents=True` | Model signals "done planning"; on the human's approval it restores `base_mode` (so the model acts in the same turn). Registered always but **no-ops outside plan mode**. `spawns_subagents` keeps it out of subagents (a plan-mode exploration subagent must not exit plan mode / prompt the human / flip the shared policy) |
| Same-turn action + backstop | `exit_plan_mode` flips the mode mid-turn; the REPL also resets `mode = base_mode` after every turn | The model plans then acts within one turn; the post-turn reset guarantees plan mode can't leak past the turn even if `exit_plan_mode` was never called or was rejected |
| System prompt | a `PLAN_MODE` section appended when `policy.mode == "plan"` at turn-build time | The system prompt is rebuilt each turn, so the plan instructions appear only during a plan turn |

## Components

- **`harness/permissions.py`**: `MODES += "plan"`. `PermissionPolicy.__init__` stores `self.base_mode = mode` alongside `self.mode`. In `decide`, `case "readOnly" | "plan": return "deny"` (read-only tools still allowed by the early return).
- **`harness/tools/plan.py`** (new): `exit_plan_mode_tool(policy, approve) -> Tool`.
  - `approve: Callable[[str], tuple[bool, str]]` — injected; given the plan, returns `(approved, feedback)`.
  - `execute(plan)`: if `policy.mode != "plan"` → return "Not in plan mode." (no-op). Else `approved, feedback = approve(plan)`; approved → `policy.mode = policy.base_mode`, return "Plan approved — proceed."; else → return "Not approved; revise. Feedback: …" (stays `"plan"`).
  - `read_only=True`, `spawns_subagents=True`.
- **`harness/prompts.py`**: a `PLAN_MODE` section string (investigate read-only, do not act, present a plan via `exit_plan_mode`, wait for approval).
- **`main.py`**:
  - Register `exit_plan_mode_tool(policy, approve_plan)` in `tools` before `with_hooks` (always). `approve_plan(plan)` prints the plan, asks `[y]es / [n]o`, and on `n` reads a feedback line; non-interactive stdin → `(False, "")`.
  - Slash dispatch (extending lesson 21's block): after `parse_slash`, check **built-ins first**: if `name == "plan"` → `policy.mode = "plan"`; if `args` → `user_input = args` (run this turn); else → print "(plan mode armed — your next message runs in plan mode)" and `continue`. Otherwise fall through to skill resolution.
  - After each `run_turn` call: `policy.mode = policy.base_mode` (plan mode never survives a turn).
  - System prompt each turn: `current_system_prompt(workspace, context_sections + ([PLAN_MODE] if policy.mode == "plan" else []))`.

## Data flow

```
You: /plan refactor the parser
  → built-in: policy.mode = "plan"; user_input = "refactor the parser"
  → run_turn (system prompt includes PLAN_MODE)
      → read_file / grep  (read-only — allowed)
      → write_file(...)   → decide() "plan" → DENY → "Permission denied"
      → exit_plan_mode(plan="1. … 2. …")
           → approve(plan): prints it, asks — you: y
           → policy.mode = policy.base_mode ("default"); "Plan approved — proceed."
      → write_file(...)   → decide() now "default" → ask → you approve → runs
  → after run_turn: policy.mode = base_mode   (already restored; idempotent)
You: <next message>  → runs in base_mode (not plan)
```

Reject: `approve` → `(False, "also update the tests")` → the tool returns that
feedback → the model revises, still read-only; the post-turn reset clears plan
mode when the turn ends.

## Error handling

- A mutating tool in plan mode → the loop's existing "Permission denied" result.
- `exit_plan_mode` called outside plan mode → "Not in plan mode." (no-op).
- Non-interactive stdin during `approve_plan` → `(False, "")` (no human to consent), like the other approval gates.
- `--mode readOnly` + `/plan` → the model can plan, but `base_mode` is `readOnly`, so after approval it still can't mutate (readOnly wins). Coherent; not special-cased.

## Testing

`tests/test_permissions.py`:
- `PermissionPolicy("plan")` is valid; `base_mode == "plan"`. `decide` denies a non-read-only tool and allows a `read_only` tool in plan mode.
- `PermissionPolicy("default")` → `base_mode == "default"`.

`tests/test_plan.py` (new):
- `exit_plan_mode_tool(policy, approve)` with `policy.mode == "plan"` and `approve → (True, "")` → `execute` sets `policy.mode == policy.base_mode` and the result says approved.
- `approve → (False, "fix X")` → `policy.mode` stays `"plan"`, result carries the feedback.
- `policy.mode == "default"` (not plan) → `execute` returns "Not in plan mode." and does not call `approve`.
- the tool is `read_only=True` and `spawns_subagents=True`.

The `main.py` wiring (`/plan` dispatch, the post-turn reset, `approve_plan` IO, the dynamic prompt) is diff-reviewed; `decide`, `base_mode`, and the tool logic are unit-tested.

## Out of scope (deferred)

- Persistent plan mode that spans multiple turns.
- A rendered/structured plan artifact (the plan is plain text).
- Other built-in slash commands (`/model`, `/clear`, …).
