# Local service final-fix report

Date: 2026-08-08

Review base: `91de309`

Implementation commits: `90fc742`, `e69cc32`, `b0945b7`

## Outcome

The final coordinated pass closes C1, C2, and I1-I7 from `final-review.md`.
The warning-clean UI suite passes 387/387, focused service integration passes
251/251, and no production or test changes were made under the root `harness/`
or `tests/` trees. The web client was deliberately not implemented or changed.

The only non-green requested gate is the root suite under `-W error`. Both root
discovery forms pass 545/545 without warning escalation, but both expose the
same pre-existing resource-finalizer debt when warnings become errors. The
final-fix range has no root-harness diff.

## Finding closure

| Finding | Resolution | Regression authority |
| --- | --- | --- |
| C1 | A stable per-user, environment-independent coordination domain now holds an OS `flock` for the full secure lease. Its key includes the workspace device/inode and validated session ID. Coordination-root and lock entries are opened fail-closed with ownership, type, link-count, mode, no-follow, and post-lock identity checks. The coordination claim is released only after public lock, stage, and directory cleanup succeeds. | A subprocess cannot acquire after the public lock pathname is unlinked/replaced while the parent lease is live; it can acquire after release. Distinct sessions remain concurrent, unsafe coordination entries are rejected, and cleanup retries retain ownership. |
| C2 | Browser bootstrap exchanges the launch secret once for independent 32-byte static-path and API capabilities. No credential cookie is created, the launch secret is retired after exchange, and bootstrap/static responses use `Referrer-Policy: no-referrer`. Native Tauri launch-secret behavior remains supported. | Browser exchange, cross-port cookie absence, launch-secret retirement, static/API capability separation, concurrent launch isolation, and process startup are covered in `test_auth_boundary.py` and CLI tests. |
| I1 | One outer ASGI boundary now authenticates and validates origin before routing or body parsing, including arbitrary methods and `static_root=None`; only the intentional bootstrap exchange is exempt. | Anonymous or disallowed-origin malformed UTF-8/JSON cannot reach parsing. Route-independent method and factory configurations are covered. |
| I2 | Exact `tauri://localhost` CORS and preflight behavior is handled at the outer boundary without weakening bearer/origin enforcement. | Authenticated readable responses, allowed preflight, and rejected origin/header/method cases are covered. |
| I3 | Worker waits are shielded from caller cancellation. Explicit `active`, `draining`, and `cleanup_failed` lifecycle states retain worker/runtime/channel ownership through close completion and allow cleanup retry while rejecting reconnect, turns, transcript, and safety access. | Blocking-LLM archive cancellation and injected partial-close failure/retry regressions pass. |
| I4 | The pinned descriptor-backed JSONL log is the active transcript authority and its load/record/append/replace operations are serialized. A persistence failure reloads the public `SessionLog` boundary and restores both in-memory messages and the public recorded-length cursor before later turns. If that reload also fails, the runner restores its last known durable snapshot and the channel enters a fail-closed durability quarantine that rejects turns, reconnect, runtime open, and safety access. | In-flight messages are absent from REST transcripts; an injected partial append leaves no poisoned first turn, and a later complete pair survives reopen. A persistent append-plus-reload failure cannot reach channel messages or a reconnect snapshot. |
| I5 | Metadata mutation return-row construction now occurs inside each transaction, and transaction rollback covers `BaseException`. | Injected post-write record-construction failures leave no row, JSONL, private stage, or cached runtime for create; sibling mutation probes also roll back. |
| I6 | Recursive Unicode-scalar validation covers protocol payloads and keys, provider deltas/replies, decoded tool arguments, tool results/activity, loaded messages, descriptor-log writes, and compaction. Unsafe upstream text is converted into a UTF-8-safe typed terminal without mutating the durable boundary. | Malformed provider/tool payloads and a pre-poisoned completed log fail safely; no new JSONL poison is written. |
| I7 | Static resolution distinguishes ordinary SPA misses from unsafe paths and encoded/malformed service namespaces, rejecting the latter before fallback. | Encoded extra-leading-slash API/WS paths, traversal, backslash, symlink escape, and arbitrary unsafe paths fail closed. |

## Test-first evidence

- C2/I1/I2/I7 RED: `tests/test_auth_boundary.py` produced `16 failed, 1 passed` before production edits. GREEN: `17 passed`.
- C1/I3 RED: the three demonstrated subprocess/archive cases produced `3 failed`. GREEN: `3 passed`; the expanded coordination/retry selection passed `11 passed`.
- I4 RED: the original selection produced `2 failed`. GREEN: `2 passed`, including reopen authority after a partial append. Independent adversarial review then exposed a second-order append-plus-reload failure; its dedicated regression was RED `1 failed` and GREEN `1 passed` after durability quarantine was added.
- I5 RED: five metadata mutation probes failed and the manager ghost probe separately persisted a row. GREEN: the combined selection passed `6 passed` with no ghost artifacts.
- I6 RED: the primary malformed-Unicode selection produced `7 failed`, and the malformed tool-result case separately produced `1 failed`. GREEN: `7 passed` and `2 passed`, respectively.

Detailed command evidence is retained in `auth-boundary-fix-notes.md` and
`runtime-authority-fix-notes.md` beside this report.

## Web-client adapter/router carry-forward

No file under `ui/frontend/` changed. A later web-client integration must carry
forward this exact boundary contract:

1. Browser startup opens the one-shot
   `http://127.0.0.1:<port>/bootstrap?token=<launch-secret>` URL and follows the
   redirect to `/_app/<static-capability>/#token=<api-capability>`.
2. The router base, SPA routes, and relative static-asset URLs remain below
   `/_app/<static-capability>/`; the bare `/` is not the application root.
3. The transport adapter keeps the fragment API capability in memory, removes
   it from the visible URL after capture, sends
   `Authorization: Bearer <api-capability>` on every API call, and opens the
   WebSocket with subprotocols `harness-ui` and `<api-capability>` in that order.
4. Browser code must not put either capability in cookies, query strings,
   local/session storage, logs, or referrers. It must not reuse the retired
   launch secret as an API/static credential.
5. The native Tauri adapter continues using its sidecar-delivered launch secret
   directly as the API bearer/WebSocket credential with origin
   `tauri://localhost`; it does not perform the browser bootstrap exchange.

The server-facing form of this contract is also documented in `ui/README.md`.

## Verification

| Check | Result |
| --- | --- |
| Focused warning-clean service integration (`protocol`, `bridge`, `runner`, `runtime`, `metadata`, REST, WS) | `251 passed in 2.45s` |
| Auth/application warning-clean focus | `265 passed` |
| Full UI, `cd ui && uv run pytest -W error -q` | `387 passed in 7.91s` |
| Root discovery, `uv run pytest -q` | `545 passed in 12.42s` |
| Explicit root scope, `uv run pytest tests -q` | `545 passed in 12.46s` |
| Root warning-clean discovery, both `uv run pytest -W error -q` and `uv run pytest tests -W error -q` | each `532 passed, 12 failed, 1 error` |
| UI Python compilation, `python -m py_compile server/*.py tests/*.py` | pass |
| Targeted durability/worker/subprocess lifecycle probes | `5 passed` |
| Task-leak probe after one complete worker turn and manager close | `0` pending tasks |
| FD-leak probe across 25 secure acquire/abort cycles | `4` before, `4` after; `0` process claims |
| `git diff --check 91de309..HEAD` | pass |
| Root-harness diff (`harness`, root `tests`, `pyproject.toml`) | empty |
| Web-client diff (`ui/frontend`) | empty |

## Independent adversarial self-review

The read-only independent review reported no Critical issue and one Important
I4 edge case: a simultaneous append and authoritative-reload failure left the
unverified completed turn in `runtime.messages`, which the channel could copy
for a later snapshot. That case was converted into the deterministic RED
regression recorded above. Commit `b0945b7` preserves the latest trusted
durable snapshot, tracks successful compact/turn boundaries, quarantines the
channel on unresolved authority failure, and rejects all paths that could
reuse it. The regression, focused integration, full warning-clean UI suite,
compilation, and leak probes all passed after the fix. The independent review
reported no other Critical or Important findings.

For one normalized root warning-strict run, the emitted warning categories were
152 unclosed `sqlite3.Connection` reports, one unclosed `BufferedRandom`, one
unclosed read `FileIO`, one unclosed write `FileIO`, and one live-subprocess
report. Ten failed test outcomes were folding SQLite finalizers; the remaining
hook/MCP failure and following skills setup error were file/subprocess
finalizers. There were no assertion failures. The same tree passes all 545
root tests without warning escalation, and `91de309..HEAD` does not touch the
root sources/tests that own those finalizers.

## Scope and remaining concerns

- Root warning escalation remains a baseline gate concern outside this
  local-service-only fix pass.
- The accepted deferred minors remain: explicit plan-entry safety snapshot
  observability, duplicate sidecar JSON-member rejection, a raw-stderr secrecy
  assertion, and static conditional/ETag policy.
- Frozen tiktoken resource-root validation remains owned by the downstream
  macOS packaging plan.
- No hooks, MCP actions, skill commands, web-client integration, commits to
  remote, or merge actions were performed.
