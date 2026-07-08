# Skill Tool — Dynamic Command Injection (Lesson 18)

**Date:** 2026-07-08
**Status:** Approved pending user review

**Arc:** Lesson 18 of the "full skill tool" arc, decomposed from the request
"implement the full skill tool":

- **18 — dynamic `!` injection** (this spec): the tool stops being a read and
  starts running code.
- **19 — bundled dirs + args**: skill becomes a directory (`SKILL.md` +
  `scripts/`/`references/`), `${SKILL_DIR}`, `$1`/`$ARGUMENTS`, third-tier
  disclosure. *Out of scope here.*
- **20 — frontmatter as policy**: `allowed-tools` gating, `model` override,
  `context: fork` subagent. *Out of scope here.*

Each lesson gets its own spec → plan → implementation → `lesson-NN` tag.

## Goal

Turn the read-only `view_skill` tool (lesson 15) into an executing `skill`
tool. A skill body may contain `` !`cmd` `` blocks; at invocation time each is
replaced by the command's live output before the body is handed to the model.

This is the conceptual leap the arc is built around: the *same* tool that was a
permission-free content fetch in lesson 15 now runs sandboxed shell during
load. The lesson is visible in one attribute — `read_only` flips `True →
False` — and its consequences (a security gate, a sandbox, honest wording)
follow from that flip.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Evolve vs. new tool | Evolve `view_skill` → `skill` (rename, `read_only=False`) | Smallest diff; the flag flip *is* the lesson. A second tool would make the model guess which to call for the same skill. |
| When injection runs | At invocation, every `execute()` call | Liveness. A `` !`git diff` `` captured once at discovery is stale by the time the model invokes the skill — that staleness is the bug that kills "dynamic." |
| Execution path | Reuse the bash tool's sandboxed runner, cwd = workspace | One code path, identical output contract; failures render inline for free. Matches the hooks/MCP rule that config commands resolve in the workspace the human read. |
| Security gate | Session-start approval mirroring `approve_hooks`/`approve_mcp`, but **sandboxed**; **content-gated** (only skills containing `!` blocks) | Skill files are clone-shippable and model-writable, like `hooks.json`. Config-authored code deserves one-time human consent even when sandboxed. Pure-prose skills carry no execution, so they need no gate and stay always-available. |
| Decline behavior | Fail-open: drop the executable skills, keep pure-prose ones, session continues | A skill is a capability, not policy (the MCP line, not the hooks line). Invariant: the sandbox runner is reachable only from an *approved* skill, so no un-consented command can run. |
| Approval wording | Generalize `approve_commands` with a `sandboxed` flag | The existing gate prints "(unsandboxed)". Skills are sandboxed; printing the truth for each caller keeps the human's consent informed. Backward-compatible (default preserves hooks/MCP wording). |
| `args` / user-invocation | Deferred to lesson 19; schema stays `{name}` | YAGNI for the injection lesson. |

## Components

**`harness/skills.py`** (home of the injection logic):

- `_CMD` — a compiled regex matching `` !`cmd` `` (a bang, then a
  backtick-quoted command): a `!`, a backtick, a run of non-backtick
  characters captured, a closing backtick.
- `cmd_blocks(body) -> list[str]` — the commands a body will run, in order.
- `has_cmd_blocks(body) -> bool` — does this body execute anything?
- `expand_body(body, run) -> str` — replace each `` !`cmd` `` with `run(cmd)`.
  Pure; `run` is injected so tests need no shell. Never lets an exception
  escape (a raising `run` becomes an inline `[skill command failed: …]`).
- `skill_tool(skills, run) -> Tool` — was `view_skill_tool`. `name="skill"`,
  `read_only=False`; `execute()` returns `expand_body(body, run)`.

**`harness/tools/bash.py`**:

- Lift the existing sandboxed runner to a reusable
  `run_sandboxed(command, sandbox) -> str`. Shared by `bash_tool` and skill
  injection. Skill execution inherits the bash runner's existing tests and
  calls it with the runner's default timeout and output limit.

**`main.py`**:

- `approve_commands(source, noun, commands, *, sandboxed=False)` — add the
  flag; print `"(sandboxed)"` or `"(unsandboxed)"`.
- `approve_skill_execution(skills)` — build the command list from executable
  skills, call `approve_commands(..., sandboxed=True)`.
- Wire `run = lambda cmd: run_sandboxed(cmd, sandbox)` into `skill_tool`.
- Content-gated drop-on-decline:
  ```python
  executable = [s for s in skills if has_cmd_blocks(s.body)]
  if executable and not approve_skill_execution(executable):
      print(f"(skill execution declined — dropping {len(executable)} executable skill(s))")
      skills = [s for s in skills if not has_cmd_blocks(s.body)]
  ```

## Data flow

```
model → skill(name="review-staged")
      → execute() → expand_body(body, run)
            for each !`cmd`:  run(cmd)  →  sandbox @ workspace  →  "exit code: 0\n<output>"
      → tool result = body with live output substituted
      → model reads the current diff, not a startup snapshot
```

## Error handling

- **`!cmd` fails** (nonzero exit, not found): the bash contract returns
  `"exit code: N\n<combined stdout+stderr>"`; it is substituted inline. The
  model sees the failure and can react — tool results are ground truth,
  errors included. No exit-code special-casing.
- **`run` raises** (e.g. sandbox spawn error): `expand_body` substitutes
  `[skill command failed: <err>]`. One bad block cannot sink the load — the
  same soft-failure spirit as `discover()` skipping a malformed file.
- **Unknown skill name**: unchanged lesson-15 soft `Error: no skill named …`.
- **Declined / non-interactive stdin**: executable skills dropped, notice
  printed; pure-prose skills unaffected.

## Testing

**Unit-tested in `tests/test_skills.py`** (the injection core — the lesson):

- `expand_body`: single block substituted; multiple blocks in order; no
  blocks → body byte-identical; lone/unmatched backtick left untouched;
  multi-line `run` output substituted verbatim; a raising `run` → inline
  marker, no exception escapes.
- `cmd_blocks` / `has_cmd_blocks`: commands extracted in order; true/false
  detection.
- `skill_tool`: `execute()` injects via a fake `run`; `read_only is False`;
  unknown name still returns the lesson-15 soft `Error`.

**Verified by diff-read, not unit tests** — matching how `approve_hooks` /
`approve_mcp` already live in `main.py` (no test imports `main`):

- `approve_skill_execution`, the `sandboxed=True` wording, and the
  drop-on-decline filter. **Regression note for the reviewer:** hooks and MCP
  must still print "(unsandboxed)".

This split is stated so no one mistakes the `main.py` glue for unit-covered.

## Out of scope (deferred, YAGNI)

- `args` / `$ARGUMENTS` / `$1` — lesson 19.
- Bundled skill directories, `${SKILL_DIR}`, referenced files — lesson 19.
- Frontmatter as policy (`allowed-tools`, `model`, `context: fork`) — lesson 20.
- Escape syntax for a literal `` !`…` `` you don't want run; nested backticks.
- User-invocation / slash-command routing.
