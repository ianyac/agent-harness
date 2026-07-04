# TypeScript Speed-Run — Re-express Lessons 1–3 (Plan)

> **Status: PAUSED (2026-07-04).** The curriculum continues in Python first;
> this speed-run resumes after lesson 15 and then covers all lessons, not
> just 1–3. Work parked on branch `ts-speedrun`: TS-1 complete (tag
> `ts-lesson-01`), TS-2 mid-flight (commit `29c959f`).

> Teacher writes, learner reviews each diff, quiz at each tag — the standing
> protocol. This plan re-expresses the already-designed and already-reviewed
> Python lessons 1–3 in TypeScript on Bun. No new design decisions: where the
> Python line settled something, the TS line inherits it. Reference
> implementation: `python/` (tag `python-final`).

**Done when:** `bun test` green offline; `RUN_CODEX_TESTS=1 bun test` green
against live codex; REPL (`bun run main.ts`) holds a real conversation and a
live tool round-trip works; tags `ts-lesson-01`..`ts-lesson-03` exist. Then
lesson 4 (the agent loop) proceeds in TS per the Stage 2 plan.

## TS-1: FakeLLM + runTurn + REPL  → tag `ts-lesson-01`

- `tests/fakeLlm.ts`: `FakeLlm` with the unified turn-record design (script
  entries `{type: "text"| "tool_calls", ...}`, records
  `{output, messages, tools}`, deep-copied snapshots, strict on unknown
  types). TS types for script entries and turn records.
- `src/messages.ts`? No — same call as Python: plain objects, no wrapper
  types beyond TS *interfaces* (`Message`, `ToolCall`), which cost nothing
  at runtime. Interfaces live in `src/llm.ts`.
- `main.ts`: `runTurn(messages, userInput, llm)` + readline REPL, EOF-clean.
- Tests port 1:1 from `python/tests/test_lesson01.py` + fake tests.

## TS-2: LLMClient + CodexAdapter + contract  → tag `ts-lesson-02`

- `src/llm.ts`: `LLMClient` interface; `normalize`, `toWireTools`,
  `toWireInput` pure functions; `CodexAdapter` using `fetch` streaming
  (Bun-native SSE consumption), auth from `~/.codex/auth.json`.
- Contract test parametrized over implementations, codex gated by
  `RUN_CODEX_TESTS=1`. Fixtures reused verbatim from
  `python/tests/fixtures/` (they're provider ground truth, not
  language-specific — copied to `tests/fixtures/`).

## TS-3: Tool + registry + FakeLlm tool scripting  → tag `ts-lesson-03`

- `src/tools.ts`: `Tool` interface + construction-time schema validation
  (`defineTool()` factory — TS's dataclass-`__post_init__` equivalent),
  `definitions()`.
- FakeLlm tool-call scripting (already in TS-1's format), wire translation
  tests ported, live round-trip smoke re-verified.
- Lesson 3 quiz (deferred from the Python line) happens at this tag.
