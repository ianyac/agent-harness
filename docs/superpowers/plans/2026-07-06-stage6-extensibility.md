# Stage 6: Extensibility — Implementation Plan (Lessons 14–15)

> Standing protocol: teacher writes code and tests and explains; learner
> reviews every diff before it commits; quiz on built material at each tag.
> Task-assigning messages end with a numbered TODO list.

**Goal:** Open the harness to its user without opening the loop. Lesson 14
adds hooks — user-configured commands that observe, block, or inject
context at lifecycle points. Lesson 15 adds skills — progressive
disclosure of instructions: metadata always in the system prompt, full
content pulled into context on demand. Both land WITHOUT touching
`run_turn`: the whole stage is a demonstration that the loop's existing
seams (the tool registry, `extra_sections`, the caller-side lifecycle)
are sufficient extension surface.

**Architecture:** `harness/hooks.py` loads `hooks.json` from the workspace
root and exposes two things: lifecycle event runners (`session_start`,
`stop`) called by `main.py`, and a registry decorator that wraps each
tool's `execute` with the `pre_tool_use` / `post_tool_use` hooks.
`harness/skills.py` discovers `skills/*.md`, feeds name+description into
the system prompt via `extra_sections`, and exposes a read-only `view_skill`
tool that returns a skill's full body on demand.

## Decisions under review

1. **Hooks are workspace config, not code:** `hooks.json` at the workspace
   root (visible, versionable — unlike the runtime artifacts under
   `.agent/`). Schema: `{"<event>": [{"matcher": "<optional tool-name
   regex>", "command": "<shell command>"}]}` with exactly the four spec
   events: `pre_tool_use`, `post_tool_use`, `session_start`, `stop`.
   Missing file = no hooks; malformed file = clean startup error.
2. **The hook contract mirrors the industry shape:** the command runs via
   the shell, receives the event payload as JSON on stdin, and speaks
   through its exit code. Exit 0 = proceed; exit 2 on `pre_tool_use` =
   block, with the hook's stderr delivered to the model as the tool
   result (`Blocked by hook: ...`) — failure-as-information, lesson 8.
   `post_tool_use` and `stop` observe only. `session_start` stdout is
   injected into the system prompt as an extra section — the hook way to
   say "here's context the harness can't know".
3. **Tool hooks live in a registry decorator, not in the loop.**
   `with_hooks(tools, hookset)` returns wrapped Tools whose execute runs
   pre-hooks (block short-circuits), the real tool, then post-hooks.
   `run_turn` is untouched — the permission gate (human consent) still
   runs first in the loop, then hooks (user automation), then execution.
   Ordering note: a hook cannot approve what the human denied; it can
   only narrow further. The lesson's thesis: the L7 permission gate is
   just a built-in pre-tool hook — and here is the general mechanism.
4. **Hooks run UNSANDBOXED; blocking points still fail closed**
   (learner's ruling 2026-07-06, reverting the same-day sandbox ruling).
   Rationale for the revert: hook utility lives outside the workspace —
   notifications, external logs, network calls — and the bash sandbox
   (workspace-confined writes, no network) would gut most real hooks.
   Failure semantics unchanged: `pre_tool_use` fails CLOSED (a crashed
   or timed-out hook blocks the call — a policy hook must never silently
   stop enforcing, especially in acceptAll mode where it may be the only
   gate); `session_start` fails CLOSED (aborts startup with a clean
   error); `post_tool_use` and `stop` are pure observers with nothing to
   halt — they fail LOUD. Timeout 10s.
   **Accepted risk, documented:** hooks.json is model-reachable (the
   agent can write it; a cloned repo can ship it), so unsandboxed hooks
   are an escalation path from sandboxed model to unsandboxed execution
   at the next session start. Mitigation adopted (learner's ruling
   2026-07-07): a startup approval gate — before ANY hook runs
   (session_start included), the REPL prints every configured command
   labeled by event and requires an explicit "y"; decline or EOF
   disables hooks for the session (the harness still runs). The gate
   applies in every permission mode: tool consent (acceptAll) and
   config-as-code consent are different axes.
5. **Skills are flat markdown files with minimal frontmatter:**
   `skills/<name>.md` with `---` frontmatter carrying `name:` and
   `description:` as plain strings — parsed by ~15 lines of our own code
   rather than a YAML dependency. Malformed skills are skipped with a
   warning, never fatal.
6. **Progressive disclosure via the two existing seams:** discovery
   builds an "Available skills" section (name + description only) that
   rides `extra_sections` — the seam built in lesson 10 finally meets its
   intended customer. Full bodies enter context only as the result of a
   read-only `view_skill` tool call (`view_skill(name=...)`), so an
   unused skill costs one metadata line, not its whole body — lesson 11's
   economy applied to instructions.
7. **`view_skill` is a plain registry entry** (read_only=True, denied
   never, unknown names return an error string listing what exists).
   Subagents inherit it like any tool, so a delegated task can load the
   same instructions.

## Lesson 14: Hooks

### Task 14.1: harness/hooks.py + tests
- `HookSet` loaded from `hooks.json` (`load_hooks(path)`); `run_event()`
  for lifecycle events returning injected stdout; `with_hooks(tools,
  hookset)` registry decorator implementing block/observe.
- Tests (offline, scripted with tiny shell commands): exit-2 pre-hook
  blocks and the model sees stderr; exit-0 proceeds; matcher scopes a
  hook to named tools; post-hook receives result JSON; session_start
  stdout comes back for injection; a crashed or timed-out pre-hook
  blocks the call (fail closed); a crashed post-hook warns and proceeds;
  missing config is an empty HookSet; hook commands run through the
  sandbox wrapper (a write outside the workspace is refused).
- Review gate, commit.

### Task 14.2: REPL wiring + live smoke
- `main.py`: load hooks at startup, wrap the registry, inject
  session_start stdout into both system prompts' extra sections, fire
  `stop` after each completed turn.
- Live smoke: a `hooks.json` blocking `write_file` (agent reports the
  block), plus a `session_start` hook injecting a fact the model then
  knows.
- Review gate, quiz, commit, tag `lesson-14`.

## Lesson 15: Skills

### Task 15.1: harness/skills.py + tests
- `discover(skills_dir) -> list[Skill]` (name, description, body);
  frontmatter parser; `skills_section(skills) -> str` for the prompt;
  `view_skill_tool(skills) -> Tool` returning bodies on demand.
- Tests: discovery + parsing; malformed file skipped with warning;
  section lists metadata only (bodies absent); the tool returns a body;
  unknown skill name returns an error naming available skills; empty
  skills dir produces no section and no tool.
- Review gate, commit.

### Task 15.2: REPL wiring + live smoke + curriculum close
- `main.py`: discover `skills/` at startup, thread the section into both
  prompts, register the tool.
- Live smoke: a demo skill (e.g. commit-message conventions); ask the
  agent something the skill governs; verify it loads the skill on demand
  and follows it.
- Review gate, quiz, commit, tag `lesson-15`. Stage 6 done = curriculum
  complete; the TS speedrun unparks per the spec.

## Stage 6 done when
- Default suite green offline; gated suite green live.
- A hook observably blocks a tool call and injects session context; a
  skill is listed cheaply, loaded on demand, and followed.
- Tags `lesson-14`, `lesson-15`; `run_turn` unchanged since lesson 13;
  transport still quarantined; the loop still tool-name-free.
