# Lesson 18 Revision — route embedded commands through the governed tool path

> Fixes the 10 findings from the xhigh review. Decision (yc): **Option A** —
> an embedded `` !`cmd` `` is executed as a *governed bash call*, so permission
> (`--mode`), PreToolUse/PostToolUse hooks, and the action journal all apply
> automatically. The whole session-start approval gate dissolves.

**Goal:** an embedded skill command is indistinguishable from a `bash` tool call
the model made — same permission gate, same hooks, same journal entry — and the
`` !`cmd` `` grammar stops misfiring on prose and supports an escape.

## What each finding maps to

| # | Finding | Fix |
|---|---|---|
| 1 | embedded cmd bypasses PreToolUse hooks | route through `run_tool` → hook-wrapped `bash` |
| 2 | embedded cmd absent from action journal | `run_tool` calls `on_tool_call` (journal) |
| 6 | readOnly bypass via allowlist | `run_tool` → `_permitted` → readOnly denies bash; gate/allowlist removed |
| 7 | "(sandboxed)" label lies on NoSandbox | `approve_skill_execution` removed entirely |
| 8 | gate wiring untested | gate removed; `run_tool` is unit-tested in `test_loop.py` |
| 3 | seam rename breaks UI lane | `run` optional (None → non-executing, read_only); `view_skill_tool` compat alias; **flag yc to coordinate ui migration** |
| 4 | regex matches prose | `(?<!\`)` lookbehind + require non-empty command |
| 5 | verbatim invariant lost; docs execute | escape: `` \!`cmd` `` → literal |
| 9 | duplicated 30/8000 defaults | module constants in `bash.py` |
| 10 | stale approve_commands docstring | revert `approve_commands` (no `sandboxed`, no skills caller) |

## Global constraints (unchanged)

Python 3.14; `uv run pytest` offline; no new deps; commit `lesson 18: <what>` +
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## Change 1 — `harness/loop.py`: extract the governed dispatch

Split the govern-and-execute half of `_run_one_call` into a public `run_tool`, so
a tool can delegate to another *governed* tool.

```python
def run_tool(
    name: str,
    args: dict,
    tools: dict[str, Tool],
    policy: PermissionPolicy | None,
    asker: Callable[[str, dict], str] | None,
    on_tool_call: Callable[[str, dict], None] | None,
) -> str:
    """Run one already-parsed tool call through the governed path: permission,
    journal, then the (hook-wrapped) tool. A tool may call this to delegate to
    another governed tool — the skill tool runs its embedded !`cmd` as a bash
    call here, so --mode, hooks, and the journal apply exactly as they would to
    a bash call the model made itself."""
    if name not in tools:
        available = ", ".join(tools) or "none"
        return f"Error: unknown tool {name!r}. Available tools: {available}"
    if not _permitted(tools[name], args, policy, asker):
        return (
            f"Permission denied: {name} was not allowed. "
            "Do not retry unless the user asks for it differently."
        )
    if on_tool_call is not None:
        on_tool_call(name, args)
    try:
        return tools[name].execute(**args)
    except Exception as error:  # noqa: BLE001 — the model handles it from here
        return f"Error: {type(error).__name__}: {error}"


def _run_one_call(call, tools, policy, asker, on_tool_call) -> str:
    """Parse one wire-format tool call, then run it through run_tool."""
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"]["arguments"])
    except json.JSONDecodeError as error:
        return f"Error: arguments are not valid JSON ({error}). Retry the call."
    return run_tool(name, args, tools, policy, asker, on_tool_call)
```

**Tests (`tests/test_loop.py`)** — `run_tool` is now the security seam, so test it:
- readOnly mode denies a non-read-only tool (returns "Permission denied").
- a permitted call invokes `on_tool_call(name, args)` exactly once (journal).
- unknown tool name → the "unknown tool" error.
- `_run_one_call` still parses args and delegates (one regression test).

---

## Change 2 — `harness/tools/bash.py`: one source of truth for the limits

```python
DEFAULT_TIMEOUT = 30
DEFAULT_OUTPUT_LIMIT = 8000


def run_sandboxed(
    command: str,
    sandbox: Sandbox,
    timeout: int = DEFAULT_TIMEOUT,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
) -> str:
    ...  # body unchanged


def bash_tool(
    sandbox: Sandbox | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
) -> Tool:
    ...  # body unchanged
```

No new tests (existing bash tests cover behavior; defaults unchanged in value).

---

## Change 3 — `harness/skills.py`: grammar + escape, drop the gate helpers, run optional

**Grammar + escape.** Replace `_CMD` and `expand_body`; **delete `cmd_blocks` and
`has_cmd_blocks`** (only the removed session-start gate used them).

```python
# !`cmd` — a bang (not inside a code span, not escaped) then a backtick-quoted,
# non-empty command. `\!`cmd`` is an escaped literal. `(?<!`)` keeps ordinary
# prose like "the `!` key" from being read as a command.
_CMD = re.compile(r"(?<!`)(\\?)!`([^`]+)`")


def expand_body(body: str, run: Callable[[str], str]) -> str:
    """Replace each !`cmd` with run(cmd) at invocation time. `\\!`cmd`` is a
    literal — the backslash is stripped and the command is NOT run, so a skill
    can document the syntax. A raising run degrades to an inline marker rather
    than sinking the load."""

    def replace(match: "re.Match[str]") -> str:
        escaped, command = match.group(1), match.group(2)
        if escaped:
            return f"!`{command}`"  # literal: drop the backslash, do not run
        try:
            return run(command)
        except Exception as error:  # a bad block degrades, never raises
            return f"[skill command failed: {error}]"

    return _CMD.sub(replace, body)
```

**Tool factory** — `run` optional (None → non-executing, read_only), plus a
backward-compat alias so the ui lane's `view_skill_tool(skills)` import keeps
working.

```python
def skill_tool(skills: list[Skill], run: Callable[[str], str] | None = None) -> Tool:
    """The skill tool. With `run`, a skill body's !`cmd` blocks execute and the
    body is not verbatim (read_only False). Without `run` (the default), bodies
    are returned verbatim and the tool is read-only — the lesson-15 behavior."""
    bodies = {s.name: s.body for s in skills}

    def execute(name: str) -> str:
        if name not in bodies:
            available = ", ".join(sorted(bodies)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        body = bodies[name]
        return body if run is None else expand_body(body, run)

    return Tool(
        name="skill",
        description=(
            "Load and run one of the available skills (listed in the system "
            "prompt) by name. Do this before a task the skill governs. Some "
            "skills run shell commands to gather live context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name."}
            },
            "required": ["name"],
        },
        execute=execute,
        read_only=run is None,
    )


# Deprecated compat alias: the ui lane still imports view_skill_tool. Calling it
# with no `run` yields the non-executing, read-only lesson-15 tool. The ui lane
# should migrate to skill_tool; tracked as a cross-lane coordination item.
view_skill_tool = skill_tool
```

**Tests (`tests/test_skills.py`):**
- Remove the `cmd_blocks`/`has_cmd_blocks` tests (functions gone).
- `expand_body`: existing cases still pass; ADD — a `` \!`cmd` `` is returned
  literal (backslash stripped) and `run` is never called; prose `` the `!` key ``
  is left unchanged (no match); an empty `` !`` `` does not match.
- `skill_tool` with `run=None`: `read_only is True`, body returned verbatim
  (no execution); with a `run`: injects and `read_only is False`.
- `view_skill_tool(skills)` returns a read-only tool (alias smoke).

---

## Change 4 — `main.py`: remove the gate, wire the governed run

**Remove:**
- `approve_skill_execution` (the whole function).
- the `sandboxed` parameter and `kind` line from `approve_commands` — restore the
  original 3-argument function and its original docstring/message.
- the session-start gate block (`executable = [...]; if executable and not
  approve_skill_execution(...): ... drop`), back to `skills = discover(...)` then
  `section = skills_section(skills)`.
- the `if skills: policy.session_allowlist.add("skill")` exemption block.

**Imports:** drop `Skill`, `cmd_blocks`, `has_cmd_blocks` from the `harness.skills`
import (now unused); add `run_tool` to the `harness.loop` import.

**Reorder + wire:**
- Move `policy = PermissionPolicy(cli_args.mode)` up to just before the tool
  registry is built (it depends only on `cli_args`).
- Do NOT put `skill_tool` in the initial `registry` list. After the agent tool is
  added and **before** `with_hooks` (so the skill tool is itself hook-wrapped),
  add:

```python
    if skills:
        # an embedded !`cmd` runs as a governed bash call: run_tool applies the
        # same permission gate (--mode), hooks, and journal that a model-issued
        # bash call gets. tools["bash"] is looked up at call time so the
        # hook-wrapped version (after with_hooks) is the one used.
        def run(command: str) -> str:
            return run_tool(
                "bash", {"command": command}, tools, policy, asker, observe_tool_call
            )

        tools["skill"] = skill_tool(skills, run)
```

`asker`, `observe_tool_call`, `policy`, and `tools` are all defined by this point.

No unit tests for the `main.py` glue (diff-review only, as before), but the
governed path it depends on (`run_tool`) is now unit-tested in `test_loop.py`,
which is the security-critical logic finding #8 asked to cover.

---

## Verification

1. `uv run pytest` — full suite green (new `run_tool` tests + revised skill tests).
2. `uv run python -c "import main"` — clean import.
3. Manual (yc, optional): a skill body with `` !`echo hi` `` prompts for the *bash*
   command in default mode, is denied under `--mode readOnly`, and a PreToolUse
   `bash` hook sees it.
4. Re-run the adversarial review over the new diff to confirm the 10 findings are
   resolved and nothing regressed.

## Cross-lane item for yc

The `view_skill_tool` → `skill_tool` seam change affects the **ui lane**
(`ui/server/harness_session.py`). A compat alias keeps it importable, but the ui
lane should migrate to `skill_tool` and decide whether it wants executing skills.
Per CLAUDE.md this seam change is routed through you, not self-served.
