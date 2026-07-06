# Stage 5: Scale-out — Implementation Plan (Lessons 12–13)

> Standing protocol: teacher writes code and tests and explains; learner
> reviews every diff before it commits; quiz on built material at each tag.
> Task-assigning messages end with a numbered TODO list.

**Goal:** Scale the harness beyond one context and one process. Lesson 12
adds subagents — a task that would pollute the parent's context with 50k
tokens of exploration comes back as a 200-token answer. Lesson 13 makes the
conversation durable — JSONL transcripts on disk, resumable across process
restarts. Both are the scale-out answers to lesson 11's scarcity: compaction
shrinks history after the fact; subagents prevent pollution up front;
sessions stop a process exit from being amnesia.

**Architecture:** The subagent is just a tool (`harness/tools/agent.py`) —
the loop invariant holds (the loop never knows a tool's name), and the
"inner harness" is the same `run_turn` on a fresh messages list. Sessions
live in `harness/session.py` as an append-only JSONL event log under
`<workspace>/.agent/sessions/`, folded back into a messages list on resume.

## Decisions under review

1. **The subagent is a registry tool, not a loop feature.** `agent_tool()`
   closes over everything the inner loop needs (llm, tools, policy, asker,
   system-prompt builder) and its `execute(task: str)` runs `run_turn` on a
   fresh empty messages list, returning ONLY the final reply's content as
   the tool-result string. The parent never sees the sub's transcript —
   that's the whole point. Failures inside the sub come back as error
   strings (lesson 8 discipline), never exceptions.
2. **No recursion, structurally.** The subagent's registry is the parent's
   minus the `agent` tool itself. Not a depth flag to check — a capability
   the inner loop simply doesn't have.
3. **Subagents run in the background and never prompt the human**
   (learner's ruling 2026-07-06, revising the earlier shared-asker
   design after the lesson-13 review). The sub gets the parent's
   sandbox-wrapped tools and `PermissionPolicy`, but no asker: a
   permission decision that would ask resolves to deny, delivered to the
   sub as an ordinary tool result it reports in its answer. All consent
   prompts therefore happen at parent level, where the human sees the
   context they are approving. This closes a consent-scope escalation:
   delegate a benign-looking task, harvest an "always" at the sub's
   prompt (which the human mentally scopes to the subtask), then exploit
   the per-tool, argument-blind grant parent-side. Grants flow down via
   the shared allowlist; nothing can flow up. `on_tool_call` is still
   forwarded so the REPL prints the sub's tool use with a depth marker.
4. **The sub gets its own system prompt through the lesson-10 seam.**
   `build_system_prompt(env, extra_sections=[...])` with a subagent role
   section — the first real customer of `extra_sections` before skills
   (lesson 15) arrive.
5. **Session transcript = append-only event log, written at turn
   boundaries.** One JSON line per message, appended only when a turn
   completes (the plain-assistant boundary — the same invariant compaction
   cuts on and Ctrl-C rolls back to; this is its third customer). A crash
   or cancel mid-turn therefore leaves the transcript ending at the last
   completed exchange, matching the in-memory rollback semantics for free.
6. **Compaction is recorded as an event, not an edit.** The log is
   append-only; when a turn compacted, the log gains a compaction event
   line (the summary message + the kept-from position) instead of
   rewriting history. Resume folds the log: replay message lines, apply
   compaction events. Teaches event-sourcing vs. state-snapshot honestly.
7. **Resume rebuilds the system prompt fresh.** The transcript stores
   conversation messages only — env facts (cwd, date) are re-read at
   startup like any session, so a transcript resumed tomorrow gets
   tomorrow's date (the lesson-10 injection discipline, again). CLI:
   `--resume <id>` plus `--continue` for the most recent session.

## Lesson 12: Subagents

### Task 12.1: the agent tool + inner loop
- `harness/tools/agent.py`: `agent_tool(llm, tools, policy=None, asker=None,
  system=None, on_tool_call=None) -> Tool`; `execute(task)` runs `run_turn`
  on `[]`, returns final content; sub-registry excludes `agent`.
- Tests (FakeLLM): parent receives only the final answer string; the sub's
  intermediate tool calls never appear in the parent's messages; the sub
  cannot see or call `agent` (definitions list pinned); a sub that errors
  returns an error string to the parent loop; the shared-policy grant
  carries into the sub.
- Review gate, commit.

### Task 12.2: REPL wiring + live smoke
- `main.py`: add the agent tool to the registry with a delegation-oriented
  description; depth-marked `⚙` printing for sub tool calls.
- Live smoke: ask the parent to delegate a multi-file exploration; verify
  the parent's context stays small (estimate_tokens before/after vs. doing
  it inline) and the answer is correct.
- Review gate, quiz, commit, tag `lesson-12`.

## Lesson 13: Sessions

### Task 13.1: transcript record + fold
- `harness/session.py`: `SessionLog(path)` with `record_turn(new_messages)`
  and `record_compaction(...)` appending JSONL events; `load(path) ->
  list[dict]` folding the log into the current messages state.
- Tests: round-trip (record turns → load → equal messages); compaction
  event folds to the post-compaction state; a torn/partial final line is
  tolerated on load (crash mid-write); empty/missing file loads to `[]`.
- Review gate, commit.

### Task 13.2: REPL resume + live smoke
- `main.py`: sessions under `<workspace>/.agent/sessions/<timestamp>.jsonl`;
  record after each completed turn; `--resume <id>` / `--continue` rebuild
  messages and continue the conversation.
- Live smoke: hold a conversation, kill the process, `--continue`, ask a
  question that requires memory of the prior exchange.
- Review gate, quiz, commit, tag `lesson-13`.

## Stage 5 done when
- Default suite green offline; gated suite green live.
- A delegated task returns a small answer while the parent's token estimate
  stays flat; a killed session resumes with memory intact across the
  restart.
- Tags `lesson-12`, `lesson-13`; the loop still tool-name-free; transport
  still quarantined; the transcript log append-only with compaction as
  events.
