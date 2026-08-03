# Skill Args & Fork-Skills (Lesson 20)

**Date:** 2026-07-09
**Status:** Approved pending user review

**Arc:** Final lesson of the full-skill-tool arc — 18 `` !`cmd` `` injection, 19
directories + `${SKILL_DIR}`, **20 args + frontmatter-as-policy** (this spec).

## Goal

Two capabilities:
1. **Args** — the skill tool takes an `args` string; `$ARGUMENTS`/`$1`…`$9` are
   substituted into the body at invocation (matching Claude Code's
   `Skill(skill, args)`).
2. **Fork-skills** — a skill whose frontmatter declares `context: fork` runs as a
   **subagent** (its body is the task), configured by `model` and `allowed-tools`,
   returning the subagent's answer. Skills without `context: fork` inject text
   (lessons 18–19 behavior).

## Model data (from the live `/codex/models` probe, 2026-07-09)

API-usable slugs, all context window 272000: `gpt-5.5` (current default),
`gpt-5.4`, `gpt-5.4-mini`. (`gpt-5.3-codex-spark` is not API-exposed;
`codex-auto-review` is hidden.) So `CONTEXT_WINDOWS` gains `gpt-5.4` and
`gpt-5.4-mini` — `gpt-5.4-mini` is the natural "run this subtask cheaper" model.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Arg syntax | `$ARGUMENTS` = whole string; `$1`…`$9` = whitespace-split positionals; single-pass `re.sub` (no re-substitution of arg text) | Claude Code parity; single pass avoids an arg that contains `$2` being re-expanded |
| Arg scope | Substituted into the whole body **including** `` !`cmd` ``, before `expand_body` | yc's choice: the human approved the command *template* (`git log $1`) at session start; the arg fills it at call (sandboxed) |
| Fork model | `context: fork` → run the skill as a subagent; else inject | The only placement where `allowed-tools`/`model` cohere with our injection model |
| `model` | Real multi-model: `CONTEXT_WINDOWS` += `gpt-5.4`, `gpt-5.4-mini`; a `make_llm(slug)` factory builds a `CodexAdapter` per slug (cached). Unknown slug on a skill → skill skipped with a warning at discovery | yc's choice; `CodexAdapter` already takes `model` at construction, so no `complete()` change |
| `allowed-tools` | Space-separated tool names; filters the fork subagent's tool dict; absent → full (minus recursion-guarded) set | Simple, mirrors the hooks `matcher` style |
| Subagent spawn | Extract `run_subagent(...)` shared by `agent_tool` and the skill `fork_run` | `fork_run` and `agent_tool.execute` are the same run_turn+ABORTED shape; DRY |
| `read_only` | `skill_tool` stays `read_only=True` even for fork skills | Like `agent_tool`: delegation itself changes nothing; the subagent's actions are gated by the shared policy (ask→deny) |
| Nested fork | `skill_tool.spawns_subagents = True` → subagents never receive the skill tool | Prevents unbounded nested fork (a fork subagent forking again); mirrors `agent_tool`. Tradeoff: subagents lose skill access — acceptable, a subagent already gets its task spelled out |
| `_parse` return | Return `(meta: dict, body)` instead of `(name, description, body)` | The frontmatter now carries `context`/`model`/`allowed-tools`; a dict is the extensible shape |
| Fork body's `` !`cmd` `` | Expanded in the parent (its existing session-approval) before becoming the fork task | Consistent with injection; the command output rides into the subagent's task |

## Components

**`harness/llm.py`**
- `CONTEXT_WINDOWS` += `"gpt-5.4": 272_000`, `"gpt-5.4-mini": 272_000`.
- `make_llm(slug: str | None = None, *, build: Callable[[str], LLMClient] = CodexAdapter) -> LLMClient`
  — returns a client for `slug` (default `gpt-5.5`), cached per slug so repeated
  forks reuse one client. `build` is injectable so the **offline** suite can test
  `make_llm`'s default/caching without constructing a real `CodexAdapter` (whose
  `__init__` reads `~/.codex/auth.json`).

**`harness/tools/agent.py`**
- Extract `run_subagent(task, llm, tools, *, policy, system=None, on_tool_call=None, max_iterations=20, compact_threshold=None, keep_recent=8) -> str`: the recursion-guard filter (`spawns_subagents`), the `run_turn([], task, …, asker=None)` call, and the `ABORTED_PREFIX` → error mapping — everything `agent_tool.execute` does today. `agent_tool` calls it.

**`harness/skills.py`**
- `_parse(text) -> tuple[dict, str]` — returns `(meta, body)`; still raises unless `name` and `description` are present.
- `Skill` gains `fork: bool`, `model: str | None`, `allowed_tools: list[str] | None`.
- `discover` reads the new fields from `meta`: `fork = meta.get("context") == "fork"`, `model = meta.get("model")`, `allowed_tools = meta["allowed-tools"].split() if present`. A skill whose `model` is set but not in `CONTEXT_WINDOWS` is skipped with a warning (like any malformed skill).
- `substitute_args(body, args) -> str` — single-pass `re.sub(r"\$(ARGUMENTS|[1-9])", …)`; `$ARGUMENTS` → `args`, `$N` → the Nth whitespace-split token or `""`.
- `skill_tool(skills, run=None, fork_run=None) -> Tool` — schema gains an optional `args` string. `execute(name, args="")`:
  1. `processed = substitute_args(skill.body, args)`
  2. if `run` is not None: `processed = expand_body(processed, run)`
  3. if `skill.fork`: `return fork_run(processed, skill.model, skill.allowed_tools)` (or a clear error if `fork_run is None`)
  4. else: `return processed`
  Still `read_only=True`; now `spawns_subagents=True`.

**`main.py`**
- Replace `llm = CodexAdapter()` with the `make_llm` factory (default = `gpt-5.5`).
- Build a `fork_run(task, model, allowed_tools) -> str` closure: filter `tools` to `allowed_tools` (when given), then `run_subagent(task, make_llm(model), filtered_tools, policy=policy, system=current_subagent_prompt(...), on_tool_call=observe_sub_tool_call, …)`.
- Wire both `run` and `fork_run` into `skill_tool(skills, run, fork_run)`.

## Data flow — a fork skill

```
skill(name="summarize-pdfs", args="reports/")
  → substitute_args: "$ARGUMENTS" → "reports/"
  → expand_body: any !`cmd` runs in the parent (session-approved)
  → context: fork → fork_run(task=processed, model="gpt-5.4-mini",
                             allowed_tools=["read_file","list_dir"])
      → run_subagent: fresh run_turn on a gpt-5.4-mini CodexAdapter,
        tools = {read_file, list_dir} (recursion-guarded), policy ask→deny
      → returns the subagent's final answer
  → that answer is the skill tool's result
```

## Error handling

- Unknown `model` slug on a skill → discovery warning, skill skipped (never a
  crash at fork time).
- A `context: fork` skill invoked when `fork_run is None` (the `view_skill_tool`
  compat alias) → returns an error string; injection/prose skills are unaffected.
- `allowed-tools` naming a tool that doesn't exist → simply absent from the
  filtered set (no error); an all-invalid list → subagent runs with no tools and
  answers from its task.
- `$N` with no Nth positional → `""` (documented).
- Subagent exhausts `max_iterations` → `run_subagent` returns the same "no final
  answer" error `agent_tool` already returns.

## Testing (`tests/`)

`test_skills.py`:
- `substitute_args`: `$ARGUMENTS`; `$1`/`$2`; missing `$3` → `""`; a `$1` inside a
  `` !`cmd` `` is filled before `cmd_blocks`/execution.
- `_parse` returns a `meta` dict carrying `context`/`model`/`allowed-tools`.
- `discover`: `fork`/`model`/`allowed_tools` populated; a skill with an unknown
  `model` slug is skipped with a warning.
- `skill_tool`: schema has `args`; a non-fork skill injects with args substituted;
  a fork skill calls `fork_run(processed, model, allowed_tools)` (a fake capturing
  the call) and returns its answer; `read_only is True`; `spawns_subagents is True`.

`test_agent_tool.py` (regression + new): `agent_tool` still delegates correctly
after the `run_subagent` extraction; `run_subagent` filters `spawns_subagents`
tools and maps `ABORTED_PREFIX` to the error string.

`test_llm_contract.py` (or similar): with a fake `build`, `make_llm("gpt-5.4-mini")`
builds for that slug, `make_llm()` defaults to `gpt-5.5`, and a repeated call
returns the cached client — all offline (no real `CodexAdapter`, no auth). The
unknown-`model`-slug rejection is tested in `discover` via `CONTEXT_WINDOWS`
membership (a dict check, no client construction), so it too is offline.

The `main.py` `fork_run`/`make_llm` wiring is diff-reviewed (no test imports
`main`), but the logic it leans on (`run_subagent`, the allowed-tools filter,
`make_llm`) is unit-tested.

## Out of scope (deferred / not doing)

- Per-call `model` in `LLMClient.complete()` (we build an adapter per slug instead).
- `gpt-5.3-codex-spark` (not API-exposed) and `codex-auto-review` (hidden).
- User-invocation / slash-command routing of skills.
- Frontmatter beyond `context`/`model`/`allowed-tools` (e.g. `disallowed-tools`,
  per-skill hooks).
