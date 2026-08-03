# Lesson 18 — Skill Dynamic Command Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the read-only `view_skill` tool into an executing `skill` tool that replaces `` !`cmd` `` blocks in a skill body with the command's live sandboxed output at invocation time.

**Architecture:** A shared sandbox runner (lifted from the bash tool) executes each `` !`cmd` `` at the moment the model invokes a skill. The tool's `read_only` flag flips to `False`; a session-start approval gate (mirroring `approve_hooks`/`approve_mcp`, but sandboxed and content-gated) consents to skills that execute, and drops them on decline (fail-open). Injection logic is pure and unit-tested; the `main.py` approval glue is diff-reviewed only.

**Tech Stack:** Python 3.14, `uv`, `pytest`, stdlib `re` + `subprocess` (via the existing sandbox). No new dependencies.

## Global Constraints

- Python 3.14; run everything with `uv run` (e.g. `uv run pytest`).
- Default test suite runs offline — no test may hit the network or the LLM backend.
- No new third-party dependencies; reuse `harness/sandbox.py` and stdlib `re`.
- Commit style: `lesson 18: <what changed>` (imperative). End every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- `args`, bundled skill directories, `${SKILL_DIR}`, and frontmatter-as-policy are **out of scope** (lessons 19–20). The skill tool schema stays `{name}`.
- Injection grammar is exactly `` !`cmd` `` (a `!`, a backtick, non-backtick chars, a backtick). No escape syntax, no nested backticks.

---

### Task 1: Expose the shared sandbox runner

Lift bash.py's private `_run` to a public `run_sandboxed` so skill injection and the bash tool share one execution path and one output contract. `hooks.py` has its own unrelated `_run` — do **not** touch it.

**Files:**
- Modify: `harness/tools/bash.py:8-19` (rename + defaults), `harness/tools/bash.py:44` (call site)
- Test: `tests/test_tools_bash.py`

**Interfaces:**
- Produces: `run_sandboxed(command: str, sandbox: Sandbox, timeout: int = 30, output_limit: int = 8000) -> str` — runs `command` through `sandbox.wrap`, returns `"exit code: {N}\n{combined stdout+stderr, truncated}"`, or `"command timed out after {timeout}s"`. Never raises on ordinary command failure.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_bash.py`:

```python
from harness.tools.bash import bash_tool, run_sandboxed
from harness.sandbox import NoSandbox


def test_run_sandboxed_reports_exit_and_output():
    out = run_sandboxed("echo hi", NoSandbox())
    assert "hi" in out
    assert "exit code: 0" in out
```

(Update the existing `from harness.tools.bash import bash_tool` line at the top to the combined import above.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_bash.py::test_run_sandboxed_reports_exit_and_output -v`
Expected: FAIL with `ImportError: cannot import name 'run_sandboxed'`.

- [ ] **Step 3: Rename `_run` to `run_sandboxed` with defaults**

In `harness/tools/bash.py`, replace the `_run` definition (lines 8-19):

```python
def run_sandboxed(
    command: str, sandbox: Sandbox, timeout: int = 30, output_limit: int = 8000
) -> str:
    try:
        proc = subprocess.run(
            sandbox.wrap(command),  # sandbox decides how the command runs
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"command timed out after {timeout}s"
    body = proc.stdout + proc.stderr
    return f"exit code: {proc.returncode}\n{truncate(body, output_limit)}"
```

And update the call site (line 44) inside `bash_tool`:

```python
        execute=lambda command: run_sandboxed(command, sandbox, timeout, output_limit),
```

- [ ] **Step 4: Run the bash tests to verify they pass**

Run: `uv run pytest tests/test_tools_bash.py -v`
Expected: PASS (all tests, including the pre-existing `bash_tool` tests that exercise the same runner).

- [ ] **Step 5: Commit**

```bash
git add harness/tools/bash.py tests/test_tools_bash.py
git commit -m "lesson 18: expose run_sandboxed as the shared sandbox runner

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Injection primitives

Add the pure functions that detect and expand `` !`cmd` `` blocks. No I/O — `run` is injected, so these test without a shell.

**Files:**
- Modify: `harness/skills.py` (add `import re` and four module-level names)
- Test: `tests/test_skills.py`

**Interfaces:**
- Produces:
  - `cmd_blocks(body: str) -> list[str]` — the commands a body will run, in order.
  - `has_cmd_blocks(body: str) -> bool` — True iff the body contains at least one block.
  - `expand_body(body: str, run: Callable[[str], str]) -> str` — replace each `` !`cmd` `` with `run(cmd)`; a raising `run` yields `[skill command failed: <err>]`; no exception escapes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py` a new import line and these tests:

```python
from harness.skills import cmd_blocks, has_cmd_blocks, expand_body


def test_cmd_blocks_extracts_commands_in_order():
    assert cmd_blocks("a !`one` b !`two`") == ["one", "two"]


def test_has_cmd_blocks_detects_presence():
    assert has_cmd_blocks("x !`pwd` y") is True
    assert has_cmd_blocks("plain prose, no blocks") is False


def test_expand_body_substitutes_command_output():
    out = expand_body("diff:\n!`git diff`", run=lambda cmd: f"<{cmd}>")
    assert out == "diff:\n<git diff>"


def test_expand_body_substitutes_every_block():
    out = expand_body("!`a` and !`b`", run=lambda cmd: cmd.upper())
    assert out == "A and B"


def test_expand_body_leaves_a_body_without_blocks_unchanged():
    body = "just prose, no bang-backtick here"
    assert expand_body(body, run=lambda cmd: "X") == body


def test_expand_body_ignores_a_bare_code_span_and_bare_bang():
    body = "a `code span` and a bare ! and !not-a-block"
    assert expand_body(body, run=lambda cmd: "RAN") == body  # no !`...` pattern


def test_expand_body_turns_a_failing_run_into_an_inline_marker():
    def boom(cmd):
        raise RuntimeError("sandbox down")

    out = expand_body("!`whoami`", run=boom)
    assert out == "[skill command failed: sandbox down]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_skills.py -k "cmd_blocks or expand_body" -v`
Expected: FAIL with `ImportError: cannot import name 'cmd_blocks'`.

- [ ] **Step 3: Implement the primitives**

In `harness/skills.py`, add `import re` at the top (with the other imports) and these definitions above `view_skill_tool`:

```python
_CMD = re.compile(r"!`([^`]*)`")  # !`cmd` — a bang, then a backtick-quoted command


def cmd_blocks(body: str) -> list[str]:
    """The commands a body will run, in order."""
    return _CMD.findall(body)


def has_cmd_blocks(body: str) -> bool:
    """Does this body execute anything? Pure-prose skills answer False and so
    need no execution approval."""
    return _CMD.search(body) is not None


def expand_body(body: str, run: Callable[[str], str]) -> str:
    """Replace each !`cmd` with run(cmd), at invocation time so the output is
    live, not a startup snapshot. A raising run becomes an inline marker rather
    than sinking the load — one bad block must not cost the whole skill (the
    discover() rule)."""

    def replace(match: "re.Match[str]") -> str:
        try:
            return run(match.group(1))
        except Exception as error:  # a bad block degrades to a note, never raises
            return f"[skill command failed: {error}]"

    return _CMD.sub(replace, body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_skills.py -k "cmd_blocks or expand_body" -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 18: add !\`cmd\` detection and expansion primitives

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The executing skill tool

Rename `view_skill_tool` → `skill_tool`, inject at invocation, flip `read_only` to `False`, and update the menu text and stale comments. After this task the suite is green, but `main.py` will not import until Task 4 rewires the call site — that is expected (no test imports `main`).

**Files:**
- Modify: `harness/skills.py:74` (menu text), `:59` and `:70` (stale comments), `:79-104` (the tool)
- Test: `tests/test_skills.py:1` (import), `:53-69` (the three `view_skill` tests), plus two new tests

**Interfaces:**
- Consumes: `expand_body` (Task 2), `run_sandboxed` (Task 1).
- Produces: `skill_tool(skills: list[Skill], run: Callable[[str], str]) -> Tool` — `name="skill"`, `read_only` is `False`; `execute(name)` returns `expand_body(body, run)` for a known skill, else the lesson-15 soft `Error: no skill named ...`.

- [ ] **Step 1: Update existing tests and add the new ones (they fail)**

In `tests/test_skills.py`, change the top import (line 1) from `view_skill_tool` to `skill_tool`:

```python
from harness.skills import Skill, discover, skills_section, skill_tool
```

Replace the three `view_skill` tests (currently lines 53-69) with:

```python
def _noop(cmd):  # a run that is never called (bodies here have no !`cmd`)
    raise AssertionError(f"run should not have been called, got {cmd!r}")


def test_skill_returns_the_full_body(tmp_path):
    write_skill(tmp_path, "commit-style", "how to write commits", "Use imperative mood.")
    tool = skill_tool(discover(tmp_path), run=_noop)
    assert "Use imperative mood." in tool.execute(name="commit-style")


def test_skill_is_not_read_only(tmp_path):
    write_skill(tmp_path, "x", "d", "b")
    assert skill_tool(discover(tmp_path), run=_noop).read_only is False


def test_skill_on_an_unknown_name_lists_what_exists(tmp_path):
    write_skill(tmp_path, "commit-style", "d", "b")
    write_skill(tmp_path, "review-style", "d", "b")
    result = skill_tool(discover(tmp_path), run=_noop).execute(name="nope")
    assert result.startswith("Error")
    assert "commit-style" in result and "review-style" in result
```

Add two new tests (injection via a fake run, and end-to-end through the real runner):

```python
def test_skill_injects_command_output_at_invocation(tmp_path):
    write_skill(tmp_path, "ctx", "gathers context", "user is !`whoami` now")
    tool = skill_tool(discover(tmp_path), run=lambda cmd: f"[{cmd}]")
    assert tool.execute(name="ctx") == "user is [whoami] now"


def test_skill_executes_a_real_command_through_the_sandbox_runner(tmp_path):
    from harness.tools.bash import run_sandboxed
    from harness.sandbox import NoSandbox

    write_skill(tmp_path, "greet", "greets", "says: !`echo tester`")
    tool = skill_tool(discover(tmp_path), run=lambda cmd: run_sandboxed(cmd, NoSandbox()))
    out = tool.execute(name="greet")
    assert "tester" in out
    assert "exit code: 0" in out
```

- [ ] **Step 2: Run the skills tests to verify the new/renamed ones fail**

Run: `uv run pytest tests/test_skills.py -v`
Expected: FAIL with `ImportError: cannot import name 'skill_tool'`.

- [ ] **Step 3: Rename and inject in `skills.py`**

Replace `view_skill_tool` (lines 79-104) with:

```python
def skill_tool(skills: list[Skill], run: Callable[[str], str]) -> Tool:
    bodies = {s.name: s.body for s in skills}

    def execute(name: str) -> str:
        if name not in bodies:
            available = ", ".join(sorted(bodies)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        return expand_body(bodies[name], run)  # inject live command output

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
    )  # read_only now defaults False — this tool can run commands
```

Update the menu line in `skills_section` (line 74):

```python
    lines = ["Available skills (call the skill tool to load one in full):"]
```

Update the two stale comments so they don't name the old tool:
- Line 59: `# a duplicate name would shadow the first in the skill tool's lookup;`
- Line 70: `bodies are pulled in on demand by the skill tool, so an unused skill costs`

- [ ] **Step 4: Run the skills tests to verify they pass**

Run: `uv run pytest tests/test_skills.py -v`
Expected: PASS (all tests, including the two new injection tests).

- [ ] **Step 5: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 18: turn view_skill into the executing skill tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Session approval gate and main wiring

Consent to executing skills once at session start (sandboxed wording), drop them on decline before the menu is built, and wire the real sandbox runner into `skill_tool`. Per the spec, this `main.py` glue is **verified by diff-read and a manual smoke, not unit tests** (no test imports `main`).

**Files:**
- Modify: `main.py:26` (skills import), `:28` (bash import), `:63-74` (`approve_commands` flag), after `:102` (`approve_skill_execution`), `:305-308` (gate before the menu), `:352-355` (register `skill_tool`), and the stale `view_skill` comments at `:305` and `:353`

**Interfaces:**
- Consumes: `skill_tool`, `has_cmd_blocks`, `cmd_blocks`, `Skill` (from `harness.skills`); `run_sandboxed` (from `harness.tools.bash`); `sandbox` (already defined at `main.py:215`).
- Produces: nothing downstream — terminal task.

- [ ] **Step 1: Add the `sandboxed` flag to `approve_commands`**

`main.py`, change the signature (line 63) and the print line (line 74):

```python
def approve_commands(
    source: str, noun: str, commands: list[str], *, sandboxed: bool = False
) -> bool:
```

```python
    kind = "sandboxed" if sandboxed else "unsandboxed"
    print(f"{source} wants to run these commands ({kind}):")
```

Hooks and MCP keep the default and still print "(unsandboxed)".

- [ ] **Step 2: Add `approve_skill_execution`**

`main.py`, immediately after `approve_mcp` (after line 102):

```python
def approve_skill_execution(skills: list[Skill]) -> bool:
    commands = [
        f"{skill.name}: {command}"
        for skill in skills
        for command in cmd_blocks(skill.body)
    ]
    return approve_commands("skills/", "skill commands", commands, sandboxed=True)
```

- [ ] **Step 3: Update the imports**

`main.py:26`:

```python
from harness.skills import (
    Skill,
    cmd_blocks,
    discover,
    has_cmd_blocks,
    skill_tool,
    skills_section,
)
```

`main.py:28`:

```python
from harness.tools.bash import bash_tool, run_sandboxed
```

- [ ] **Step 4: Gate the executable skills before the menu is built**

`main.py`, replace lines 305-307 (the discover + comment + `section =`) with:

```python
    # skills menu (metadata only — bodies load on demand via the skill tool)
    skills = discover(workspace / "skills")
    executable = [s for s in skills if has_cmd_blocks(s.body)]
    if executable and not approve_skill_execution(executable):
        # a skill is a capability, not policy: decline drops the executable
        # ones (fail-open, the MCP line) and the session continues on prose
        print(f"(skill execution declined — dropping {len(executable)} executable skill(s))")
        skills = [s for s in skills if not has_cmd_blocks(s.body)]
    section = skills_section(skills)
```

Dropping before `skills_section` keeps a declined skill out of the menu too.

- [ ] **Step 5: Wire the runner and register `skill_tool`**

`main.py`, replace the `view_skill` registration block (lines 352-355):

```python
    if skills:
        # only offer the skill tool when there is a menu to view — otherwise the
        # model can waste a turn calling a tool that can only ever error
        def run(command: str) -> str:
            return run_sandboxed(command, sandbox)

        registry.append(skill_tool(skills, run))
```

- [ ] **Step 6: Run the full suite to verify nothing regressed**

Run: `uv run pytest`
Expected: PASS (whole suite green; `main` is not imported by any test, so this confirms the harness modules stay consistent).

- [ ] **Step 7: Manual smoke (optional but recommended)**

Create a throwaway executable skill and drive it end-to-end:

```bash
mkdir -p /tmp/lesson18-smoke/skills
printf -- '---\nname: whoami-demo\ndescription: reports the running user\n---\nThe user is !`echo hello-from-skill`.\n' \
  > /tmp/lesson18-smoke/skills/whoami-demo.md
uv run python main.py --workspace /tmp/lesson18-smoke
```

Expected: at startup the harness prints `skills/ wants to run these commands (sandboxed):` listing `whoami-demo: echo hello-from-skill` and asks to enable. Approve, ask the model to "use the whoami-demo skill", and confirm `hello-from-skill` appears in the injected skill body. Decline instead and confirm the skill is dropped and the prompt notes it. (Adjust the `--workspace` flag to match `main.py`'s actual argument name if it differs.)

- [ ] **Step 8: Commit**

```bash
git add main.py
git commit -m "lesson 18: gate and wire skill command execution in main

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Reviewer notes (diff-read checklist for the untested glue)

- `approve_commands` still prints "(unsandboxed)" for hooks and MCP; only skills pass `sandboxed=True`.
- The executable-skill gate runs **before** `skills_section`, so a declined skill appears in neither the menu nor the registry.
- `run` closes over the session `sandbox` (main.py:215), so every `` !`cmd` `` is sandboxed exactly like a `bash` tool call.
- Invariant: `skill_tool`'s `run` is reachable only from a skill that survived the gate, so no un-consented command can execute.

## After implementation

Implementation ends here (suite green). The repo workflow then takes over — this is **not** part of this plan: rebase `lesson-18` on `main`, open the PR, pass yc's diff-read + concept quiz, and only then tag `lesson-18` on the squash-merge commit.

## Self-review

- **Spec coverage:** rename + `read_only` flip (Task 3) ✓; invocation-time `!` injection via the shared sandbox runner (Tasks 1–3) ✓; content-gated session approval with honest "sandboxed" wording (Task 4 Steps 1–2, 4) ✓; drop-on-decline fail-open (Task 4 Step 4) ✓; soft failure via the bash contract + inline marker (Task 2 `expand_body`, Task 3 real-command test) ✓; the unit/diff-review test split (Tasks 2–3 unit, Task 4 diff-read) ✓; deferred `args`/dirs/frontmatter noted in Global Constraints ✓.
- **Placeholder scan:** none — every code step shows full code; the only "optional" step (manual smoke) is fully scripted.
- **Type consistency:** `run_sandboxed(command, sandbox, timeout=30, output_limit=8000)` used identically in bash.py and the `run` closure; `run: Callable[[str], str]` matches across `expand_body`, `skill_tool`, and the `def run` in main; `skill_tool(skills, run)` call sites (tests + main) all pass `run`.
