# Context-Folding Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sensitive purging comprehensive, make every outbound request independently reconstructable, and make user deletion provenance-safe.

**Architecture:** Keep `FoldingContext` as the ledger coordinator, but separate the two erasure policies at the method boundary: sensitive values are high-confidence substrings scrubbed from every data field, while user deletion operates on discovered span/message aliases and exact values only. Persist the exact outbound projection plus aligned source identifiers so request reconstruction is independent of coarse user-turn numbers; erasure rewrites affected snapshots and marks them redacted without changing their original hash chain.

**Tech Stack:** Python 3.12, SQLite, pytest, existing `harness.folding` ledger/projection code, existing `FakeLLM` integration harness.

## Global Constraints

- A sensitive value must not remain in SQLite bytes, mounted JSONL artifacts, live shadow messages, or projected model input after the scanner fires.
- User deletion must never change roles, dictionary keys, call IDs, tool names, or unrelated prose.
- User deletion is provenance-first: selected spans, chunk indexes, exact duplicate entries, registered tool-input fields, associated folds/notices, and aligned stored projections only.
- Existing schema versions are rejected explicitly; schema version 3 is not silently migrated.
- Non-redacted stored projections must hash to their persisted `projection_hash`.
- Redacted projections retain their original hash/parent hash and expose `redacted = 1` rather than claiming the new bytes reproduce the old hash.
- Every production behavior is introduced by a regression test that is observed failing first.

---

## File map

- `harness/folding.py` — schema v3, secret extraction, sensitive purge orchestration, provenance-safe user deletion, projection source capture, stored request reconstruction, and projection redaction.
- `harness/loop.py` — no API redesign; continues calling `project()` followed immediately by `record_request()`, which consumes the captured source mapping.
- `tests/test_folding.py` — unit/integration regressions for sensitive copies, deletion safety, projection persistence, redaction, restart, and schema behavior.
- `tests/test_folding_loop.py` — real multi-iteration model-loop request reconstruction.
- `README.md` — document request-level reconstruction and the hard-erasure exception.

---

### Task 1: Comprehensive sensitive-value purge

**Files:**
- Modify: `harness/folding.py:46-68, 350-425, 530-590, 1000-1385`
- Test: `tests/test_folding.py`

**Interfaces:**
- Consumes: existing `_SECRET_PATTERNS`, `_scrub_structured`, `_scrub_sqlite`, `_purge_session_log`, `_scrub_live_shadow`.
- Produces: `FoldingContext._secret_values(content: str) -> tuple[str, ...]`, substring-aware scrub helpers, and `_vacuum_pending` handling at the end of `sync()`.

- [ ] **Step 1: Add the failing credential-alias regression**

Add a test using a real `FoldingContext`, mounted session/action logs, and a credential present in user content, assistant tool arguments, and a larger tool result:

```python
def test_scanner_purges_a_matched_secret_from_every_local_alias(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    path = tmp_path / "folds.sqlite3"
    session_path = tmp_path / "session.jsonl"
    actions_path = tmp_path / "actions.jsonl"
    session_path.write_text(
        json.dumps({"type": "message", "message": {"role": "user", "content": secret}})
        + "\n"
    )
    actions_path.write_text(
        json.dumps({"name": "leak", "args": {"token": secret}}) + "\n"
    )
    messages = [
        {"role": "user", "content": f"inspect {secret}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_0",
                "type": "function",
                "function": {
                    "name": "leak",
                    "arguments": json.dumps({"token": secret}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_0",
            "content": f"diagnostic prefix {secret} diagnostic suffix",
        },
    ]
    context = FoldingContext(path, "session", session_log_path=session_path)
    context.register_purge_path(actions_path)
    context.sync(messages, {"leak": noop_tool(name="leak")})

    assert context.state("m2.r0") == "purged"
    assert secret not in json.dumps(messages)
    assert secret not in json.dumps(context.shadow_messages())
    assert secret not in json.dumps(context.project(messages))
    assert secret not in session_path.read_text()
    assert secret not in actions_path.read_text()
    assert secret.encode() not in path.read_bytes()
```

The production mutation this catches is restoring the current boolean-only scanner branch that purges only `m2.r0`.

- [ ] **Step 2: Run the test and verify the security failure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest tests/test_folding.py::test_scanner_purges_a_matched_secret_from_every_local_alias -q
```

Expected: FAIL because the secret remains in assistant arguments, live shadow/SQLite, and mounted artifacts.

- [ ] **Step 3: Replace boolean detection with concrete secret extraction**

Reshape assignment patterns to expose the credential value and add:

```python
@staticmethod
def _secret_values(content: str) -> tuple[str, ...]:
    values: list[str] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(content):
            value = match.groupdict().get("secret") or match.group(0)
            if value and value not in values:
                values.append(value)
    return tuple(values)
```

Keep `_contains_secret()` as a compatibility wrapper returning
`bool(FoldingContext._secret_values(content))` only if existing tests call it.

- [ ] **Step 4: Make scrub policy explicit**

Change the recursive data scrubber to require a keyword policy:

```python
def _scrub_data(
    cls,
    value: object,
    erased: tuple[str, ...] | list[str],
    marker: str,
    *,
    replace_substrings: bool,
) -> object:
    if isinstance(value, dict):
        return {
            key: cls._scrub_data(
                item,
                erased,
                marker,
                replace_substrings=replace_substrings,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            cls._scrub_data(
                item,
                erased,
                marker,
                replace_substrings=replace_substrings,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    if value in erased:
        return marker
    cleaned = value
    if replace_substrings:
        for content in erased:
            if content:
                cleaned = cleaned.replace(content, marker)
    return cleaned
```

Exact string equality always becomes the marker. Substring replacement runs
only when `replace_substrings=True`; remove length/word heuristics from this
primitive. Thread the same flag through `_scrub_structured`,
`_scrub_arguments`, `_scrub_canonical_key`, `_scrub_text`, `_scrub_sqlite`,
`_purge_session_log`, and `_scrub_live_shadow`.

- [ ] **Step 5: Scrub sensitive values before recording the terminal result**

In `_ingest_tool_result`, compute `secrets = self._secret_values(content)`. When
non-empty:

```python
self._purge_session_log(secrets, _REDACTION_MARKER, replace_substrings=True)
sanitized_meta = dict(self._scrub_structured(
    meta,
    secrets,
    _REDACTION_MARKER,
    replace_substrings=True,
))
with self._db:
    self._scrub_sqlite(secrets, _REDACTION_MARKER, replace_substrings=True)
    self._scrub_live_shadow(secrets, _REDACTION_MARKER, replace_substrings=True)
    self._current_notices = [
        str(self._scrub_data(
            notice,
            secrets,
            _REDACTION_MARKER,
            replace_substrings=True,
        ))
        for notice in self._current_notices
    ]
    self._insert_entry(
        span_id,
        None,
        "tool_result",
        "tool",
        None,
        sanitized_meta,
        content_sha=_sha(content),
        tokens_est=count_text_tokens(content),
        state="purged",
    )
    self._db.execute(
        "INSERT INTO folds(span_id, reason, note, decider, folded_turn, "
        "placement, applied_turn) VALUES (?, 'sensitive', ?, 'scanner', ?, "
        "'in_place', ?)",
        (span_id, "credential detected in tool output", self.turn, self.turn),
    )
message["content"] = _REDACTION_MARKER
self._vacuum_pending = True
```

Ensure `sanitized_meta` is produced by the same substring scrub before insert.
At the end of `sync()`, commit first, then run `VACUUM` once when
`_vacuum_pending` is true and clear the flag.

- [ ] **Step 6: Verify the focused scanner and existing folding tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest \
  tests/test_folding.py::test_scanner_purges_a_matched_secret_from_every_local_alias \
  tests/test_folding.py \
  tests/test_folding_loop.py -q
```

Expected: PASS. Confirm the output contains no warnings or skipped new test.

- [ ] **Step 7: Commit the sensitive purge fix**

```bash
git add harness/folding.py tests/test_folding.py
git commit -m "lesson 25: purge every sensitive value alias"
```

---

### Task 2: Provenance-safe user deletion

**Files:**
- Modify: `harness/folding.py:1020-1385`
- Test: `tests/test_folding.py`

**Interfaces:**
- Consumes: explicit `replace_substrings` scrub policy from Task 1 and existing span metadata (`parent_id`, `origin`, `meta_json`).
- Produces: `_user_delete_aliases(target: str) -> tuple[set[str], set[str]]` returning affected span IDs and owner message IDs; targeted message/tool-call/notices scrubbing.

- [ ] **Step 1: Add failing common-word deletion regressions**

Add one parameterized real-ledger test:

```python
@pytest.mark.parametrize("payload", ["true", "error", "done"])
def test_user_delete_preserves_unrelated_prose_containing_common_payloads(
    tmp_path, payload
):
    messages = [
        {"role": "system", "content": f"keep the {payload} branch intact"},
        *tool_exchange("noop", {}, payload),
        {"role": "user", "content": f"the word {payload} here is unrelated"},
    ]
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0),
    )
    context.sync(messages, {"noop": noop_tool()})
    system_before = deepcopy(messages[0])
    user_before = deepcopy(messages[-1])

    context.delete("m3.r0")

    assert messages[0] == system_before
    assert messages[-1] == user_before
    assert context.state("m3.r0") == "purged"
    assert context.project(messages)[3]["content"] == "[deleted by user]"
```

The production mutation this catches is reintroducing global word-boundary
replacement in `_scrub_data()` for user deletion.

- [ ] **Step 2: Run the test and verify unrelated prose is rewritten**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest tests/test_folding.py::test_user_delete_preserves_unrelated_prose_containing_common_payloads -q
```

Expected: three FAIL results showing the payload replaced in system/user prose.

- [ ] **Step 3: Discover aliases from ledger provenance**

Implement:

```python
def _user_delete_aliases(self, target: str) -> tuple[set[str], set[str]]:
    root = self._entry(target)
    payload = root["content"]
    span_ids = {target, *self.child_ids(target)}
    if payload is not None:
        exact = self._db.execute(
            "SELECT span_id FROM entries WHERE session_id = ? "
            "AND content = ? AND (parent_id IS NULL OR origin = 'tool_input')",
            (self.session_id, payload),
        ).fetchall()
        for row in exact:
            span_ids.add(row["span_id"])
            span_ids.update(self.child_ids(row["span_id"]))
    owner_ids = {span_id.split(".", 1)[0] for span_id in span_ids}
    return span_ids, owner_ids
```

When an exact child chunk matches the target payload, include its result parent
as an affected alias and sanitize/repartition only that parent. Do not treat a
payload crossing unrelated chunk boundaries as an alias merely because its
concatenated parent contains the same substring.

- [ ] **Step 4: Replace global delete scrubbing with targeted updates**

Refactor `delete()` so it:

1. resolves a selected result chunk to its owning result;
2. discovers affected span/message IDs;
3. rewrites mounted artifacts with `replace_substrings=False`;
4. marks selected/exact alias entries and their descendants purged;
5. updates only owning `messages.message_json` fields;
6. updates `tool_calls.args_json`/`canonical_key` only for affected tool-input
   owners;
7. scrubs folds/notices joined to affected span IDs; and
8. updates only matching live-shadow indices derived from `_active_ids`.

Use the existing `_mark_entry_purged()` and chunk reconciliation for affected
result parents. Remove user-delete calls that scan every message/notice with
substring replacement.

- [ ] **Step 5: Update obsolete embedded-substring tests to the approved semantics**

Change the cross-boundary tests so an unrelated larger result containing the
payload remains byte-identical, while target/exact duplicate roots and exact
indexed child aliases are still purged. Keep the inactive-row assertions for
actual affected aliases.

Use literal expectations; do not compute expected content with the production
scrubber.

- [ ] **Step 6: Verify deletion behavior and resume durability**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest \
  tests/test_folding.py::test_user_delete_preserves_unrelated_prose_containing_common_payloads \
  tests/test_folding.py -q
```

Expected: PASS, including short payload, duplicate root, exact chunk alias,
inactive crash-tail, and stale-session-resume regressions.

- [ ] **Step 7: Commit provenance-safe deletion**

```bash
git add harness/folding.py tests/test_folding.py
git commit -m "lesson 25: make user deletion provenance safe"
```

---

### Task 3: Persist and reconstruct every outbound projection

**Files:**
- Modify: `harness/folding.py:70-190, 1675-1716, 1765-2075`
- Test: `tests/test_folding.py`
- Test: `tests/test_folding_loop.py`

**Interfaces:**
- Consumes: current `project()`, `_project_with_ids()`, `_record_projection()`, `record_request()`, `projection_chain()`, and the two erasure policies.
- Produces: schema version 3, aligned source IDs, `reconstruct_projection(projection_id: int) -> list[dict]`, redacted projection records.

- [ ] **Step 1: Add the failing multi-request reconstruction integration test**

In `tests/test_folding_loop.py`, run a real two-request tool turn and assert the
stored request rows reproduce the exact arrays received by `FakeLLM`:

```python
def test_each_model_request_can_be_reconstructed_after_reopen(tmp_path):
    path = tmp_path / "folds.sqlite3"
    context = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    tool = noop_tool(name="noop")
    llm = FakeLLM([
        {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
        {"type": "text", "content": "done"},
    ])
    messages: list[dict] = []
    run_turn(messages, "inspect", llm, tools={"noop": tool}, context=context)
    expected = deepcopy([turn["messages"] for turn in llm.turns])
    request_ids = [
        row["projection_id"]
        for row in context.projection_chain()
        if row["kind"] == "request"
    ]
    context.close()

    resumed = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    assert [resumed.reconstruct_projection(row_id) for row_id in request_ids] == expected
```

The production mutation this catches is reducing reconstruction to
`created_turn <= N`.

- [ ] **Step 2: Run the test and verify the API/boundary is missing**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest tests/test_folding_loop.py::test_each_model_request_can_be_reconstructed_after_reopen -q
```

Expected: FAIL because `projection_id` is absent and
`reconstruct_projection()` is undefined.

- [ ] **Step 3: Extend schema version 3**

Set `_SCHEMA_VERSION = 3` and define projection columns:

```sql
projection_json TEXT NOT NULL,
source_ids_json TEXT NOT NULL,
redacted INTEGER NOT NULL DEFAULT 0
```

Keep the existing explicit version rejection. New rows always provide all
three values; no default empty projection is accepted.

- [ ] **Step 4: Capture aligned projection source identifiers**

Refactor projection building into:

```python
def _build_projection(
    self,
    messages: list[dict],
    message_ids: list[str],
    turn: int | None,
) -> tuple[list[dict], list[str]]:
    projected: list[dict] = []
    sources: list[str] = []

    def append(message: dict, source: str) -> None:
        projected.append(message)
        sources.append(source)

    # Move the existing _project_with_ids loop here. Replace each existing
    # projected.append(message) with append(message, f"message:{message_id}").
    # Replace the tail append with append(message, f"span:{span_id}").
    self._lint(projected)
    return projected, sources
```

Append `f"message:{message_id}"` whenever a projected source message is
appended. Append `f"span:{span_id}"` for synthetic tail messages. When
quarantine removes a tool result, append neither its message nor a source ID.

`_project_with_ids()` calls `_build_projection()`, stores
`(_sha(_canonical(projected)), source_ids)` in `_last_projection_sources`, and
returns only `projected` to preserve the public API.

- [ ] **Step 5: Persist and verify projection snapshots**

Update `_record_projection()` to require aligned sources captured for the same
hash and insert canonical messages/sources. If the caller supplies an array
without a matching captured source map, persist a list of `"unknown"` entries
of equal length rather than misaligning identifiers.

Add:

```python
def reconstruct_projection(self, projection_id: int) -> list[dict]:
    row = self._db.execute(
        "SELECT projection_hash, projection_json, redacted "
        "FROM projections WHERE projection_id = ?",
        (projection_id,),
    ).fetchone()
    if row is None:
        raise ProjectionError(f"unknown projection {projection_id}")
    try:
        messages = json.loads(row["projection_json"])
    except json.JSONDecodeError as error:
        raise ProjectionError(f"projection {projection_id} is malformed") from error
    if not row["redacted"] and _sha(_canonical(messages)) != row["projection_hash"]:
        raise ProjectionError(f"projection {projection_id} hash mismatch")
    return messages
```

Return `projection_id` and boolean `redacted` from `projection_chain()` in
addition to its current fields.

- [ ] **Step 6: Verify exact reconstruction before erasure**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest \
  tests/test_folding_loop.py::test_each_model_request_can_be_reconstructed_after_reopen \
  tests/test_folding.py::test_each_checkpoint_extends_a_persisted_projection_hash_chain -q
```

Expected: PASS with two distinct request projection IDs in the same turn.

- [ ] **Step 7: Add failing redacted-history tests**

Add two tests in `tests/test_folding.py`:

```python
def test_user_delete_redacts_only_affected_stored_projection_sources(tmp_path):
    messages = tool_exchange("noop", {}, "true")
    messages[0]["content"] = "keep the true branch intact"
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0),
    )
    context.sync(messages, {"noop": noop_tool()})
    outgoing = context.project(messages)
    context.record_request(outgoing)
    before = context.projection_chain()[-1]
    context.delete("m2.r0")
    after = context.projection_chain()[-1]
    historical = context.reconstruct_projection(after["projection_id"])
    assert after["projection_hash"] == before["projection_hash"]
    assert after["redacted"] == 1
    assert historical[2]["content"] == "[deleted by user]"
    assert historical[0] == {"role": "user", "content": "keep the true branch intact"}


def test_sensitive_scan_redacts_stored_requests_without_changing_hash_chain(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    messages = [{"role": "user", "content": f"inspect {secret}"}]
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0),
    )
    context.sync(messages)
    context.record_request(context.project(messages))
    projection_id = context.projection_chain()[0]["projection_id"]
    messages.extend([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_0",
                "type": "function",
                "function": {
                    "name": "leak",
                    "arguments": json.dumps({"token": secret}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_0",
            "content": f"observed {secret} in output",
        },
    ])
    context.sync(messages, {"leak": noop_tool(name="leak")})
    assert secret not in json.dumps(context.reconstruct_projection(projection_id))
    assert context.projection_chain()[0]["redacted"] == 1
```

Use complete real setup from Tasks 1 and 2; the literal unrelated message must
contain the deleted common word so source targeting is proven.

- [ ] **Step 8: Run the redaction tests and verify snapshots retain erased data**

Run the two exact node IDs. Expected: FAIL because erasure does not yet update
`projection_json`/`redacted`.

- [ ] **Step 9: Integrate stored projections with both erasure policies**

Add:

```python
def _redact_sensitive_projections(self, secrets: tuple[str, ...]) -> None:
    rows = self._db.execute(
        "SELECT projection_id, projection_json FROM projections"
    ).fetchall()
    for row in rows:
        decoded = json.loads(row["projection_json"])
        cleaned = self._scrub_structured(
            decoded,
            secrets,
            _REDACTION_MARKER,
            replace_substrings=True,
        )
        if cleaned != decoded:
            self._db.execute(
                "UPDATE projections SET projection_json = ?, redacted = 1 "
                "WHERE projection_id = ?",
                (_canonical(cleaned), row["projection_id"]),
            )

def _redact_user_projections(
    self,
    operations: dict[str, dict[str, object]],
) -> None:
    rows = self._db.execute(
        "SELECT projection_id, projection_json, source_ids_json FROM projections"
    ).fetchall()
    for row in rows:
        messages = json.loads(row["projection_json"])
        sources = json.loads(row["source_ids_json"])
        changed = False
        for message, source in zip(messages, sources):
            source_id = source.split(":", 1)[1] if ":" in source else source
            operation = operations.get(source_id)
            if operation is None:
                continue
            marker = str(operation["marker"])
            if operation["kind"] in {"content", "result"}:
                message["content"] = marker
                changed = True
                continue
            call_id = operation["call_id"]
            field = str(operation["field"])
            for call in message.get("tool_calls") or []:
                if call["id"] != call_id:
                    continue
                arguments = json.loads(call["function"]["arguments"])
                arguments[field] = marker
                call["function"]["arguments"] = _canonical(arguments)
                changed = True
        if changed:
            self._db.execute(
                "UPDATE projections SET projection_json = ?, redacted = 1 "
                "WHERE projection_id = ?",
                (_canonical(messages), row["projection_id"]),
            )
```

`operations` maps owner IDs to one of these literal shapes:

```python
{"kind": "content", "marker": "[deleted by user]"}
{"kind": "tool_input", "call_id": "call_0", "field": "content", "marker": "[deleted by user]"}
{"kind": "result", "marker": "[deleted by user]"}
```

For sensitive erasure, scrub every data-bearing value with
`replace_substrings=True`. For user deletion, parse `source_ids_json`, alter
only messages whose source ID is present in `operations`, preserve all
structural fields, and set `redacted=1` only on changed rows. Never update
`projection_hash` or `parent_hash`.

- [ ] **Step 10: Verify projection reconstruction and erasure together**

Run:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest \
  tests/test_folding.py \
  tests/test_folding_loop.py -q
```

Expected: PASS, including exact pre-erasure hashes and explicit redacted
post-erasure snapshots.

- [ ] **Step 11: Commit request-level reconstruction**

```bash
git add harness/folding.py tests/test_folding.py tests/test_folding_loop.py
git commit -m "lesson 25: reconstruct every projected request"
```

---

### Task 4: Documentation, full verification, and review

**Files:**
- Modify: `README.md`
- Modify if test evidence requires: `harness/folding.py`
- Test: entire repository

**Interfaces:**
- Consumes: `projection_chain()` with `projection_id`/`redacted`, and `reconstruct_projection()` from Task 3.
- Produces: user-facing documentation and verified merge-ready branch.

- [ ] **Step 1: Document request reconstruction and erasure semantics**

Add concise README text stating:

```markdown
Each model dispatch stores its exact projected request and aligned ledger
sources. `reconstruct_projection(id)` verifies non-redacted snapshots against
the persisted hash. User deletion and sensitive scanning rewrite affected
historical snapshots to markers, preserve the original hash chain, and expose
`redacted: true` because hard-erased bytes are intentionally no longer
reconstructable.
```

Also state that user deletion follows ledger provenance and exact aliases; it
is not a global find-and-replace operation.

- [ ] **Step 2: Run focused formatting and diff checks**

Run:

```bash
git diff --check
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest \
  tests/test_folding.py tests/test_folding_heuristics.py \
  tests/test_folding_loop.py tests/test_folding_main.py \
  tests/test_folding_tools.py -q
```

Expected: clean diff and all focused folding tests pass.

- [ ] **Step 3: Run the complete suite with the macOS sandbox test enabled**

Run outside the managed sandbox when required:

```bash
UV_CACHE_DIR=/private/tmp/agent-harness-uv-cache uv run pytest -q
```

Expected: all tests pass, including
`tests/test_sandbox.py::test_write_inside_workspace_succeeds`.

- [ ] **Step 4: Commit documentation and any final test-only adjustments**

```bash
git add README.md harness/folding.py tests
git commit -m "docs: explain folding erasure and request replay"
```

- [ ] **Step 5: Request a fresh read-only code review**

Provide the reviewer with:

```text
Requirements: docs/superpowers/specs/2026-08-07-context-folding-review-fixes-design.md
Base: eaffb847bd32e843333df9344cf5e41f4a42f737
Head: current lesson-25 HEAD
Focus: credential copies, unrelated-content deletion, request-level reconstruction,
hard-erasure redaction, schema compatibility, and regression coverage.
```

Expected: no Critical or Important findings. If the reviewer finds one, add a
failing regression, fix it, rerun focused/full verification, and request review
again before handoff.

- [ ] **Step 6: Confirm final branch state**

Run:

```bash
git status --short
git log --oneline --decorate main..HEAD
git diff --check main...HEAD
```

Expected: clean worktree, intentional lesson-25 commits only, and no whitespace
errors.
