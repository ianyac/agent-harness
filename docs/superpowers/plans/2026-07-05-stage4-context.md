# Stage 4: Context Engineering — Implementation Plan (Lessons 10–11)

> Standing protocol: teacher writes code and tests and explains; learner
> reviews every diff before it commits; quiz on built material at each tag.
> Task-assigning messages end with a numbered TODO list.

**Goal:** Make the agent *good*, not just capable. Lesson 10 gives it a
designed system prompt — who it is, what it knows about its environment, how
to use its tools. Lesson 11 keeps the conversation from outgrowing the context
window by summarizing old turns when the token budget runs low.

**Architecture:** The system prompt is assembled from parts in
`harness/prompts.py` and passed to `complete()` as the `instructions` the
adapter already sends. Compaction lives in `harness/compaction.py` and is a
pure transform on the messages list, triggered by a token estimate; the loop
calls it before each model turn. Both are seams: prompt sections are data,
the token counter and the compaction summarizer are injectable.

**Tech Stack:** Python 3.14, `uv`, `pytest`, plus `tiktoken` for real token
counting (added per learner's call, 2026-07-05 — see decision 3).

## Decisions under review

1. **The system prompt becomes a real parameter of `run_turn`**, threaded to
   `complete(..., system=...)`. Today `CodexAdapter` hardcodes
   `instructions="You are a helpful assistant."`; that constructor arg stays
   as the *default*, but the loop can now override per turn. FakeLLM records
   the system prompt it was given (new turn-record field) so tests can assert
   what was sent.
2. **Prompt is assembled from ordered sections** (`harness/prompts.py`):
   identity, environment (cwd, workspace, OS, date), tool-use guidance. A
   `build_system_prompt(env, extra_sections=[])` function returns the string.
   Sections are data so lesson 15 (skills) can inject skill metadata later.
3. **Real token counting via `tiktoken`** (learner's call, 2026-07-05,
   overriding the original `len/4` heuristic). Our backend is
   OpenAI-compatible (codex/GPT-5.5), so `tiktoken`'s `o200k_base` encoding
   is the right tokenizer. `count_tokens(messages) -> int` encodes each
   message's text content and adds the small per-message structural overhead
   OpenAI's accounting uses (~3–4 tokens/message for role/formatting, per the
   documented `num_tokens_from_messages` approach), so the count tracks what
   the provider actually bills — not just raw text length. Still behind a
   seam (one function) so it stays swappable. Trade-off accepted: a real,
   compiled dependency, in exchange for accuracy that makes the compaction
   trigger trustworthy rather than approximate.
4. **Compaction summarizes the oldest turns, keeps the newest verbatim.**
   `compact(messages, llm, keep_recent=N)`: everything older than the last N
   messages gets replaced by a single summary message (the model summarizes
   its own history); recent messages and the system prompt survive untouched.
   Triggered when `estimate_tokens` exceeds a threshold.
5. **The summary is produced by the model itself** — a separate `complete()`
   call with a "summarize this conversation" instruction, using the same
   LLMClient. Tested with FakeLLM scripting the summary. Named tension: this
   spends tokens/latency to save context; worth it near the limit, wasteful
   early — hence threshold-triggered, not every turn.
6. **Compaction preserves the tool-call/result pairing invariant.** The cut
   point can't fall between an assistant `tool_calls` message and its `tool`
   results, or we recreate the dangling-call corruption from lessons 4/8. The
   summarizer snaps the boundary to a safe split point (after a plain
   assistant message) — backward first (keep at least keep_recent), falling
   forward when tool traffic leaves no boundary there (keep less rather
   than overflow); a lone leading summary is never re-summarized.
7. **Breadcrumbs are mechanical and point at a durable action log**
   (learner's call, 2026-07-06). The model's summary carries judgment
   sections only (goal, state, decisions, learnings/warnings, unfinished
   work); `compact()` appends an `[Auto-generated — not summarized]` block
   from an injected `breadcrumbs` string, so pointers never pass through
   the summarizer's judgment — and never degrade across repeated
   compactions. The REPL journals every *executed* tool call (the
   `on_tool_call` seam fires post-permission-gate, pre-execution) as JSONL
   to `.agent/actions.jsonl` inside the workspace, truncated at session
   start, and passes `Action log: <path> (<n> entries)` as the note.
   Recovery uses the existing tools (bash/read_file) — no dedicated grep
   tool. Aggregates like "files touched" were dropped: they need per-tool
   semantics the loop must not have, and they're derivable from the log.
8. **The default threshold is a fraction of the model's context window**
   (learner's call, 2026-07-06). `CodexAdapter` carries
   `context_window` (272k for gpt-5.5 — confirmed by probing the
   backend's `/codex/models` metadata, which itself models an
   `auto_compact_token_limit`); `main.py` defaults `--compact-threshold`
   to 80% of it. The remaining 20% is headroom for output tokens,
   mid-turn growth (the trigger checks once per turn), and estimate
   bias. `run_turn` still takes an absolute number — the percentage is
   policy, computed where the window knowledge lives (the adapter);
   the loop mechanism stays dumb. Hard-coded rather than fetched: the
   models route is undocumented and needs a spoofed client_version, so
   it's provenance for a constant, not a runtime dependency.

## Lesson 10: The system prompt

### Task 10.1: build_system_prompt + wire through the loop
- `harness/prompts.py`: `Environment` (cwd, workspace, os, date) +
  `build_system_prompt(env, extra_sections=None) -> str` assembling ordered
  sections.
- `LLMClient.complete` gains `system: str | None = None`; `CodexAdapter`
  sends it as `instructions` (falls back to its constructor default);
  FakeLLM records it.
- `run_turn` gains `system: str | None = None`, passed to every
  `complete()` in the turn.
- Tests: prompt contains each section's key facts; loop forwards system to
  the model every iteration (FakeLLM turn-record assertion); None falls back
  to adapter default; contract test still green.
- Review gate, commit.

### Task 10.2: REPL builds and uses the prompt + live smoke
- `main.py`: construct `Environment` from real cwd/workspace/os/date, build
  the prompt, pass to `run_turn`.
- Live smoke: ask "what directory are you in and what day is it?" — the model
  answers from the injected environment, not a guess.
- Review gate, quiz, commit, tag `lesson-10`.

## Lesson 11: Context is scarce

### Task 11.1: token estimate + compaction transform
- `harness/compaction.py`: `estimate_tokens(messages) -> int`;
  `compact(messages, llm, keep_recent, summary_instruction) -> list[dict]`
  returning a new list (system + summary + recent), snapping the cut to a
  safe boundary.
- Tests (all FakeLLM): summary replaces old turns; recent N preserved
  verbatim; system prompt survives; cut never splits a tool_calls/tool pair;
  a conversation with no old turns is returned unchanged; token estimate
  monotonic in length.
- Review gate, commit.

### Task 11.2: loop triggers compaction + live smoke
- `run_turn` (or a wrapper) calls `compact` when `estimate_tokens` exceeds a
  threshold, re-checked before every model call in the turn (tool results
  can balloon the context mid-turn). Threshold + keep_recent are parameters.
- Observability: an `on_compact` callback (like `on_tool_call`) so the REPL
  can print "[compacted N messages]".
- `main.py` journals executed tool calls to `.agent/actions.jsonl` via
  `on_tool_call` (decision 7) and passes the log pointer as `compact`'s
  `breadcrumbs`. Needs a `.gitignore` entry for `.agent/` — shared file,
  routed through yc.
- Live smoke: drive a long conversation past the threshold, confirm it keeps
  working and the model still remembers recent context after a compaction.
- Review gate, quiz, commit, tag `lesson-11`.

## Stage 4 done when
- Default suite green offline; gated suite green live.
- REPL agent answers environment questions from its injected prompt; a
  conversation driven past the token threshold compacts and continues
  coherently.
- Tags `lesson-10`, `lesson-11`; loop still tool-name-free; transport still
  quarantined in `harness/llm.py`; compaction never corrupts the transcript.
