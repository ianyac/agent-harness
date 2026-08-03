# Lesson 20 — Skill Args & Fork-Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The skill tool takes an `args` string (`$ARGUMENTS`/`$1`…`$9` substituted into the body before `` !`cmd` `` expansion), and a `context: fork` skill runs as a subagent configured by `model` (real multi-model) and `allowed-tools`, returning the subagent's answer.

**Architecture:** Five tasks across four files. `llm.py` gains model slugs + a `make_llm` factory. `agent.py` extracts a shared `run_subagent`. `skills.py` gains frontmatter parsing + arg substitution (Task 3) and the fork-capable `skill_tool` (Task 4). `main.py` wires `make_llm` + a `fork_run` closure.

**Tech Stack:** Python 3.14, `uv`, `pytest`, stdlib `re`. No new dependencies.

## Global Constraints

- Python 3.14; `uv run pytest` **offline** — no test may need credentials or the network. `make_llm` takes an injectable `build` so it's tested without a real `CodexAdapter`.
- Commit style `lesson 20: <what>` + `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Args substitute everywhere including `` !`cmd` `` (template approval), before `expand_body`. Single-pass `re.sub` (arg text is never re-scanned).
- Valid API model slugs: `gpt-5.5` (default), `gpt-5.4`, `gpt-5.4-mini` — all context window 272000.
- `skill_tool` stays `read_only=True` and becomes `spawns_subagents=True` (bars nested fork; subagents no longer receive the skill tool).
- Out of scope: per-call `model` in `complete()`, non-API models, user-invocation, other frontmatter fields.

---

### Task 1: `llm.py` — model slugs + `make_llm` factory

**Files:** Modify `harness/llm.py`; Test `tests/test_llm_contract.py`

**Interfaces:**
- Produces: `CONTEXT_WINDOWS` includes `gpt-5.4`, `gpt-5.4-mini`. `make_llm(slug: str | None = None, *, build=CodexAdapter) -> LLMClient` — returns a client for `slug` (default `"gpt-5.5"`), cached per slug; `build` is the constructor (injectable for offline tests).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_llm_contract.py`:

```python
from harness.llm import CONTEXT_WINDOWS, make_llm


def test_context_windows_has_the_api_models():
    for slug in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"):
        assert CONTEXT_WINDOWS[slug] == 272_000


def test_make_llm_defaults_to_gpt55_and_caches(monkeypatch):
    built = []

    def fake_build(slug):
        built.append(slug)
        return object()

    a = make_llm(build=fake_build)          # default slug
    b = make_llm("gpt-5.4", build=fake_build)
    a2 = make_llm(build=fake_build)          # cached — no second build
    assert built == ["gpt-5.5", "gpt-5.4"]
    assert a is a2 and a is not b
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_llm_contract.py -k "context_windows_has or make_llm_defaults" -v`
Expected: FAIL — `make_llm` undefined; `gpt-5.4` not in `CONTEXT_WINDOWS`.

- [ ] **Step 3: Implement**

In `harness/llm.py`, extend the constant and add the factory (place `make_llm` just after the `CodexAdapter` class):

```python
CONTEXT_WINDOWS = {"gpt-5.5": 272_000, "gpt-5.4": 272_000, "gpt-5.4-mini": 272_000}
```

```python
_LLM_CACHE: dict[str, LLMClient] = {}


def make_llm(slug: str | None = None, *, build: Callable[[str], LLMClient] = CodexAdapter) -> LLMClient:
    """Return a client for `slug` (default gpt-5.5), one per slug for the whole
    process so repeated forks reuse a client. `build` is injectable so the
    offline suite can exercise the factory without constructing a real
    CodexAdapter (whose __init__ reads ~/.codex/auth.json)."""
    slug = slug or "gpt-5.5"
    if slug not in _LLM_CACHE:
        _LLM_CACHE[slug] = build(slug)
    return _LLM_CACHE[slug]
```

(`Callable` is already imported in `llm.py`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_llm_contract.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add harness/llm.py tests/test_llm_contract.py
git commit -m "lesson 20: add gpt-5.4 models and a make_llm factory

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `agent.py` — extract `run_subagent`

**Files:** Modify `harness/tools/agent.py`; Test `tests/test_agent_tool.py`

**Interfaces:**
- Produces: `run_subagent(task, llm, tools, *, policy, system=None, on_tool_call=None, max_iterations=20, compact_threshold=None, keep_recent=8) -> str` — the recursion-guard filter + `run_turn([], …, asker=None)` + `ABORTED_PREFIX`→error mapping. `agent_tool.execute` calls it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent_tool.py` (a fake `llm` + a spy tool; use the existing test helpers/fakes in that file for the LLM — mirror how the current agent-tool tests build one):

```python
from harness.tools.agent import run_subagent
from harness.tools.base import Tool


def _noop_tool(name, spawns=False):
    return Tool(name=name, description="d",
                parameters={"type": "object", "properties": {}},
                execute=lambda: "ok", read_only=True, spawns_subagents=spawns)


def test_run_subagent_excludes_spawns_subagents_tools(fake_llm_that_answers):
    # fake_llm_that_answers: an LLMClient returning a plain answer, no tool calls
    seen = {}
    def spy(messages, tools=None, system=None, on_text_delta=None):
        seen["tools"] = [t["function"]["name"] for t in (tools or [])]
        return {"role": "assistant", "content": "done"}
    tools = {"read_file": _noop_tool("read_file"), "agent": _noop_tool("agent", spawns=True)}
    out = run_subagent("do it", type("L", (), {"complete": staticmethod(spy)})(), tools, policy=None)
    assert out == "done"
    assert "agent" not in seen["tools"] and "read_file" in seen["tools"]
```

(If `tests/test_agent_tool.py` already has a fake-LLM fixture/helper, use it instead of the inline `spy`/`type(...)` shim — match the file's existing style.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_tool.py -k run_subagent -v`
Expected: FAIL — `run_subagent` undefined.

- [ ] **Step 3: Extract**

In `harness/tools/agent.py`, add `run_subagent` and call it from `agent_tool.execute`:

```python
def run_subagent(
    task: str,
    llm: LLMClient,
    tools: dict[str, Tool],
    *,
    policy: PermissionPolicy | None,
    system: str | Callable[[], str] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    max_iterations: int = 20,
    compact_threshold: int | None = None,
    keep_recent: int = 8,
) -> str:
    """Run one subagent to completion and return its final answer. A subagent
    never finds a delegation tool (the spawns_subagents recursion guard) and
    never prompts (asker=None → ask-decisions become denials)."""
    inner = {name: t for name, t in tools.items() if not t.spawns_subagents}
    reply = run_turn(
        [],
        task,
        llm,
        tools=inner,
        max_iterations=max_iterations,
        on_tool_call=on_tool_call,
        policy=policy,
        asker=None,
        system=system() if callable(system) else system,
        compact_threshold=compact_threshold,
        keep_recent=keep_recent,
    )
    content = reply["content"] or ""
    if content.startswith(ABORTED_PREFIX):
        return f"Error: subagent gave no final answer within {max_iterations} iterations"
    return content
```

Then replace the body of `agent_tool`'s `execute` with a single delegation:

```python
    def execute(task: str) -> str:
        return run_subagent(
            task, llm, tools,
            policy=policy, system=system, on_tool_call=on_tool_call,
            max_iterations=max_iterations, compact_threshold=compact_threshold,
            keep_recent=keep_recent,
        )
```

- [ ] **Step 4: Run to verify pass + no regression**

Run: `uv run pytest tests/test_agent_tool.py -v` → PASS (new test + every existing agent-tool test — behavior is unchanged).

- [ ] **Step 5: Commit**

```bash
git add harness/tools/agent.py tests/test_agent_tool.py
git commit -m "lesson 20: extract run_subagent from agent_tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `skills.py` — frontmatter fields + arg substitution

**Files:** Modify `harness/skills.py`; Test `tests/test_skills.py`

**Interfaces:**
- Consumes: `CONTEXT_WINDOWS` (Task 1).
- Produces: `_parse(text) -> tuple[dict, str]` (meta, body). `Skill` gains `fork: bool = False`, `model: str | None = None`, `allowed_tools: list[str] | None = None`. `discover` reads them and skips a skill with an unknown `model`. `substitute_args(body, args) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py`:

```python
from harness.skills import substitute_args


def test_substitute_args_arguments_and_positionals():
    assert substitute_args("all=$ARGUMENTS first=$1 third=$3", "a b") == "all=a b first=a third="


def test_substitute_args_fills_a_positional_inside_a_command():
    assert substitute_args("!`git log $1`", "HEAD") == "!`git log HEAD`"


def test_discover_reads_fork_model_and_allowed_tools(tmp_path):
    write_dir_skill(
        tmp_path, "research", "research", "does research",
        "body",
        # frontmatter written directly to control the extra keys:
    )
    # overwrite SKILL.md with the policy frontmatter
    (tmp_path / "research" / "SKILL.md").write_text(
        "---\nname: research\ndescription: d\ncontext: fork\n"
        "model: gpt-5.4-mini\nallowed-tools: read_file list_dir\n---\nBody."
    )
    (skill,) = discover(tmp_path)
    assert skill.fork is True
    assert skill.model == "gpt-5.4-mini"
    assert skill.allowed_tools == ["read_file", "list_dir"]


def test_discover_skips_a_skill_with_an_unknown_model(tmp_path):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text(
        "---\nname: bad\ndescription: d\nmodel: gpt-9-imaginary\n---\nBody."
    )
    write_skill(tmp_path, "good", "d", "b")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert [s.name for s in skills] == ["good"]
    assert warnings and "gpt-9-imaginary" in warnings[0]


def test_a_plain_skill_has_no_fork_or_policy(tmp_path):
    write_skill(tmp_path, "plain", "d", "b")
    (skill,) = discover(tmp_path)
    assert skill.fork is False and skill.model is None and skill.allowed_tools is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_skills.py -k "substitute_args or fork_model or unknown_model or plain_skill" -v`
Expected: FAIL — `substitute_args` undefined; `Skill` has no `fork`.

- [ ] **Step 3: Implement**

In `harness/skills.py`:

Add the import near the top:
```python
from harness.llm import CONTEXT_WINDOWS
```

Extend `Skill`:
```python
@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: Path
    fork: bool = False
    model: str | None = None
    allowed_tools: list[str] | None = None
```

Change `_parse` to return `(meta, body)` — replace its last line:
```python
    return meta, "\n".join(lines[end + 1 :]).strip()
```
and its signature/docstring first line:
```python
def _parse(text: str) -> tuple[dict, str]:
    """Split a skill file into (frontmatter dict, body). ..."""
```

In `discover`, replace the parse+construct region (the `try/except` through the `skills.append(...)`) with:
```python
        try:
            meta, body = _parse(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            on_warning(f"skipping skill {entry.name}: {error}")
            continue
        model = meta.get("model")
        if model is not None and model not in CONTEXT_WINDOWS:
            on_warning(f"skipping skill {entry.name}: unknown model {model!r}")
            continue
        name = meta["name"]
        body = body.replace("${SKILL_DIR}", str(base))  # a fixed path, resolved once
        if name in seen:
            on_warning(f"skipping skill {entry.name}: duplicate name {name!r}")
            continue
        seen.add(name)
        skills.append(
            Skill(
                name=name,
                description=meta["description"],
                body=body,
                dir=base,
                fork=meta.get("context") == "fork",
                model=model,
                allowed_tools=(
                    meta["allowed-tools"].split() if "allowed-tools" in meta else None
                ),
            )
        )
```

Add `substitute_args` (near `expand_body`):
```python
_ARG = re.compile(r"\$(ARGUMENTS|[1-9])")


def substitute_args(body: str, args: str) -> str:
    """Replace $ARGUMENTS (the whole string) and $1..$9 (whitespace-split
    positionals; missing → "") in one pass, so an arg that itself contains a
    $-token is never re-expanded."""
    parts = args.split()

    def repl(match: "re.Match[str]") -> str:
        token = match.group(1)
        if token == "ARGUMENTS":
            return args
        i = int(token)
        return parts[i - 1] if i <= len(parts) else ""

    return _ARG.sub(repl, body)
```

- [ ] **Step 4: Run the full skills suite**

Run: `uv run pytest tests/test_skills.py -v`
Expected: PASS — new tests plus every lesson-18/19 test (the new `Skill` fields have defaults; `skill_tool` still reads `.body`).

- [ ] **Step 5: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 20: parse fork/model/allowed-tools frontmatter + arg substitution

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `skills.py` — fork-capable `skill_tool`

**Files:** Modify `harness/skills.py`; Test `tests/test_skills.py`

**Interfaces:**
- Consumes: `substitute_args`, `Skill.fork/model/allowed_tools` (Task 3).
- Produces: `skill_tool(skills, run=None, fork_run=None) -> Tool` — schema has optional `args`; `execute(name, args="")` substitutes args, expands `` !`cmd` `` (if `run`), then forks (if `skill.fork`) via `fork_run(processed, model, allowed_tools)` else injects. `read_only=True`, `spawns_subagents=True`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py`:

```python
def test_skill_tool_substitutes_args_when_injecting(tmp_path):
    write_skill(tmp_path, "greet", "d", "hello $1, args=$ARGUMENTS")
    tool = skill_tool(discover(tmp_path))
    assert tool.execute(name="greet", args="world x") == "hello world, args=world x"


def test_skill_tool_forks_and_returns_the_subagent_answer(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: fork\nmodel: gpt-5.4-mini\n"
        "allowed-tools: read_file\n---\nresearch $1"
    )
    calls = {}
    def fake_fork(task, model, allowed_tools):
        calls.update(task=task, model=model, allowed_tools=allowed_tools)
        return "SUBAGENT ANSWER"
    tool = skill_tool(discover(tmp_path), fork_run=fake_fork)
    out = tool.execute(name="r", args="pdfs")
    assert out == "SUBAGENT ANSWER"
    assert calls == {"task": "research pdfs", "model": "gpt-5.4-mini", "allowed_tools": ["read_file"]}


def test_skill_tool_is_read_only_and_bars_nested_fork(tmp_path):
    write_skill(tmp_path, "x", "d", "b")
    t = skill_tool(discover(tmp_path))
    assert t.read_only is True and t.spawns_subagents is True


def test_a_fork_skill_without_fork_run_reports_an_error(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: fork\n---\nBody."
    )
    tool = skill_tool(discover(tmp_path))  # no fork_run
    assert tool.execute(name="r").startswith("Error")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_skills.py -k "substitutes_args_when_injecting or forks_and_returns or bars_nested or without_fork_run" -v`
Expected: FAIL — `skill_tool` has no `args`/`fork_run`; `spawns_subagents` is False.

- [ ] **Step 3: Implement**

In `harness/skills.py`, replace `skill_tool` (and its docstring) with:

```python
def skill_tool(
    skills: list[Skill],
    run: Callable[[str], str] | None = None,
    fork_run: Callable[[str, str | None, list[str] | None], str] | None = None,
) -> Tool:
    """The skill tool. `execute(name, args)` substitutes $ARGUMENTS/$1..$9, then
    runs the body's !`cmd` (if `run` is wired). A `context: fork` skill runs as a
    subagent via `fork_run` (returning its answer); other skills inject the text."""
    by_name = {s.name: s for s in skills}

    def execute(name: str, args: str = "") -> str:
        if name not in by_name:
            available = ", ".join(sorted(by_name)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        skill = by_name[name]
        processed = substitute_args(skill.body, args)
        if run is not None:
            processed = expand_body(processed, run)
        if skill.fork:
            if fork_run is None:
                return "Error: this skill runs as a subagent, which is unavailable here."
            return fork_run(processed, skill.model, skill.allowed_tools)
        return processed

    return Tool(
        name="skill",
        description=(
            "Load and run one of the available skills (listed in the system "
            "prompt) by name, optionally passing `args`. Do this before a task "
            "the skill governs. Some skills run shell commands to gather live "
            "context; some run as a subagent and return its result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name."},
                "args": {
                    "type": "string",
                    "description": "Optional arguments, substituted as $ARGUMENTS / $1..$9.",
                },
            },
            "required": ["name"],
        },
        execute=execute,
        read_only=True,          # the call injects text or delegates; sub actions are policy-gated
        spawns_subagents=True,   # a fork skill delegates — keep it out of subagents (no nested fork)
    )
```

(The `view_skill_tool = skill_tool` alias below is unchanged and still works — a fork skill invoked through it hits the `fork_run is None` error path.)

- [ ] **Step 4: Run the full skills suite**

Run: `uv run pytest tests/test_skills.py -v` → PASS (new fork/args tests + all prior).

- [ ] **Step 5: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 20: skill tool takes args and forks context:fork skills

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `main.py` — wire `make_llm` + `fork_run`

**Files:** Modify `main.py`

**Interfaces:**
- Consumes: `make_llm` (Task 1), `run_subagent` (Task 2), `skill_tool(skills, run, fork_run)` (Task 4).
- Produces: nothing downstream (terminal).

No unit tests (no test imports `main`); verified by diff-read + `uv run pytest` staying green + `import main` clean. The logic it leans on is unit-tested in Tasks 1/2/4.

- [ ] **Step 1: Imports**

`main.py`: change `from harness.llm import CodexAdapter` → `from harness.llm import CodexAdapter, make_llm`; add `run_subagent` to the `harness.tools.agent` import (`from harness.tools.agent import agent_tool, run_subagent`).

- [ ] **Step 2: Use the factory for the default client**

Replace `llm = CodexAdapter()` with:
```python
    llm = make_llm()  # the main-loop / agent-tool client (gpt-5.5)
```

- [ ] **Step 3: Move skill wiring after `policy`, and add `fork_run`**

Remove the `if skills: def run(...): ...; registry.append(skill_tool(skills, run))` block from the registry-build region (leave the base `registry = [read/write/list/bash]`). Then, **after** `tools["agent"] = agent_tool(...)` and **before** `with_hooks(tools, ...)`, insert:

```python
    if skills:
        # a skill's !`cmd` runs as a sandboxed preprocessor (config-authored,
        # session-approved — the lesson-18 model)
        def run(command: str) -> str:
            return run_sandboxed(command, sandbox)

        # a context:fork skill runs as a subagent: its body is the task, `model`
        # picks the client, `allowed-tools` filters the tool set. run_subagent
        # applies the recursion guard and the ask->deny policy.
        def fork_run(task: str, model: str | None, allowed_tools: list[str] | None) -> str:
            sub_tools = (
                tools
                if allowed_tools is None
                else {n: t for n, t in tools.items() if n in allowed_tools}
            )
            return run_subagent(
                task,
                make_llm(model),
                sub_tools,
                policy=policy,
                system=lambda: current_subagent_prompt(workspace, context_sections),
                on_tool_call=observe_sub_tool_call,
                compact_threshold=compact_threshold,
            )

        tools["skill"] = skill_tool(skills, run, fork_run)
```

(`skill_tool` is added to `tools` before `with_hooks`, so the skill tool call itself is hooked; its `spawns_subagents=True` keeps it out of subagents.)

- [ ] **Step 4: Verify**

Run: `uv run pytest` → whole suite green (no test imports `main`; this confirms the modules stay consistent).
Run: `uv run python -c "import main"` → clean import.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "lesson 20: wire make_llm and the skill fork_run in main

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Reviewer notes (diff-read, Task 5)

- `make_llm()` (no slug) is the default client; `make_llm(model)` in `fork_run` picks the fork's model.
- The skill wiring moved *after* `policy`/`agent_tool` because `fork_run` closes over `policy`/`tools`; the tool is still added before `with_hooks`.
- `fork_run` filters `tools` by `allowed_tools`, then `run_subagent` applies the `spawns_subagents` recursion guard — a fork subagent gets `allowed_tools ∩ (not spawns_subagents)`, and never the skill tool itself.

## Self-Review

- **Spec coverage:** args syntax + scope (Task 3 `substitute_args`, Task 4 execute) ✓; fork spawns subagent w/ model+allowed-tools (Tasks 4/5) ✓; real multi-model (Task 1) ✓; `run_subagent` shared (Task 2) ✓; `spawns_subagents=True`/nested-fork bar (Task 4) ✓; `_parse`→dict (Task 3) ✓; unknown-model skip (Task 3) ✓; offline `make_llm` (Task 1) ✓; `main.py` diff-review (Task 5) ✓.
- **Placeholder scan:** none — full code per step. The one soft spot (Task 2's fake-LLM shim) explicitly says to reuse the test file's existing fake if present.
- **Type consistency:** `fork_run(task, model, allowed_tools)` signature identical in the `skill_tool` param type, the Task-4 fake, and the Task-5 closure; `run_subagent(...)` keyword args match between `agent_tool` and `fork_run`; `Skill` field names (`fork`/`model`/`allowed_tools`) consistent across `discover`, `skill_tool`, and tests.
