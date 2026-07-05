# Stage 3: Safety — Implementation Plan (Lessons 7–9)

> Standing protocol: teacher writes code and tests and explains; learner
> reviews every diff before it commits; quiz on built material at each tag.
> Task-assigning messages end with a numbered TODO list.

**Goal:** Make the Stage 2 agent usable on a machine you care about. Lesson 7
gates *intent* (the model wants to run something — may it?), lesson 8 turns
failures into information the model can act on, lesson 9 contains *execution*
(even a permitted action can't damage what the sandbox walls off).

**Architecture:** The gate is a single choke point inside `run_turn`, between
argument parsing and `execute`. Policy is data + one injected callable
(the asker), never code baked into the loop — same seam philosophy as
`on_tool_call`, and for the same reason: subagents and tests need different
policies through the same loop. Sandboxing wraps only `bash` (and later any
tool that shells out) via macOS `sandbox-exec`.

**Tech Stack:** unchanged. Python 3.14, `uv`, `pytest`; no new dependencies.

## Decisions under review (the judgment calls in this plan)

1. **Tools declare `read_only: bool`** (new `Tool` field, default `False`).
   The tool is the authority on whether it mutates; policy consumes that
   flag. Alternative rejected: name-based classification in policy config
   (drifts from reality when tools change).
2. **The gate returns a decision, the loop enforces it.** New
   `harness/permissions.py`: `PermissionPolicy` with
   `decide(tool: Tool, args: dict) -> "allow" | "deny" | "ask"`. On "ask",
   the loop calls an injected `asker(tool_name, args) -> bool`. The loop
   never prompts anyone itself (subagent silence, testability).
3. **Denial is a tool result, not an exception.** A denied call appends
   `{"role": "tool", ..., "content": "Permission denied by user: ..."}` and
   the loop continues — the model gets to adapt (ask differently, explain,
   give up). This is deliberately the first taste of lesson 8's theme.
4. **Three modes, not five**: `default` (read-only tools run, everything
   else asks), `acceptAll` (everything runs — tests and future subagent
   use), `readOnly` (read-only runs, everything else denied without
   asking). Mode names mirror Claude Code's; the count stays minimal.
5. **Session allowlist**: answering an ask with "always" records
   `(tool_name)` for the session so repeat calls skip the prompt.
   Argument-pattern rules (à la `Bash(git status:*)`) are explicitly out of
   scope — noted as the production feature ours simplifies.
6. **Lesson 8 keeps the loop's crash-to-result conversion inside
   `run_turn`**, not inside tools: tools stay simple functions that raise;
   the loop is the single place where exceptions become
   `{"role": "tool", "content": "Error: ..."}`. API retries live in the
   adapter (transport concern); the iteration cap becomes a graceful
   final message instead of `RuntimeError`.
7. **Lesson 9 sandboxes bash only** (`sandbox-exec` profile: writes
   confined to the workspace, network off by default), because bash is the
   only tool that executes foreign programs. Python fs tools get path
   confinement (workspace-root check) instead of an OS sandbox — cheaper
   and sufficient for tools whose code we wrote. Trade-off named in the
   lesson: confinement-in-Python is advisory, the OS sandbox is not.

## Lesson 7: Permissions

### Task 7.1: read_only flags + PermissionPolicy + the gate
- `Tool` gains `read_only: bool = False`; `read_file`/`list_dir` set it.
- `harness/permissions.py`: `PermissionPolicy(mode, session_allowlist)`,
  `decide()` per decision table above.
- `run_turn` gains `policy: PermissionPolicy | None` and
  `asker: Callable[[str, dict], bool] | None`; gate sits after JSON parse,
  before `on_tool_call`/`execute`. `None` policy = today's behavior
  (documented as the no-gate escape hatch, used by tests that predate it).
- Tests (FakeLLM-scripted): allow path unchanged; deny appends denial
  result and the model's next reply sees it; ask→yes runs, ask→no denies;
  readOnly mode never invokes the asker; "always" answer populates the
  allowlist and skips the second ask; asker never called for read_only
  tools in default mode.
- Review gate, commit.

### Task 7.2: REPL asker + mode flag
- `main.py`: interactive asker (`y/n/a` prompt), `--mode` CLI flag
  (default `default`).
- Live smoke: ask the agent to write a file → REPL prompts → approve once
  with `a` → second write skips the prompt.
- Review gate, quiz, commit, tag `lesson-07`.

## Lesson 8: Failure is information

### Task 8.1: crash-to-result in the loop
- `execute` exceptions, unknown tool names, and malformed argument JSON
  each become error-text tool results; the loop continues. Iteration cap
  becomes a final assistant-visible message, not a raise.
- Tests: each failure class scripted via FakeLLM; model's next turn sees
  the error text; a tool that raises twice then succeeds completes the turn.
- Review gate, commit.

### Task 8.2: transport retries + cancellation
- Adapter: retry on transport errors/429/5xx with capped backoff (the
  ConnectError from lesson 4's live crash becomes a retry); honor
  Retry-After when present.
- REPL: Ctrl-C mid-turn cancels the turn, keeps the session (messages
  list intact minus the unfinished turn), Ctrl-C at prompt exits.
- Review gate, quiz, commit, tag `lesson-08`.

## Lesson 9: Sandboxing

Design (chosen 2026-07-05): a `Sandbox` seam so the confinement *policy* is
platform-neutral and testable while OS *enforcement* is one swappable call —
the `LLMClient` pattern aimed at the OS. Backends built: `MacOSSandbox`
(sandbox-exec, live-tested), `NoSandbox` (pass-through fallback + unit tests).
`LinuxSandbox` (bwrap) is a documented `NotImplementedError` stub — building
an unrunnable/untestable backend would violate the no-unverified-code rule.

### Task 9.1: the Sandbox seam + backends + bash wiring
- `harness/sandbox.py`:
  - `Sandbox` Protocol: `wrap(command: str) -> list[str]` (shell command →
    argv that runs it confined).
  - `SandboxPolicy(workspace: Path, allow_network: bool = False)` — the
    platform-neutral confinement policy.
  - `macos_profile(policy) -> str` — pure function building the sandbox-exec
    profile string (deny default, allow workspace writes + /tmp, network per
    flag). Fully unit-tested without invoking the OS.
  - `MacOSSandbox(policy).wrap(cmd)` → `["sandbox-exec", "-p", profile,
    "sh", "-c", cmd]`.
  - `NoSandbox().wrap(cmd)` → `["sh", "-c", cmd]`.
  - `LinuxSandbox` stub raising `NotImplementedError` with a bwrap TODO.
  - `default_sandbox(policy)` picks by `sys.platform`.
- `bash` tool takes an optional `sandbox: Sandbox`; runs `sandbox.wrap(cmd)`
  via `subprocess.run` (no more `shell=True` — argv comes pre-formed). Result
  string shape unchanged.
- Tests: `macos_profile` contents (policy → profile string) cross-platform;
  `NoSandbox` round-trips a command; bash with `NoSandbox` behaves as today.
  macOS-only (skipped elsewhere): write inside workspace succeeds; write
  outside → non-zero exit in-band; network blocked when disabled, allowed
  when flagged.
- Review gate, commit.

### Task 9.2: fs path confinement + stage close
- fs tools resolve paths against a workspace root and refuse escapes
  (including `..` and symlink tricks via `Path.resolve`).
- Live smoke: agent asked to "clean up /tmp" gets refusals it can read.
- Review gate, quiz, commit, tag `lesson-09`.

## Planned follow-up (deferred, committed 2026-07-05)

**Lesson "9.5": sandbox-first execution + prompt escalation.** Once both
layers exist independently (this stage), couple them by *escalation*, the
way Claude Code does: run bash sandboxed-and-silent by default (a contained
command has bounded blast radius, so no prompt), and only invoke the `asker`
when a command hits a sandbox wall (needs network, or a write outside the
workspace). Makes `_permitted` sandbox-aware: try contained → escalate to
the human only on the genuinely consequential calls. Kills prompt fatigue.
Pairs naturally with the deferred argument-pattern allowlist (L7 decision 5),
since both are about prompting the human *less, but smarter*. Deferred
deliberately: the escalation earns its complexity only once prompt fatigue is
felt, and it can't be understood until both layers are built and tested alone
(this stage builds them independent, ANDed — the prerequisite, not a detour).

## Stage 3 done when
- Default suite green offline; gated suite green live.
- REPL: denied action produces a model-visible denial; killed network
  mid-turn retries then degrades gracefully; bash cannot write outside the
  workspace even when permitted by the user.
- Tags `lesson-07`..`lesson-09`; loop still tool-name-free; transport still
  quarantined in `harness/llm.py`.
