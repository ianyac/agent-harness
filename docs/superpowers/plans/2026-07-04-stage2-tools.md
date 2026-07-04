# Stage 2: Tools — Implementation Plan (Lessons 3–6)

> **Language pivot (2026-07-04):** project moved to TypeScript on Bun after
> Task 3.3; Python implementation archived under `python/` (tag
> `python-final`). Lessons 1–3 are being re-expressed per
> `2026-07-04-ts-speedrun.md`; lessons 4–6 below then proceed in TS (file
> names map `harness/*.py` → `src/*.ts`, `uv run pytest` → `bun test`).

> **Protocol (since the 2026-07-03 role switch):** the teacher writes all code
> and tests, explaining as it goes; the learner reviews every diff before it
> commits, and takes a concept quiz at each lesson tag. Steps below are the
> teacher's to execute; **Review gate** steps are the learner's.

**Goal:** Turn the chat harness into an agent: the model can request tool
executions — filesystem reads/writes and shell commands — and the loop honors
them until the model answers in plain text. Lessons 3–6 of the spec.

**Architecture:** Tools are described to the model as JSON schemas and looked
up by name in a plain dict registry (spec invariant 2: the loop never
hardcodes a tool name). Internal message format stays OpenAI
chat-completions dicts, now including `tool_calls` on assistant messages and
`role: "tool"` results. The adapter alone translates to/from the Responses
API wire shapes (`function_call` / `function_call_output` items). FakeLLM
learns to script tool calls, which is what makes the loop testable offline.

**Tech Stack:** Python 3.14+ (pinned 2026-07-04), `uv`, `pytest`, `httpx` in
the adapter only.

## Decisions under review (the five judgment calls in this plan)

1. **Internal message shapes** (next section): OpenAI `tool_calls` format
   adopted warts-and-all, per the Stage 1 format decision — including
   `arguments` as a JSON string. Least reversible choice in the stage.
2. **The socket changes once** (next section): `LLMClient.complete` gains
   `tools: list[dict] | None = None`. Backward-compatible, but it is a
   Protocol change.
3. **Scoping calls** (Tasks 4.1, 5.1): tool-execution errors crash until
   lesson 8; filesystem tools get no confinement until lessons 7/9; the loop
   caps at a crude `max_iterations=20` as a lesson-8 placeholder. Same
   "scheduled, not missing" pattern as Stage 1's retries.
4. **Truncation lands early** (Task 6.1): bash forces the context-budget
   question in lesson 6 rather than waiting for Stage 4.
5. **Registry is a plain `dict[str, Tool]`** (Task 3.1): no registry class
   until something needs one.

## Global Constraints

- All Stage 1 constraints hold (no control-flow frameworks; plain dicts only
  outside `harness/llm.py`; no network in the default test suite;
  `RUN_CODEX_TESTS=1` gates real-API tests; every task-assigning message ends
  with a `TODO:` list).
- Tool `execute` functions take keyword args (parsed from the call's JSON
  `arguments`) and return a **string** — results are text for the model,
  always.
- Tool execution failures raise no further than the loop: lesson 8 turns them
  into results; until then the loop may crash (documented limitation).
- Every lesson ends: learner review gate → quiz → commit → tag `lesson-NN`.

## Internal message shapes (fixed here, used everywhere)

```python
# assistant message requesting tool calls (OpenAI chat format, accepted warts included)
{"role": "assistant", "content": None, "tool_calls": [
    {"id": "call_0", "type": "function",
     "function": {"name": "read_file", "arguments": "{\"path\": \"x.txt\"}"}}]}

# tool result message
{"role": "tool", "tool_call_id": "call_0", "content": "<string result>"}

# tool definition handed to LLMClient.complete
{"type": "function", "function": {
    "name": ..., "description": ..., "parameters": {<json schema>}}}
```

`LLMClient.complete` grows a parameter — the socket's one and only change this
stage: `complete(self, messages: list[dict], tools: list[dict] | None = None)
-> dict`.

---

## Lesson 3: Tools are structured text

**Concept:** a tool is a *description* (name, purpose, JSON schema of
arguments) sent with every request; a tool call is *structured text* the model
emits back; nothing executes anywhere in this lesson. Vocabulary only:
`Tool` + registry, FakeLLM scripting of tool calls, adapter translation both
directions, and one real captured `function_call` fixture.

### Task 3.1: Tool + registry (`harness/tools.py`)

- `Tool` dataclass: `name: str`, `description: str`, `parameters: dict`,
  `execute: Callable[..., str]`; method `definition() -> dict` returning the
  OpenAI-format dict above. Registry is a plain `dict[str, Tool]`; module
  function `definitions(tools: dict[str, Tool]) -> list[dict]`.
- Tests (`tests/test_tools.py`): definition shape; definitions() order/length;
  a sample tool's `execute(**json.loads(args))` round-trip.
- Review gate, commit.

### Task 3.2: FakeLLM scripts tool calls

- Script entries may now be: `str` (plain reply, as today) **or**
  `("tool_name", {args})` tuple → assistant message with `tool_calls`,
  deterministic ids `call_0`, `call_1`, …
- `complete` accepts the new optional `tools=` parameter (recorded in a new
  `tool_offers` spy list; existing `calls` spy unchanged).
- Tests (`tests/test_fake_llm_tools.py`): tuple entry yields correct
  tool_calls shape with parsed-back arguments; ids increment; tools= is
  recorded; str entries still work (regression).
- Review gate, commit.

### Task 3.3: Adapter speaks tool wire-format

- Request direction: tool definitions → Responses API flat format
  (`{"type": "function", "name", "description", "parameters"}`); assistant
  `tool_calls` history → `function_call` items; `role: "tool"` messages →
  `function_call_output` items (the lesson-2 guard admits `tool` now).
- Response direction: `function_call` output items → internal `tool_calls`
  message; `normalize` extended, message items still win when present.
- One-off capture (teacher): real response where codex calls a tool →
  `tests/fixtures/codex_tool_call.json`.
- Tests (`tests/test_codex_adapter.py` additions): both translation
  directions as pure-function tests; fixture-pinned parse.
- Contract test unchanged and still green both modes (socket change is
  backward-compatible: `tools` defaults to `None`).
- Review gate, quiz, commit, tag `lesson-03`.

## Lesson 4: The agent loop

**Concept:** the ~20-line heart: send messages + tool definitions; if the
reply requests tool calls, execute each via registry lookup, append results,
call again; a plain-text reply ends the turn. The conversation grows
`assistant(tool_calls) → tool → assistant` sandwiches that lesson 1's REPL
never sees.

### Task 4.1: `harness/loop.py`

- Move `run_turn` out of `main.py`; new signature:
  `run_turn(messages, user_input, llm, tools: dict[str, Tool] | None = None,
  max_iterations: int = 20) -> dict`. Hitting the cap raises `RuntimeError`
  (placeholder — lesson 8 owns real policy).
- Tests (`tests/test_loop.py`, all offline via FakeLLM scripts): single tool
  call then answer (verify the full message sandwich, in order); two
  sequential calls; multiple tool_calls in one assistant message; zero tools
  → behaves exactly like Stage 1 (regression); cap raises.
- Review gate, commit.

### Task 4.2: REPL shows tool activity

- `main.py`: builds the registry (one toy tool for now — `add(a, b)`),
  passes it to `run_turn`, prints one line per executed call:
  `⚙ add({"a": 2, "b": 3})`. Where that print lives without fattening the
  humble shell is a design point to discuss at review.
- Smoke: real model, ask "use the add tool to sum 2 and 3" — watch the wire
  work end to end.
- Review gate, quiz, commit, tag `lesson-04`.

## Lesson 5: Real tools — the filesystem

**Concept:** tool-design craft. Descriptions are prompts; schemas are
guardrails; results are strings sized for a context window, not for a
program.

### Task 5.1: `read_file`, `write_file`, `list_dir` (`harness/tools_fs.py`)

- All three operate relative to the process cwd, results as plain strings
  (`read_file` → content; `write_file` → confirmation with byte count;
  `list_dir` → one name per line, dirs marked with `/`).
- No confinement yet — **deliberate**: the unease this causes is lesson 7's
  (permissions) and lesson 9's (sandbox) motivation, and gets said out loud.
- Tests against `tmp_path`: happy paths ×3; missing-file behavior raises for
  now (lesson 8 will convert to error-results; documented).
- Review gate, commit.

### Task 5.2: Wire into the REPL + description craft

- Registry in `main.py` gains the three tools; smoke test: "create a file
  haiku.txt containing a haiku about lists, then read it back".
- Descriptions written to the standard of: model sees *only* this text —
  when to use, when not to, what comes back.
- Review gate, quiz, commit, tag `lesson-05`.

## Lesson 6: The universal tool — bash

**Concept:** one tool that subsumes all others, and the two prices it
charges: safety (deferred to lessons 7/9, named here) and unbounded output
(handled *now*: truncation is a context-budget decision, the first of Stage
4's themes arriving early).

### Task 6.1: `bash` tool (`harness/tools_bash.py`)

- `subprocess.run(command, shell=True, capture_output=True, timeout=30,
  cwd=<workspace>)`; result string = exit code line + merged
  stdout/stderr; timeout → result string, not exception.
- Truncation: over ~8000 chars → keep head + tail with
  `[... N chars truncated ...]` marker between.
- Tests: echo round-trip; non-zero exit reported in-band; timeout string;
  truncation shape (head kept, tail kept, marker present, length bounded).
- Review gate, commit.

### Task 6.2: Full-agent smoke + stage close

- All four tools registered in the REPL; smoke: "how many .py files are in
  this project, and which is longest?" — real model, real pipeline.
- `read_file` retrofitted with the same truncation helper (shared in
  `harness/truncate.py` if extraction is warranted — reviewer's call).
- Review gate, quiz, commit, tag `lesson-06`.

---

## Stage 2 done when

- Default suite green and offline; gated suite green against live codex.
- REPL agent completes a multi-tool task end to end (create/read/list/bash).
- Tags `lesson-03` … `lesson-06` exist; transport still only in
  `harness/llm.py`; the loop still contains no tool names.
