# Agent Harness — Learning Project Design

**Date:** 2026-07-03
**Status:** Approved pending user review

## Goal

Learn how agent harnesses work by building a "mini Claude Code" from the ground
up, one concept per lesson. The finished harness: an interactive CLI agent with
tool use (filesystem + bash), a permission system, sandboxed execution, a
designed system prompt, context compaction, subagents, persistent sessions,
hooks, and skills.

Roles (renegotiated 2026-07-03, mid-lesson 2): the teacher (Claude) explains
concepts, writes the code and tests, and explains every change. The learner
(yc) reviews every diff before it commits, asks questions freely, and must
pass a concept quiz at each lesson tag. (Lessons 1 and most of 2 ran the
original inverted protocol: learner wrote, teacher reviewed.)

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python 3.14 for the full curriculum; TypeScript re-expression afterward (parked on branch `ts-speedrun`, tag `ts-lesson-01`) | Learner's call (2026-07-04, revised same day): concepts first in one language end to end; the TS port becomes a consolidation exercise once the harness is complete |
| Env/deps | `uv` (`bun` on the parked TS branch) | Simple, fast, lockfile |
| LLM backend | Codex subscription, behind a learner-written adapter | Forces a real provider-abstraction boundary |
| Message format | OpenAI chat-completions dicts (`{"role", "content"}`), no wrapper type | Learner's call: de facto ecosystem standard, zero translation for the backend. Accepted knowingly: OpenAI tool-call warts (JSON-string `arguments`) live in our code |
| Frameworks | None that own the control flow | The harness *is* the framework; the loop must be ours. Libraries that stay out of the loop (HTTP client, pytest, pydantic) are fine |
| Structure | One evolving codebase; git tag per lesson (`lesson-NN`) | Diffs between tags show what each concept costs; mirrors honest evolution |
| Scaffolding | None up front — lesson 1 is a single file | Modules get extracted only when a lesson forces the boundary |

## Curriculum

One concept per lesson. Every lesson ends with a runnable harness and passing
tests. Lessons may be split or merged as actual difficulty emerges.

### Stage 1 — The Loop
1. **The conversation is the state.** Messages list, roles, a REPL against a
   hardcoded fake model. Core insight: a harness is
   `while true: messages → model → message`. The fake model stays forever as
   the test double.
2. **The provider adapter.** OpenAI message dicts as the internal format;
   `LLMClient` interface + contract test; learner writes the Codex adapter
   behind it (transport, auth, response normalization).

### Stage 2 — Tools
3. **Tools are structured text.** Tool definitions (name, description, JSON
   schema), tool registry, parsing tool calls from responses. No execution yet.
4. **The agent loop.** Execute tool calls, append results, call model again,
   repeat until a plain text reply. The ~20-line heart of every harness.
5. **Real tools: the filesystem.** `read_file`, `write_file`, `list_dir`.
   Tool-design craft: descriptions, string results.
6. **The universal tool: bash.** Shell execution, cwd, timeouts, output
   truncation.

### Stage 3 — Safety
7. **Permissions.** Gate between "model wants to" and "harness does":
   allowlists, ask-the-human, permission modes.
8. **Failure is information.** Tool errors returned to the model as results,
   not crashes. Turn limits, API retries, cancellation.
9. **Sandboxing.** Permissions gate *intent*; the sandbox contains *execution*.
   OS-level sandbox for bash and file tools (macOS `sandbox-exec` profiles):
   writes confined to the workspace, network gating. Defense in depth.

### Stage 4 — Context engineering
10. **The system prompt.** Environment info, behavioral instructions, tool
    guidance; write ours.
11. **Context is scarce.** Token counting, budget tracking, compaction
    (summarize when near the limit).

### Stage 5 — Scale-out
12. **Subagents.** An `agent` tool spawning a fresh inner loop with empty
    context, returning only its final answer.
13. **Sessions.** JSONL transcripts, resume.

### Stage 6 — Extensibility
14. **Hooks.** Lifecycle extension points (pre/post tool use, session start,
    stop): user-configured commands that can observe, block, or inject
    context. A generalization of the L7 permission gate.
15. **Skills.** Progressive disclosure of instructions: skill files with
    frontmatter, metadata listed in the system prompt, full content loaded
    into context on demand. Builds directly on L10–11.

### Stage 7 — Extensions (optional, decided later)
16. **Streaming output.** Streaming variant of `LLMClient` yielding chunks that
    assemble into the same message type; loop unchanged.
17. **MCP client.** MCP tools as registry entries whose `execute` forwards
    JSON-RPC over stdio; loop, permissions, truncation unchanged.

## Architecture

Target shape (converged at lesson 15; extracted incrementally, never
scaffolded):

```
agent-harness/
├── harness/
│   ├── llm.py           # L2: LLMClient interface + Codex adapter
│   ├── tools/           # one file per tool (learner's ruling, 2026-07-04)
│   │   ├── base.py      # L3: Tool + definitions; __init__.py stays empty
│   │   ├── read_file.py # L5   (bash.py L6, agent.py L12 join later)
│   │   ├── write_file.py
│   │   └── list_dir.py
│   ├── loop.py          # L4: the agent loop
│   ├── permissions.py   # L7
│   ├── sandbox.py       # L9: sandbox profiles wrapping bash/fs execution
│   ├── prompts.py       # L10: system prompt assembly
│   ├── compaction.py    # L11
│   ├── session.py       # L13: JSONL transcripts
│   ├── hooks.py         # L14: lifecycle events + hook config
│   └── skills.py        # L15: skill discovery and on-demand loading
├── tests/               # fake model test double + per-module tests
└── main.py              # the REPL
```
(TypeScript mirror of this tree lives on the `ts-speedrun` branch, resumed
after L15.)

**Data flow (from L4):** REPL appends user input to `messages` → loop sends
messages + tool definitions through `LLMClient` → adapter translates to/from
the provider API → tool calls in the reply are looked up in the registry and
executed (through the permission gate from L7) → results appended as messages →
model called again → repeat until plain text → display, wait for input.

**Dependency direction:** `main → loop → (llm, tools, permissions)`. Nothing
depends on `main`. Messages are plain OpenAI-format dicts; there is no shared
message module.

**Invariants (keep streaming/MCP addable):**
1. The loop and tools only ever see plain OpenAI-format message dicts.
   Provider SDK objects, HTTP transport, and auth never escape `llm.py`.
2. The loop never knows a tool's name — tools are always looked up in the
   registry.

## Lesson workflow

Each lesson (roles as renegotiated 2026-07-03):
1. **Concept briefing** — teacher explains the problem, the mechanism, and how
   Claude Code does it. Short.
2. **Tests as the spec** — teacher writes the failing pytest tests first and
   explains what they pin. Red first.
3. **Teacher implements** until green, explaining the design as it goes.
4. **Learner reviews** the diff before commit — findings are resolved or
   argued, never skipped. Teacher quizzes the learner on the lesson's
   concepts; passing the quiz is part of the gate.
5. **Verify & tag** — run the REPL as a smoke test, commit, tag `lesson-NN`.
6. Task-assigning messages to the learner end with an explicit numbered
   `TODO:` list.

## Testing strategy

- The fake `LLMClient` (from L1) returns scripted responses, enabling
  deterministic tests of the loop, tool dispatch, stop conditions, permissions,
  truncation, and compaction — zero API calls.
- Tests in `tests/`, run with `uv run pytest` (the parked TS branch uses
  `bun test`).
- The real Codex adapter is excluded from the automated suite (slow, costly,
  nondeterministic); REPL smoke tests cover it.
- Known limit, taught explicitly: unit tests can't cover model *choices*;
  end-to-end behavior is verified by using the harness.

## Out of scope

- Multi-provider support beyond the one Codex adapter (the interface allows it;
  we don't build it).
- GUI/TUI beyond a plain REPL.
- A plugin system (bundling hooks/skills/tools for distribution) — hooks and
  skills themselves are in scope (L14–15); packaging them is not.
