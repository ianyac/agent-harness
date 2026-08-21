# Context Folding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add recoverable, span-addressed context folding to the Python harness while preserving the immutable session transcript and provider tool-call/result invariants.

**Architecture:** `FoldingContext` stores span metadata and fold state in SQLite beside the existing append-only session transcript, then produces a deterministic projected message array before each model call. Agent-facing `fold`/`unfold` tools and deterministic Tier 1 rules change visibility state; `run_turn` owns the projection seam and rejects simultaneous compaction. The CLI enables folding explicitly so the existing compacting mode remains backward compatible.

**Tech Stack:** Python 3.14, standard-library `sqlite3`, `hashlib`, and `json`; existing `tiktoken`, `pytest`, `Tool`, `SessionLog`, and `run_turn` seams.

## Global Constraints

- The supplied `docs/features/context-folding-design.md` is the normative specification; do not create a competing design document.
- Storage remains append-only for ordinary folds: content is retained and only projected visibility changes.
- User and system messages are hard-protected; assistant text (`mN`) and reasoning (`mN.t0`) are foldable spans, and `poisoned` on `mN` removes the whole turn (revised 2026-08-22, PR #21).
- Every fold has a taxonomy reason and non-empty written note; agent notes are 20–1500 characters and generic or instruction-shaped notes are rejected.
- `sensitive` is scanner-only, purges content, and rebuilds immediately.
- Tool-result blocks are content-replaced, never orphaned; every projection passes a parity linter before send.
- Folding and compaction must never manage the same live context.
- Default thresholds are 2,000 tokens per ingestion chunk, 500 tokens per foldable span, and 15% marked-token share per checkpoint rebuild.
- Fold/unfold tools remain local to their session and are not inherited by subagents.
- Existing behavior is unchanged unless a caller supplies a `FoldingContext` or the CLI receives `--fold-context`.

---

### Task 1: Persistent ledger, stable span ingestion, and deterministic projection

**Files:**
- Create: `harness/folding.py`
- Modify: `harness/compaction.py`
- Test: `tests/test_folding.py`

**Interfaces:**
- Consumes: internal messages shaped as `{role, content, tool_calls?, tool_call_id?}` and existing token encoding.
- Produces: `FoldConfig`, `FoldError`, `ProjectionError`, and `FoldingContext(path, session_id, decision_log_path=None, config=FoldConfig())`; methods `sync(messages, tools=None)`, `project(messages, turn=None)`, `checkpoint(turn=None, reason="explicit")`, `reconstruct(turn=None)`, and `projection_hash(messages, turn=None)`.

- [ ] **Step 1: Write failing tests for schema creation, stable IDs, labels, chunking, replay, and parity**

```python
def test_sync_assigns_stable_result_span_and_projection_labels_it(tmp_path):
    context = FoldingContext(tmp_path / "folds.sqlite3", "s", config=FoldConfig(min_span_tokens=0))
    messages = tool_exchange("read_file", {"path": "a.py"}, "print('a')")
    context.sync(messages, {"read_file": reader_tool()})
    projected = context.project(messages)
    assert projected[2]["content"].startswith("[m2.r0 ·")
    context.sync(messages, {"read_file": reader_tool()})
    assert context.span_ids() == ["m0", "m1", "m2.r0"]

def test_large_result_is_chunked_at_ingestion_with_stable_child_ids(tmp_path):
    context = folding_context(tmp_path, chunk_tokens=5)
    messages = tool_exchange("dump", {}, "one\ntwo\nthree\nfour\nfive\nsix")
    context.sync(messages, {"dump": noop_tool(name="dump")})
    assert context.child_ids("m2.r0") == ["m2.r0.c0", "m2.r0.c1"]

def test_projection_rejects_orphaned_tool_result(tmp_path):
    context = folding_context(tmp_path)
    with pytest.raises(ProjectionError, match="orphan"):
        context.project([{"role": "tool", "tool_call_id": "missing", "content": "x"}])

def test_resume_replays_the_same_projection_hash(tmp_path):
    path = tmp_path / "folds.sqlite3"
    messages = tool_exchange("read_file", {"path": "a.py"}, "body")
    first = FoldingContext(path, "s", config=FoldConfig(min_span_tokens=0))
    first.sync(messages)
    expected = first.projection_hash(messages)
    resumed = FoldingContext(path, "s", config=FoldConfig(min_span_tokens=0))
    resumed.sync(messages)
    assert resumed.projection_hash(messages) == expected
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_folding.py -k "stable_result or chunked or orphaned or replay" -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'harness.folding'`.

- [ ] **Step 3: Expose public text counting and implement the minimal persistent projection core**

```python
@dataclass(frozen=True)
class FoldConfig:
    min_span_tokens: int = 500
    chunk_tokens: int = 2_000
    checkpoint_ratio: float = 0.15

class FoldingContext:
    def sync(self, messages: list[dict], tools: dict[str, Tool] | None = None) -> None:
        """Append unseen messages/spans without renumbering prior ledger entries."""

    def project(self, messages: list[dict], turn: int | None = None) -> list[dict]:
        """Return a deep-copied, labeled, parity-checked projection."""

    def reconstruct(self, turn: int | None = None) -> list[dict]:
        """Rebuild a projection from persisted ledger records and fold history."""
```

Create SQLite tables for `entries`, `folds`, `span_state`, message records, calls, projections, pins, and notices. Store canonical JSON and SHA-256 hashes; split large result content once at ingestion; keep an in-memory active message-id map so crash-tail rows never cause ID reuse.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/test_folding.py -k "stable_result or chunked or orphaned or replay" -v`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the projection foundation**

```bash
git add harness/folding.py harness/compaction.py tests/test_folding.py
git commit -m "lesson 25: add the folding ledger and projection"
```

### Task 2: Fold state machine, recovery, scanner, and agent tools

**Files:**
- Modify: `harness/folding.py`
- Create: `harness/tools/folding.py`
- Test: `tests/test_folding.py`
- Test: `tests/test_folding_tools.py`

**Interfaces:**
- Consumes: persisted spans from Task 1.
- Produces: `FoldingContext.fold(span_id, reason, note, decider="agent") -> str`, `unfold(span_id, decider="agent") -> str`, `pin(span_id)`, `delete(span_id)`, plus `fold_tool(context)` and `unfold_tool(context)` with the exact design enums and schemas.

- [ ] **Step 1: Write failing state-machine and rendering tests**

```python
def test_fold_is_pending_until_checkpoint_then_renders_verdict(tmp_path):
    context, messages = context_with_result(tmp_path, "evidence " * 100)
    context.fold("m2.r0", "finished", "Auth checks are clean; refresh.py remains the only untested path.")
    assert "evidence" in context.project(messages)[2]["content"]
    context.checkpoint(reason="phase boundary")
    assert "finished" in context.project(messages)[2]["content"]

def test_unfold_reinstates_full_content_at_tail_and_leaves_forward_pointer(tmp_path):
    context, messages = folded_context(tmp_path)
    context.unfold("m2.r0")
    projected = context.project(messages)
    assert "unfolded m2.r0 → tail" in projected[2]["content"]
    assert projected[-1]["content"].endswith("original evidence")

def test_double_fold_overlap_and_protected_span_are_structured_errors(tmp_path):
    context, messages = context_with_chunked_result(tmp_path)
    context.fold("m2.r0.c0", "finished", rich_note())
    with pytest.raises(FoldError, match="already folded"):
        context.fold("m2.r0.c0", "finished", rich_note())
    with pytest.raises(FoldError, match="overlap"):
        context.fold("m2.r0", "finished", rich_note())
    with pytest.raises(FoldError, match="protected"):
        context.fold("m0", "finished", rich_note())

def test_secret_scanner_purges_tool_output_and_rebuilds_immediately(tmp_path):
    context, messages = context_with_result(tmp_path, "token=sk-abcdefghijklmnopqrstuvwxyz123")
    assert "sk-" not in json.dumps(context.project(messages))
    assert context.state("m2.r0") == "purged"
    assert context.content("m2.r0") is None
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_folding.py tests/test_folding_tools.py -k "fold or unfold or secret or schema" -v`

Expected: failures report missing state-transition methods and folding tool factories.

- [ ] **Step 3: Implement transitions, markers, note gates, sensitive purge, and tools**

```python
AGENT_REASONS = (
    "duplicate", "superseded", "finished", "irrelevant",
    "handled_failure", "scaffolding", "poisoned",
)

def fold_tool(context: FoldingContext) -> Tool:
    return Tool(
        name="fold",
        description=FOLD_DESCRIPTION,
        parameters=FOLD_SCHEMA,
        execute=lambda span_id, reason, note: context.fold(span_id, reason, note),
        read_only=True,
        inheritable=False,
    )
```

Persist every accepted fold, close the open row on unfold, keep tail reinstatement deterministic, quarantine `poisoned`, purge scanner-only `sensitive`, reject unknown IDs with `difflib` suggestions, and log content-free decision events with note length/hash only.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `uv run pytest tests/test_folding.py tests/test_folding_tools.py -v`

Expected: all folding state and tool tests pass.

- [ ] **Step 5: Commit the policy surface**

```bash
git add harness/folding.py harness/tools/folding.py tests/test_folding.py tests/test_folding_tools.py
git commit -m "lesson 25: add fold and unfold policy"
```

### Task 3: Tier 1 deterministic rules, notices, and re-fetch telemetry

**Files:**
- Modify: `harness/folding.py`
- Modify: `harness/tools/base.py`
- Modify: `harness/tools/write_file.py`
- Modify: `harness/tools/agent.py`
- Test: `tests/test_folding.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_agent_tool.py`

**Interfaces:**
- Consumes: `Tool.foldable_inputs: tuple[str, ...]` and `Tool.inheritable: bool`.
- Produces: automatic `duplicate`, same-path read `superseded`, retry `handled_failure`, successful-write payload `superseded`, and consumed-agent-brief `scaffolding` folds; `turn_notice()` and decision events including `heuristic_fired`, `notice_emitted`, and `refetch_candidate`.

- [ ] **Step 1: Write failing heuristic and metadata-preservation tests**

```python
def test_identical_tool_results_fold_to_the_earliest_copy(tmp_path):
    context, messages = context_with_two_results(tmp_path, "same", "same")
    context.checkpoint()
    assert context.reason("m5.r0") == "duplicate"
    assert "dup of m2.r0" in context.project(messages)[5]["content"]

def test_later_read_of_same_path_supersedes_the_old_result(tmp_path):
    context, messages = context_with_two_reads(tmp_path, "old", "new", path="a.py")
    context.checkpoint()
    assert context.reason("m2.r0") == "superseded"
    assert "m5.r0" in context.note("m2.r0")

def test_successful_write_folds_only_registered_payload_field(tmp_path):
    tool = write_file_tool(workspace=tmp_path)
    assert tool.foldable_inputs == ("content",)

def test_hook_wrapping_preserves_folding_metadata():
    wrapped = _wrap(tool_with_fold_metadata(), HookSet(), print, 1, None)
    assert wrapped.foldable_inputs == ("content",)
    assert wrapped.inheritable is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_folding.py tests/test_tools.py tests/test_agent_tool.py -k "identical or supersedes or foldable_inputs or folding_metadata" -v`

Expected: failures report missing tool metadata and heuristic fold records.

- [ ] **Step 3: Add tool metadata and deterministic rules**

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    execute: Callable[..., str]
    read_only: bool = False
    spawns_subagents: bool = False
    inheritable: bool = True
    foldable_inputs: tuple[str, ...] = ()
```

Apply rules only after successful, addressable tool results and only above `min_span_tokens`. Canonicalize tool name/JSON arguments for retry and re-fetch matching. Queue local notices for the next turn and keep decision-log payloads free of content, paths, and arguments.

- [ ] **Step 4: Run the focused and neighboring tests and verify GREEN**

Run: `uv run pytest tests/test_folding.py tests/test_tools.py tests/test_agent_tool.py tests/test_hooks.py -v`

Expected: all selected files pass.

- [ ] **Step 5: Commit Tier 1 behavior**

```bash
git add harness/folding.py harness/tools/base.py harness/tools/write_file.py harness/tools/agent.py tests/test_folding.py tests/test_tools.py tests/test_agent_tool.py
git commit -m "lesson 25: automate safe context folds"
```

### Task 4: Project at the model-call seam and enforce compaction exclusivity

**Files:**
- Modify: `harness/loop.py`
- Modify: `harness/tools/agent.py`
- Create: `tests/test_folding_loop.py`
- Modify: `tests/test_compact_trigger.py`

**Interfaces:**
- Consumes: optional `context: FoldingContext | None` in `run_turn`.
- Produces: projected-only LLM input while `messages` stays the full transcript; turn-boundary and threshold checkpoints; `ValueError` when folding and compaction are both configured.

- [ ] **Step 1: Write failing loop-seam tests**

```python
def test_loop_sends_projection_but_retains_full_shadow_history(tmp_path):
    context, history = folded_history(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "done"}])
    run_turn(history, "next", llm, context=context)
    assert "[folded m2.r0" in llm.turns[0]["messages"][2]["content"]
    assert history[2]["content"] == "full evidence"

def test_loop_checkpoints_pending_folds_at_next_turn_boundary(tmp_path):
    context, history = pending_fold_history(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "done"}])
    run_turn(history, "next", llm, context=context)
    assert "[folded" in llm.turns[0]["messages"][2]["content"]

def test_folding_and_compaction_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_turn([], "x", FakeLLM([]), context=folding_context(tmp_path), compact_threshold=10)
```

- [ ] **Step 2: Run the loop tests and verify RED**

Run: `uv run pytest tests/test_folding_loop.py tests/test_compact_trigger.py -v`

Expected: `run_turn()` rejects the unknown `context` keyword.

- [ ] **Step 3: Implement projection-only dispatch and checkpoint timing**

```python
if context is not None and compact_threshold is not None:
    raise ValueError("context folding and compaction are mutually exclusive")

context.begin_turn(messages) if context is not None else None
messages.append({"role": "user", "content": user_input})

# Before every complete(): sync the immutable shadow, checkpoint when marked
# share reaches config.checkpoint_ratio, then send only context.project(messages).
outgoing = context.project(messages) if context is not None else messages
reply = llm.complete(outgoing, tools=defs, system=sys_prompt, **extra)
```

Keep subagent calls unchanged unless they receive their own context; session-local fold tools are excluded through `Tool.inheritable=False`.

- [ ] **Step 4: Run loop, compaction, permission, and failure tests and verify GREEN**

Run: `uv run pytest tests/test_folding_loop.py tests/test_loop.py tests/test_loop_failures.py tests/test_compact_trigger.py tests/test_permissions.py -v`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the loop mount**

```bash
git add harness/loop.py harness/tools/agent.py tests/test_folding_loop.py tests/test_compact_trigger.py
git commit -m "lesson 25: project folded context before model calls"
```

### Task 5: CLI/session mount and workspace-hygiene prompt

**Files:**
- Modify: `harness/prompts.py`
- Modify: `main.py`
- Modify: `tests/test_prompts.py`
- Create: `tests/test_folding_main.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `--fold-context`, existing session path, tool registry, and prompt sections.
- Produces: one SQLite ledger and one content-free JSONL decision log per session; registered `fold`/`unfold` tools; `WORKSPACE_HYGIENE` prompt section; resume reuses the same folding artifacts.

- [ ] **Step 1: Write failing CLI assembly and prompt tests**

```python
def test_workspace_hygiene_teaches_phase_boundary_folding_without_token_targets():
    assert "When a line of work CLOSES" in WORKSPACE_HYGIENE
    assert "future-you" in WORKSPACE_HYGIENE
    assert "token savings" not in WORKSPACE_HYGIENE.lower()

def test_fold_context_rejects_explicit_compaction(capsys):
    result = run_main("--fold-context", "--compact-threshold", "100")
    assert result.exit_code == 2
    assert "cannot be combined" in result.stderr

def test_resume_derives_the_same_fold_database_path(tmp_path):
    session = tmp_path / ".agent/sessions/s.jsonl"
    assert folding_paths(session) == (
        session.with_suffix(".folds.sqlite3"),
        session.with_suffix(".fold-decisions.jsonl"),
    )
```

- [ ] **Step 2: Run prompt and CLI tests and verify RED**

Run: `uv run pytest tests/test_prompts.py tests/test_folding_main.py -v`

Expected: missing `WORKSPACE_HYGIENE`, `folding_paths`, and CLI option failures.

- [ ] **Step 3: Mount folding in `main.py` and document usage**

```python
parser.add_argument(
    "--fold-context",
    action="store_true",
    help="enable recoverable context folding instead of compaction",
)

fold_db, decision_log = folding_paths(session_path)
folding = FoldingContext(
    fold_db,
    session_id=session_path.stem,
    decision_log_path=decision_log,
)
register_builtin("fold", fold_tool(folding))
register_builtin("unfold", unfold_tool(folding))
```

Append `WORKSPACE_HYGIENE` only for folding sessions, pass `context=folding` and `compact_threshold=None` to `run_turn`, close SQLite at shutdown, and explain the flag, marker IDs, checkpoint behavior, and artifact paths in `README.md`.

- [ ] **Step 4: Run the entire suite and verify GREEN**

Run: `uv run pytest`

Expected: all tests pass with no warnings or errors.

- [ ] **Step 5: Commit the user-facing mount**

```bash
git add harness/prompts.py main.py tests/test_prompts.py tests/test_folding_main.py README.md
git commit -m "lesson 25: expose recoverable context folding"
```

### Task 6: Final invariant audit and verification

**Files:**
- Modify only files implicated by verification failures.
- Review: `docs/features/context-folding-design.md`
- Review: all files changed in Tasks 1–5.

**Interfaces:**
- Consumes: complete implementation and tests.
- Produces: verified diff with no protected-span fold path, no parity gap, no raw secret in projected/persisted folding artifacts, and deterministic resume hashes.

- [ ] **Step 1: Run targeted invariant tests**

Run: `uv run pytest tests/test_folding.py tests/test_folding_tools.py tests/test_folding_loop.py tests/test_folding_main.py -v`

Expected: all context-folding tests pass.

- [ ] **Step 2: Run the complete offline suite**

Run: `uv run pytest`

Expected: all tests pass.

- [ ] **Step 3: Inspect the final diff and repository state**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git status --short`

Expected: only the supplied feature documents and intentional implementation/plan changes are present; no `.agent`, SQLite, cache, or temporary artifacts are tracked.

- [ ] **Step 4: Audit design coverage**

Confirm with searches and tests that the implementation includes stable IDs, written reasons, protected spans, overlap rejection, deferred checkpoints, tail unfold, poison quarantine, sensitive purge, parity linting, compaction exclusivity, Tier 1 rules, notices, decision logging, reconstruction, projection hashes, and resume. Record deliberate deferrals from the design’s sequencing section: Tier 3 critic, evaluation campaign runner, UI verbs, and external pi/Claude proxy mounts.

- [ ] **Step 5: Commit any verification-only corrections**

```bash
git add harness tests main.py README.md docs/superpowers/plans/2026-08-07-context-folding.md
git commit -m "lesson 25: finish context folding invariants"
```
