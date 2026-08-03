# Lesson 18 — place `!`cmd`` in the config-authored shell lane (match Claude Code)

> Decision (yc): **do what Claude Code does.** A skill's `` !`cmd` `` is a
> *config-authored* preprocessor (the human installs the skill file; the model
> only picks which skill to load), so it belongs in the same lane as
> `hooks.json`/`mcp.json` commands: a sandboxed shell command gated by a
> **session-start approval**, run *outside* the per-call tool path (no
> per-command permission prompt, no PreToolUse hooks, not journaled as a tool
> call) — exactly like a hook.

## Why this is invariant-consistent (put in code comments)

The harness has two shell lanes:
- **Model-authored** (`bash` tool): sandboxed + per-call permission + hooks + journal.
- **Config-authored** (`hooks.json`, `mcp.json`): session-start approval, outside per-call governance.

A skill file is clone-shippable, model-writable workspace config — lane 2. So
the earlier review's findings 1/2/6 (embedded cmd bypasses hooks/journal/per-call
permission) describe the *intended* lane-2 behavior, same as a hook. They are NOT
fixed by governing the command; they are correct by placement. **Comment each
site so a future review understands this is deliberate, not an oversight.**

Consequence to accept (document): a skill's `` !`cmd` `` runs regardless of
`--mode` (like a hook), sandboxed, after one session-start consent. The control
is decline-at-session-start (and non-interactive stdin auto-declines).

## Target = restore commit `f51c6b3`'s config-lane design + these deltas

The current branch (`ff88940`) routes commands through the governed tool path
(the "Option A" revision). Move it back to the config-lane model. `f51c6b3` is
the reference for the gate + wiring — `git show f51c6b3:main.py` /
`git show f51c6b3:harness/skills.py`.

### `harness/loop.py` — REVERT the `run_tool` extraction
The config lane never touches the tool path, so `loop.py` should be pristine.
Restore the original single `_run_one_call` (inline parse+govern+execute) and
delete the public `run_tool`. Reference: `git show 6bd68a8:harness/loop.py`.
Remove the `run_tool` tests from `tests/test_loop.py`.

### `harness/tools/bash.py` — KEEP (no change)
`DEFAULT_TIMEOUT`/`DEFAULT_OUTPUT_LIMIT` constants stay (finding #9). `run_sandboxed` stays public — the skill `run` calls it directly.

### `harness/skills.py`
- KEEP: `_CMD` anchored regex, `expand_body` (escape), `skill_tool(skills, run=None)`, `view_skill_tool = skill_tool` alias.
- CHANGE `skill_tool`'s `read_only=run is None` → **`read_only=True`** (always). Comment: the skill tool only injects preprocessed text; its `` !`cmd` `` are session-approved config shell run before/independent of per-call governance, so the tool *call* is read-only (no per-call prompt, available in every mode — like loading a skill in lesson 15).
- RESTORE `cmd_blocks` and `has_cmd_blocks` (the session gate needs them), **escape-aware** against the current `_CMD` (group 1 = escape, group 2 = command):

```python
def cmd_blocks(body: str) -> list[str]:
    """The commands a body will actually run, in order (escaped `\\!`x`` excluded)."""
    return [m.group(2) for m in _CMD.finditer(body) if not m.group(1)]


def has_cmd_blocks(body: str) -> bool:
    """True iff the body contains at least one real (unescaped) command."""
    return any(not m.group(1) for m in _CMD.finditer(body))
```

### `main.py`
- Imports: add `Skill, cmd_blocks, has_cmd_blocks` to the `harness.skills` import (alongside `discover, skill_tool, skills_section`); add `run_sandboxed` to the `harness.tools.bash` import; add `NoSandbox` to the `harness.sandbox` import; REMOVE `run_tool` from the `harness.loop` import (leave `run_turn`).
- Restore `approve_commands`'s `sandboxed: bool = False` kwarg + the `kind` line (verbatim from `f51c6b3`), AND update its docstring to name the third caller: `"Workspace config (hooks.json, mcp.json, skills/) is clone-shippable and model-writable, so its commands run only after the human reads them on a real terminal."` (finding #10).
- Restore `approve_skill_execution`, but pass the **real** sandbox state, not a hardcoded `True` (finding #7):

```python
def approve_skill_execution(skills: list[Skill], sandboxed: bool) -> bool:
    commands = [
        f"{skill.name}: {command}"
        for skill in skills
        for command in cmd_blocks(skill.body)
    ]
    return approve_commands("skills/", "skill commands", commands, sandboxed=sandboxed)
```

- Restore the session-start gate BEFORE `section = skills_section(skills)` (config-shell consent, mirrors `approve_hooks`/`approve_mcp`):

```python
    skills = discover(workspace / "skills")
    # a skill's !`cmd` is config-authored shell (the human installs the file),
    # so it is gated once here like hooks.json/mcp.json — not per call. Decline
    # drops the executable skills (a capability, not policy); prose skills stay.
    executable = [s for s in skills if has_cmd_blocks(s.body)]
    if executable and not approve_skill_execution(
        executable, sandboxed=not isinstance(sandbox, NoSandbox)
    ):
        print(f"(skill execution declined — dropping {len(executable)} executable skill(s))")
        skills = [s for s in skills if not has_cmd_blocks(s.body)]
    section = skills_section(skills)
```

- Restore the run wiring in the registry (original position — `run` only needs `sandbox`, so NO policy reorder; put `policy = PermissionPolicy(cli_args.mode)` back after the registry as in `f51c6b3`):

```python
    if skills:
        # run executes a skill's !`cmd` as a sandboxed PREPROCESSOR — deliberately
        # outside the per-call tool path (no permission prompt, no PreToolUse
        # hooks, not journaled as a tool call), because the command is config-
        # authored and was approved once at session start, exactly like a hook.
        def run(command: str) -> str:
            return run_sandboxed(command, sandbox)

        registry.append(skill_tool(skills, run))
```

- Confirm the Task-5 `session_allowlist.add("skill")` block is gone (it was removed in the revision).

### tests
- `tests/test_skills.py`: RESTORE `cmd_blocks`/`has_cmd_blocks` tests (escape-aware: an escaped-only body has NO commands → `has_cmd_blocks` False, `cmd_blocks` empty; a real block counts). KEEP the grammar/escape/D1 tests and the injection test. UPDATE the read-only tests: `skill_tool(skills, run=...)` is now `read_only is True` (change `test_skill_is_not_read_only` accordingly). Keep the `run=None` verbatim/read-only test and the alias test.
- `tests/test_loop.py`: remove the `run_tool` tests.

## Verification
1. `uv run pytest` fully green.
2. `uv run python -c "import main"` clean.
3. Manual (yc): a skill with `` !`echo hi` `` prompts ONCE at session start (listing the command, labeled by real sandbox state), not per invocation; declining drops it; a pure-prose skill is never gated and loads in any `--mode`.

## Not doing (noted for yc)
- No action journal for `` !`cmd` `` (Claude Code doesn't; consistent with hooks). A one-line `record_action` could be added later if audit is wanted.
- No separate global `disableSkillShellExecution` flag — the session-start decline (and non-interactive auto-decline) already turns it off. Easy to add later.
