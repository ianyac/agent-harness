# Context-Folding Review Fixes Design

## Goal

Close the three merge-blocking review findings in the context-folding feature:

1. sensitive scanning must remove a detected credential from every persisted,
   live, and projected copy;
2. every model request must be reconstructable independently, including
   multiple requests in one user turn; and
3. user deletion must not rewrite unrelated transcript prose through broad
   lexical substitution.

The existing ledger/projection architecture, span state machine, and public
folding behavior remain intact.

## Erasure semantics

Erasure becomes one internal engine with two explicit policies.

### Sensitive policy

The scanner returns the concrete secret substrings it matched rather than a
boolean. High-confidence secret values are replaced wherever they occur in a
data-bearing field: ledger entry content and metadata, message snapshots, tool
arguments and canonical keys, fold notes, notices, stored request projections,
registered JSONL artifacts, the live shadow, and current notices. Protocol
keys, roles, tool names, call IDs, and other structural identifiers are never
rewritten.

The current tool-result span is then inserted directly in terminal `purged`
state with only its hash/token audit metadata, and its message content becomes
the credential-redaction marker. The database transaction records the scrub
and terminal fold together. External artifact rewriting is prepared before
the SQLite transaction so an artifact error cannot leave the ledger claiming
a purge that did not run.

Secret extraction retains the existing supported credential families. For
assignment-shaped matches such as `api_key=...`, the value is extracted as an
independent secret so a matching value in tool arguments is removed even when
the result contains additional prose.

### User-delete policy

User deletion is provenance-first, not global search-and-replace. It purges:

- the selected owning span and its chunk index;
- exact duplicate ledger entries and their owning message fields;
- the registered tool argument field for a selected tool-input span;
- folds and notices associated with affected spans;
- corresponding source messages in stored request projections; and
- exact values in mounted session/action artifacts.

Unrelated user, system, assistant, or tool prose containing the same common
word is not altered. An untracked embedded substring in another span is not
treated as a proven copy. This deliberately replaces the previous heuristic
that globally substituted any word-like payload of four characters or more.

The operation continues to include inactive crash-tail rows and uses SQLite
`secure_delete` plus `VACUUM`. Target and exact duplicate spans remain
terminally purged; user deletion cannot be unfolded.

## Request-level reconstruction

Schema version 3 extends each `projections` row with:

- `projection_json`: the exact canonical message array sent or checkpointed;
- `source_ids_json`: an aligned list identifying the source message/span for
  each projected message;
- `redacted`: whether later erasure changed the stored projection.

Projection construction records source IDs while it builds the outgoing
array. Ordinary projected messages use their ledger message ID; synthetic tail
messages use their span ID. The public `project()` and `reconstruct()` methods
continue returning message lists.

`reconstruct_projection(projection_id)` returns the stored canonical array. If
`redacted = 0`, it verifies that the array hashes to the persisted
`projection_hash` and raises `ProjectionError` on mismatch. If later erasure
changed the request, the method returns the redacted array, while
`projection_chain()` exposes `redacted = 1`; the original hash remains as audit
evidence and is explicitly no longer claimed to be reproducible.

This snapshot is an audit record of the actual model boundary, not a new source
of mutable conversation truth. The ledger remains authoritative for current
projection and turn-level reconstruction.

## Erasing stored requests

Source IDs make user deletion targeted inside stored projections:

- a deleted result source becomes the user-deletion marker;
- a deleted tool-input source replaces only its registered argument field;
- a deleted ordinary message source replaces only that message's content; and
- a deleted tail source becomes the user-deletion marker.

Sensitive erasure scans every data-bearing value in stored projections because
the values are already identified as credentials. Any changed projection is
marked `redacted` without changing its original hash or parent hash.

## Failure handling and compatibility

- Existing schema versions are rejected by the current explicit compatibility
  gate; there is no silent in-place migration for an unreleased lesson branch.
- Unknown or malformed stored projection JSON raises `ProjectionError` rather
  than returning unaudited bytes.
- Erasure never changes roles, dictionary keys, call IDs, tool names, or source
  mappings.
- A redacted historical request is intentionally not hash-verifiable. This is
  the hard-erasure exception approved for both user deletion and sensitive
  purging.

## Test design

Each behavior is added test-first and observed failing before production code
changes:

1. A credential present in user input, a tool argument, and a larger tool
   result is absent from the live shadow, projected model input, SQLite bytes,
   mounted artifacts, and stored request snapshots after scanning.
2. Deleting results equal to `true`, `error`, and `done` leaves unrelated user
   and system prose byte-identical while the selected span is terminally
   purged.
3. A two-request tool loop reconstructs each request by projection ID exactly,
   before and after reopening the database.
4. Deleting or scanning content that existed in an earlier request rewrites
   that stored request to markers, sets `redacted = 1`, and retains the original
   hash chain without claiming the redacted bytes match it.
5. Existing folding, rollback, inactive-branch, chunk, projection-parity, and
   schema-compatibility tests continue to pass.

Completion requires the complete repository test suite and a fresh independent
read-only code review with no Critical or Important findings.
