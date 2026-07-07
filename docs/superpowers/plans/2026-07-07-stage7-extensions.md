# Stage 7: Extensions — Implementation Plan (Lessons 16–17)

> Standing protocol: teacher writes code and tests and explains; learner
> reviews every diff before it commits; quiz on built material at each tag.
> Task-assigning messages end with a numbered TODO list.

**Goal:** Cash the two checks written on day one. The spec's invariants
section is titled "keep streaming/MCP addable": invariant 1 (provider
details never escape `llm.py`) exists so streaming can land; invariant 2
(the loop never knows a tool's name) exists so MCP tools can land. Stage 7
is the proof that the founding bets were right — both features arrive with
zero change to the loop's control flow.

**Architecture:** Streaming is an observer callback (`on_text_delta`) threaded
through `run_turn` to the adapter exactly like `system` — the adapter
already consumes an SSE stream internally (the codex endpoint has been
stream-only since lesson 2; we assemble and discard the liveness), so
lesson 16 surfaces deltas we already receive. MCP lands in
`harness/mcp.py`: a client for the stdio transport (newline-delimited
JSON-RPC 2.0) whose discovered tools become ordinary registry entries —
permissions, hooks, truncation, and subagent inheritance all compose
untouched.

## Decisions under review

1. **Streaming is observability, not control flow.** `complete()` and
   `run_turn` gain `on_text_delta: Callable[[str], None] | None = None`
   (the name the ui stream's seam request proposed and probes for),
   forwarded like `system`. The adapter invokes it per text delta and
   still returns the same assembled message dict — chunks are UX,
   messages remain the unit of truth. The loop's control flow is
   untouched (one pass-through parameter, stated honestly).
2. **Text deltas only.** Tool-call argument streaming is real in
   production harnesses but adds parser state for no lesson value; we
   stream what the user watches — prose.
3. **Only the parent's turns stream.** The subagent's `run_turn` and the
   compaction summarizer call `complete()` without `on_text_delta` — their
   output is internal, and interleaved sub-streams would be noise.
   FakeLLM emits scripted text through `on_text_delta` in fixed chunks so the
   offline suite can pin forwarding and assembly.
4. **MCP scope: stdio transport, tools only.** Newline-delimited JSON-RPC
   over a child process's stdin/stdout; `initialize` handshake,
   `tools/list`, `tools/call`. No HTTP/SSE transport, no resources or
   prompts — the lesson is "a foreign tool ecosystem behind the registry
   seam", not full protocol coverage.
5. **MCP servers are config-as-code and get the hooks treatment.**
   `mcp.json` at the workspace root ({"servers": {name: {command}}}),
   model-writable and clone-shippable, so server commands run only after
   the same startup approval gate as hooks: listed, explicit "y",
   non-tty = disabled. Servers run unsandboxed (they are long-lived
   subprocesses with their own needs); the gate is the mitigation.
6. **MCP tools join the registry namespaced `<server>__<tool>`** to
   avoid colliding with builtins or each other. `read_only` honors the
   server's declared `readOnlyHint` annotation when present, else
   defaults to False — a foreign tool with unknown side effects should
   face the permission gate, not bypass it. Results pass through
   `truncate` like every native tool. Downstream, everything composes
   for free: the permission gate asks, hooks wrap, subagents inherit.
7. **Offline testing uses a scripted fake MCP server** — a small Python
   fixture speaking the real wire protocol over stdio, spawned by the
   tests. The protocol handling is fully exercised without network or
   third-party servers; a live smoke drives the same client against the
   fixture through the whole REPL stack.

## Lesson 16: Streaming output

### Task 16.1: the on_text_delta seam + tests
- `LLMClient.complete` and `CodexAdapter.complete` gain `on_text_delta`;
  the adapter fires it per SSE text delta while assembling the reply
  as today. `run_turn` forwards it (pass-through only).
- FakeLLM: scripted text replies are emitted through `on_text_delta` in
  chunks, then returned assembled.
- Tests: chunks join to exactly the returned content; on_text_delta is
  forwarded on every loop iteration; None = no calls (default
  unchanged); the summarizer and subagent paths never receive chunks.
- Review gate, commit.

### Task 16.2: REPL live output + smoke
- `main.py`: print "agent: " then chunks as they arrive (flush), newline
  at turn end; suppress the duplicate full-reply print when streaming
  happened.
- Live smoke: drive a long generation; assert streamed text == final
  message content and that the reply arrives as many delta events.
  Measured honestly (2026-07-08): time-to-first-chunk is dominated by
  the model's silent reasoning phase (~83% of a 20s turn before the
  first delta; the answer then paints in ~3.4s across 184 events) —
  reasoning emits no output deltas and summaries default off at the
  backend. Streaming buys incremental paint of the answer, not an end
  to the thinking silence; surfacing reasoning summaries is a possible
  future extension, out of scope here.
- Review gate, quiz, commit, tag `lesson-16`.

## Lesson 17: MCP client

### Task 17.1: harness/mcp.py + fake server + tests
- `MCPServer(name, command)`: spawn over stdio, `initialize` handshake,
  `tools/list`, `call(tool, args)`, `close()`; newline-delimited
  JSON-RPC framing; call timeout; server death or malformed replies
  become error strings (lesson 8 discipline), never exceptions.
- `mcp_tools(server) -> list[Tool]`: one registry Tool per discovered
  tool — namespaced name, server-provided description and inputSchema,
  execute forwarding to `call`, `read_only` from `readOnlyHint`,
  results truncated.
- `tests/fake_mcp_server.py` fixture + tests: handshake, discovery,
  a call round-trip, readOnlyHint mapping, error-on-crash, timeout,
  unknown tool.
- Review gate, commit.

### Task 17.2: REPL wiring + smoke + stage close
- `main.py`: load `mcp.json` (missing = none; malformed = hard error),
  approval gate (shared pattern with hooks), spawn servers, merge
  namespaced tools into the registry before the agent tool joins,
  terminate servers on exit.
- Live smoke: the fake server through the full stack — its tool listed,
  permission-gated, hook-wrappable, callable by the agent, and inherited
  by a subagent.
- Review gate, quiz, commit, tag `lesson-17`. Stage 7 done — the
  curriculum including extensions is complete; TS speedrun unparks.

## Stage 7 done when
- Default suite green offline; gated suite green live.
- The REPL visibly streams a long reply (first chunk ≪ total time); an
  MCP server's tool works through permission gate, hooks, and subagent
  inheritance with the loop untouched.
- Tags `lesson-16`, `lesson-17`; `run_turn` control flow unchanged since
  lesson 13 (only pass-through parameters added); transport still
  quarantined in `llm.py`; the loop still tool-name-free.
