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

**Tech Stack:** unchanged. Python 3.14, `uv`, `pytest`, no new deps (token
counting is a cheap heuristic, not `tiktoken` — see decision 3).

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
3. **Token counting is a heuristic, not a real tokenizer.** `len(text) / 4`
   as an approximation, behind `estimate_tokens(messages) -> int`. Rationale:
   a real BPE tokenizer is a heavy dependency and provider-specific; the
   harness only needs "are we near the limit?", which a 4-chars-per-token
   estimate answers well enough. The function is swappable if we ever want
   precision. Named as a deliberate approximation in the lesson.
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
   assistant message).

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
  threshold, before the model turn. Threshold + keep_recent are parameters.
- Observability: an `on_compact` callback (like `on_tool_call`) so the REPL
  can print "[compacted N messages]".
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
