"""Persistent, deterministic projection of a foldable conversation ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

from harness.compaction import count_text_tokens, estimate_tokens
from harness.tools.base import Tool


class FoldError(ValueError):
    """A requested fold-state transition is invalid."""


class ProjectionError(ValueError):
    """The projected message array would violate provider structure."""


AGENT_REASONS = (
    "duplicate",
    "superseded",
    "finished",
    "irrelevant",
    "handled_failure",
    "scaffolding",
    "poisoned",
)

_GENERIC_NOTE = re.compile(
    r"^(?:no longer needed|not needed|cleanup|done(?: with)?(?: [\w -]+)?|"
    r"finished(?: with)?(?: [\w -]+)?|irrelevant)(?:[.!])?$",
    re.IGNORECASE,
)
_INSTRUCTION_NOTE = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|system|user)\s+"
    r"(?:instructions?|messages?|prompts?)|"
    r"\byou must\b|\bfollow these instructions\b|"
    r"\bcall (?:the )?[\w.-]+ tool\b|^(?:system|assistant|user)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_IMPERATIVE_NOTE = re.compile(
    r"^\s*(?:please\s+)?(?:always\s+|never\s+)?(?:answer|call|change|delete|"
    r"download|execute|ignore|open|remove|replace|return|send|upload|"
    r"(?:install|read|run|write)(?!\s+(?:completed|confirmed|failed|found|returned|"
    r"showed|succeeded)\b))\b|\b(?:you|the agent|the assistant)\s+"
    r"(?:must|should|need to)\b|"
    r"[\[\]]|<\|(?:system|assistant|user|tool)",
    re.IGNORECASE | re.MULTILINE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9_./+=-]{20,}"
    ),
)
_REDACTION_MARKER = "[redacted — credential detected in tool output]"


@dataclass(frozen=True)
class FoldConfig:
    min_span_tokens: int = 500
    chunk_tokens: int = 2_000
    checkpoint_ratio: float = 0.15

    def __post_init__(self) -> None:
        if self.min_span_tokens < 0:
            raise ValueError("min_span_tokens must be non-negative")
        if self.chunk_tokens <= 0:
            raise ValueError("chunk_tokens must be positive")
        if not 0 < self.checkpoint_ratio <= 1:
            raise ValueError("checkpoint_ratio must be in (0, 1]")


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS entries (
    span_id       TEXT PRIMARY KEY,
    parent_id     TEXT,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    origin        TEXT NOT NULL,
    content       BLOB,
    content_sha   TEXT NOT NULL,
    tokens_est    INTEGER NOT NULL,
    created_turn  INTEGER NOT NULL,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS folds (
    fold_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id        TEXT NOT NULL REFERENCES entries(span_id),
    reason         TEXT NOT NULL,
    note           TEXT NOT NULL,
    decider        TEXT NOT NULL,
    folded_turn    INTEGER NOT NULL,
    unfolded_turn  INTEGER,
    placement      TEXT,
    applied_turn   INTEGER
);

CREATE TABLE IF NOT EXISTS span_state (
    span_id  TEXT PRIMARY KEY REFERENCES entries(span_id),
    state    TEXT NOT NULL DEFAULT 'visible'
);

CREATE TABLE IF NOT EXISTS messages (
    message_id    TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    ledger_order  INTEGER NOT NULL,
    role          TEXT NOT NULL,
    message_json  TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    created_turn  INTEGER NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(session_id, ledger_order)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id       TEXT NOT NULL,
    message_id    TEXT NOT NULL REFERENCES messages(message_id),
    call_index    INTEGER NOT NULL,
    tool_name     TEXT NOT NULL,
    args_json     TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    result_span   TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(message_id, call_id)
);

CREATE TABLE IF NOT EXISTS projections (
    projection_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    projection_hash  TEXT NOT NULL,
    parent_hash      TEXT,
    turn             INTEGER NOT NULL,
    tokens_est       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session_config (
    session_id   TEXT PRIMARY KEY,
    config_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pins (
    span_id      TEXT PRIMARY KEY REFERENCES entries(span_id),
    pinned_turn  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id       TEXT,
    message_id    TEXT,
    kind          TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_turn  INTEGER NOT NULL,
    emitted_turn  INTEGER
);
"""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _token_label(tokens: int) -> str:
    if tokens < 1_000:
        return str(tokens)
    value = tokens / 1_000
    rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}K"


def _split_span(text: str, limit: int) -> list[str]:
    """Split once at ingestion, preferring line boundaries.

    A line larger than the configured token window is sliced by encoded token
    windows. Importing the shared encoding lazily keeps normal module import
    network-free, matching compaction's behavior.
    """
    if count_text_tokens(text) <= limit:
        return [text]

    from harness.compaction import _encoding  # one tokenizer for both policies

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if count_text_tokens(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            tokens = _encoding().encode(line, disallowed_special=())
            chunks.extend(
                _encoding().decode(tokens[start : start + limit])
                for start in range(0, len(tokens), limit)
            )
            continue
        candidate = current + line
        if current and count_text_tokens(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


class FoldingContext:
    """Shadow-ledger index plus a pure message projection.

    The caller retains the full transcript. ``sync`` appends only unseen
    messages to SQLite and remembers which stable ledger IDs correspond to the
    caller's current list. ``project`` deep-copies that list and decorates tool
    outputs; it never mutates the caller's ordinary conversation history.
    """

    def __init__(
        self,
        path: Path,
        session_id: str,
        decision_log_path: Path | None = None,
        config: FoldConfig = FoldConfig(),
        session_log_path: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.decision_log_path = (
            Path(decision_log_path) if decision_log_path is not None else None
        )
        self.session_log_path = (
            Path(session_log_path) if session_log_path is not None else None
        )
        self.config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        snapshot = _canonical(
            {
                "harness_version": "0.1.0",
                "marker_template_version": 2,
                "token_estimator_version": "o200k_base-v1",
                "tier1_ruleset_hash": _sha(
                    "duplicate|superseded_read|handled_failure|write_payload|agent_brief:v1"
                ),
                "min_span_tokens": config.min_span_tokens,
                "chunk_tokens": config.chunk_tokens,
                "checkpoint_ratio": config.checkpoint_ratio,
            }
        )
        existing_config = self._db.execute(
            "SELECT config_json FROM session_config WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()
        new_config = existing_config is None
        if existing_config is None:
            self._db.execute(
                "INSERT INTO session_config(session_id, config_json) VALUES (?, ?)",
                (self.session_id, snapshot),
            )
            self._db.commit()
        elif existing_config["config_json"] != snapshot:
            self._db.close()
            raise FoldError("resume config does not match the session's immutable snapshot")
        self._active_ids: list[str] = []
        self._snapshots: list[str] = []
        self._shadow_ref: list[dict] | None = None
        self._event_seq = 0
        self._current_notices: list[str] = []
        self._turn_start_length = 0
        self._turn_user_id: str | None = None
        row = self._db.execute(
            "SELECT COALESCE(MAX(created_turn), 0) AS turn FROM messages "
            "WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()
        self.turn = int(row["turn"])
        if new_config:
            self._event("config_epoch", config_hash=_sha(snapshot))

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> FoldingContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def sync(
        self,
        messages: list[dict],
        tools: dict[str, Tool] | None = None,
    ) -> None:
        tools = tools or {}
        self._restore_purged_messages(messages)
        self._shadow_ref = messages
        serialized = [_canonical(message) for message in messages]
        common = 0
        for old, new in zip(self._snapshots, serialized):
            if old != new:
                break
            common += 1
        if common < len(self._snapshots):
            self._active_ids = self._active_ids[:common]
            self._snapshots = self._snapshots[:common]

        if not self._snapshots:
            persisted = self._db.execute(
                "SELECT message_id, message_json FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY ledger_order",
                (self.session_id,),
            ).fetchall()
            for row, current in zip(persisted, serialized):
                if row["message_json"] != current:
                    break
                self._active_ids.append(row["message_id"])
                self._snapshots.append(current)
            if len(self._snapshots) == len(serialized) < len(persisted):
                abandoned = [row["message_id"] for row in persisted[len(serialized) :]]
                placeholders = ",".join("?" for _ in abandoned)
                with self._db:
                    self._db.execute(
                        f"UPDATE messages SET active = 0 WHERE message_id IN ({placeholders})",
                        abandoned,
                    )
                    self._db.execute(
                        f"UPDATE entries SET active = 0 WHERE "
                        f"substr(span_id, 1, instr(span_id || '.', '.') - 1) "
                        f"IN ({placeholders})",
                        abandoned,
                    )
                    self._db.execute(
                        f"UPDATE tool_calls SET active = 0 WHERE message_id IN ({placeholders})",
                        abandoned,
                    )

        next_order_row = self._db.execute(
            "SELECT COALESCE(MAX(ledger_order), -1) + 1 AS next_order "
            "FROM messages WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()
        next_order = int(next_order_row["next_order"])
        for index in range(len(self._snapshots), len(messages)):
            message_id = f"m{next_order}"
            message = messages[index]
            raw = serialized[index]
            self._ingest_message(message_id, next_order, message, raw, tools)
            # Sensitive scanning may replace the caller's content. Persist and
            # track only that redacted form; the raw credential survives solely
            # as content_sha on its purged entry.
            current = _canonical(message)
            if current != raw:
                self._db.execute(
                    "UPDATE messages SET message_json = ?, content_sha = ? "
                    "WHERE message_id = ?",
                    (current, _sha(current), message_id),
                )
            self._active_ids.append(message_id)
            self._snapshots.append(current)
            next_order += 1
        if (
            self._turn_user_id is None
            and len(messages) > self._turn_start_length
            and messages[self._turn_start_length].get("role") == "user"
        ):
            self._turn_user_id = self._active_ids[self._turn_start_length]
            self._db.execute(
                "UPDATE notices SET message_id = ? WHERE emitted_turn = ? "
                "AND message_id IS NULL",
                (self._turn_user_id, self.turn),
            )
        self._db.commit()

    def _restore_purged_messages(self, messages: list[dict]) -> None:
        """Keep a raw SessionLog from resurrecting locally-erased bytes."""
        rows = self._db.execute(
            "SELECT m.message_json, EXISTS ("
            "SELECT 1 FROM entries e JOIN span_state s USING(span_id) "
            "WHERE e.active = 1 AND s.state = 'purged' "
            "AND (e.span_id = m.message_id OR e.span_id GLOB m.message_id || '.*')"
            ") AS has_purge FROM messages m WHERE m.session_id = ? AND m.active = 1 "
            "ORDER BY m.ledger_order",
            (self.session_id,),
        ).fetchall()
        for index, row in enumerate(rows):
            if index >= len(messages):
                break
            if row["has_purge"]:
                messages[index].clear()
                messages[index].update(json.loads(row["message_json"]))

    def _ingest_message(
        self,
        message_id: str,
        order: int,
        message: dict,
        raw: str,
        tools: dict[str, Tool],
    ) -> None:
        role = message["role"]
        self._db.execute(
            "INSERT INTO messages(message_id, session_id, ledger_order, role, "
            "message_json, content_sha, created_turn) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, self.session_id, order, role, raw, _sha(raw), self.turn),
        )
        if role == "tool":
            self._ingest_tool_result(message_id, message, tools)
            return

        content = message.get("content") or ""
        origin = role if role in ("user", "assistant", "system") else "system"
        self._insert_entry(message_id, None, role, origin, content, {})
        if role == "user" and self.turn > 0:
            self._surface_user_reference(message_id, content)
        if role == "assistant":
            for call_index, call in enumerate(message.get("tool_calls") or []):
                function = call["function"]
                args_json = function["arguments"]
                args: dict | None = None
                try:
                    args = json.loads(args_json)
                    canonical_args = _canonical(args)
                except json.JSONDecodeError:
                    canonical_args = args_json
                key = f"{function['name']}:{canonical_args}"
                self._db.execute(
                    "INSERT INTO tool_calls(call_id, message_id, call_index, "
                    "tool_name, args_json, canonical_key) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        call["id"],
                        message_id,
                        call_index,
                        function["name"],
                        args_json,
                        key,
                    ),
                )
                tool = tools.get(function["name"])
                if tool is not None and tool.foldable_inputs and isinstance(args, dict):
                    field = tool.foldable_inputs[0]
                    payload = args.get(field)
                    if isinstance(payload, str):
                        self._insert_entry(
                            f"{message_id}.i{call_index}",
                            message_id,
                            "assistant",
                            "tool_input",
                            payload,
                            {
                                "call_id": call["id"],
                                "tool_name": function["name"],
                                "field": field,
                                "args_json": args_json,
                            },
                        )
                self._detect_refetch(key, call["id"])

    def _ingest_tool_result(
        self,
        message_id: str,
        message: dict,
        tools: dict[str, Tool],
    ) -> None:
        call_id = message["tool_call_id"]
        call = self._db.execute(
            "SELECT tool_name, args_json, canonical_key FROM tool_calls "
            "WHERE call_id = ? AND active = 1 ORDER BY tool_call_id DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        tool_name = call["tool_name"] if call is not None else ""
        tool = tools.get(tool_name)
        content = message.get("content") or ""
        span_id = f"{message_id}.r0"
        meta = {
            "call_id": call_id,
            "tool_name": tool_name,
            "args_json": call["args_json"] if call is not None else "{}",
            "canonical_key": call["canonical_key"] if call is not None else "",
            "refetchable": bool(tool and tool.read_only),
            "untrusted": bool(tool and tool.untrusted_output),
        }
        if self._contains_secret(content):
            self._insert_entry(
                span_id,
                None,
                "tool_result",
                "tool",
                None,
                meta,
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
            self._event("scanner_hit", span=span_id)
            return

        self._insert_entry(span_id, None, "tool_result", "tool", content, meta)
        pieces = _split_span(content, self.config.chunk_tokens)
        if len(pieces) > 1:
            for chunk_index, piece in enumerate(pieces):
                self._insert_entry(
                    f"{span_id}.c{chunk_index}",
                    span_id,
                    "tool_result",
                    "tool",
                    piece,
                    meta,
                )
        if call is not None:
            self._db.execute(
                "UPDATE tool_calls SET result_span = ? WHERE call_id = ? AND active = 1",
                (span_id, call_id),
            )
            self._apply_heuristics(span_id, call, tool)

    def _insert_entry(
        self,
        span_id: str,
        parent_id: str | None,
        role: str,
        origin: str,
        content: str | None,
        meta: dict,
        *,
        content_sha: str | None = None,
        tokens_est: int | None = None,
        state: str = "visible",
    ) -> None:
        stored = content or ""
        self._db.execute(
            "INSERT INTO entries(span_id, parent_id, session_id, role, origin, "
            "content, content_sha, tokens_est, created_turn, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                span_id,
                parent_id,
                self.session_id,
                role,
                origin,
                content,
                content_sha or _sha(stored),
                count_text_tokens(stored) if tokens_est is None else tokens_est,
                self.turn,
                _canonical(meta),
            ),
        )
        self._db.execute(
            "INSERT INTO span_state(span_id, state) VALUES (?, ?)",
            (span_id, state),
        )

    @staticmethod
    def _contains_secret(content: str) -> bool:
        return any(pattern.search(content) for pattern in _SECRET_PATTERNS)

    def _event(self, event: str, **fields: object) -> None:
        if self.decision_log_path is None:
            return
        payload = {"t": self.turn, "seq": self._event_seq, "ev": event, **fields}
        self._event_seq += 1
        try:
            self.decision_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.decision_log_path.open("a") as stream:
                stream.write(_canonical(payload) + "\n")
        except OSError:
            # Observability cannot make a fold transition fail after SQLite has
            # accepted it; replay remains authoritative.
            pass

    def _detect_refetch(self, canonical_key: str, call_id: str) -> None:
        rows = self._db.execute(
            "SELECT result_span FROM tool_calls WHERE canonical_key = ? "
            "AND call_id != ? AND result_span IS NOT NULL AND active = 1 "
            "ORDER BY rowid DESC",
            (canonical_key, call_id),
        ).fetchall()
        for row in rows:
            span_id = row["result_span"]
            if self.state(span_id) not in ("folded", "quarantined"):
                continue
            if self.reason(span_id) == "superseded":
                continue
            self._event("refetch_candidate", span=span_id)
            return

    @staticmethod
    def _result_failed(content: str) -> bool:
        lowered = content.lstrip().lower()
        if lowered.startswith(("error:", "permission denied:", "blocked by hook:")):
            return True
        match = re.match(r"exit code:\s*(-?\d+)", lowered)
        return bool(match and int(match.group(1)) != 0)

    def _apply_heuristics(
        self,
        span_id: str,
        call: sqlite3.Row,
        tool: Tool | None,
    ) -> None:
        content = self.content(span_id) or ""
        if self._result_failed(content):
            return

        # A successful write/delegation makes its registered payload redundant
        # independently of what happens to the result itself.
        payloads = self._db.execute(
            "SELECT span_id, meta_json FROM entries WHERE session_id = ? "
            "AND origin = 'tool_input' AND active = 1 ORDER BY rowid DESC",
            (self.session_id,),
        ).fetchall()
        result_meta = json.loads(self._entry(span_id)["meta_json"])
        for payload in payloads:
            meta = json.loads(payload["meta_json"])
            if meta["call_id"] != result_meta.get("call_id"):
                continue
            if meta.get("tool_name") == "agent":
                reason = "scaffolding"
                note = f"subagent brief consumed; result returned in {span_id}"
            else:
                reason = "superseded"
                try:
                    args = json.loads(meta.get("args_json", "{}"))
                except json.JSONDecodeError:
                    args = {}
                target = args.get("path")
                pointer = f" at {target}" if isinstance(target, str) else ""
                note = (
                    f"payload applied successfully{pointer}; read the canonical "
                    "destination for current content"
                )
            self._auto_fold(payload["span_id"], reason, note)
            break

        current = self._entry(span_id)
        duplicates = self._db.execute(
            "SELECT e.span_id FROM entries e JOIN span_state s USING(span_id) "
            "WHERE e.session_id = ? AND e.origin = 'tool' AND e.parent_id IS NULL "
            "AND e.content_sha = ? AND e.span_id != ? AND s.state = 'visible' "
            "AND e.active = 1 "
            "ORDER BY e.rowid",
            (self.session_id, current["content_sha"], span_id),
        ).fetchall()
        if duplicates:
            self._auto_fold(span_id, "duplicate", f"dup of {duplicates[0]['span_id']}")
            return

        # A successful retry closes only an earlier failure with the exact same
        # canonical operation, never another call that happens to share a name.
        retries = self._db.execute(
            "SELECT result_span FROM tool_calls WHERE canonical_key = ? "
            "AND result_span IS NOT NULL AND result_span != ? AND active = 1 "
            "ORDER BY rowid DESC",
            (call["canonical_key"], span_id),
        ).fetchall()
        for retry in retries:
            prior = retry["result_span"]
            if self.state(prior) == "visible" and self._result_failed(self.content(prior) or ""):
                self._auto_fold(
                    prior,
                    "handled_failure",
                    f"earlier operation failed; successful retry is {span_id}",
                )
                break

        if call["tool_name"] != "read_file":
            return
        try:
            current_args = json.loads(call["args_json"])
        except json.JSONDecodeError:
            return
        path = current_args.get("path")
        if not isinstance(path, str):
            return
        earlier_reads = self._db.execute(
            "SELECT args_json, result_span FROM tool_calls WHERE tool_name = 'read_file' "
            "AND result_span IS NOT NULL AND result_span != ? AND active = 1 "
            "ORDER BY rowid DESC",
            (span_id,),
        ).fetchall()
        for earlier in earlier_reads:
            try:
                earlier_args = json.loads(earlier["args_json"])
            except json.JSONDecodeError:
                continue
            prior = earlier["result_span"]
            if earlier_args.get("path") == path and self.state(prior) == "visible":
                self._auto_fold(
                    prior,
                    "superseded",
                    f"later read {span_id} replaced this snapshot; successor: {span_id}",
                )
                break

    def _auto_fold(self, span_id: str, reason: str, note: str) -> bool:
        entry = self._entry(span_id)
        if entry["tokens_est"] < self.config.min_span_tokens:
            return False
        try:
            self.fold(span_id, reason, note, decider="heuristic")
        except FoldError:
            return False
        self._event("heuristic_fired", rule=f"{reason}_v1", span=span_id)
        self._db.execute(
            "INSERT INTO notices(span_id, kind, content, created_turn) "
            "VALUES (?, 'auto', ?, ?)",
            (span_id, f"[auto-folded {span_id} — {reason}: {note}]", self.turn),
        )
        self._db.commit()
        return True

    def span_ids(self) -> list[str]:
        rows = self._db.execute(
            "SELECT span_id FROM entries WHERE session_id = ? AND active = 1 "
            "ORDER BY rowid",
            (self.session_id,),
        ).fetchall()
        return [row["span_id"] for row in rows]

    def child_ids(self, parent_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT span_id FROM entries WHERE parent_id = ? AND active = 1 ORDER BY rowid",
            (parent_id,),
        ).fetchall()
        return [row["span_id"] for row in rows]

    def state(self, span_id: str, turn: int | None = None) -> str:
        if turn is not None:
            permanent = self._db.execute(
                "SELECT reason FROM folds WHERE span_id = ? "
                "AND reason IN ('sensitive', 'user_delete') "
                "ORDER BY fold_id DESC LIMIT 1",
                (span_id,),
            ).fetchone()
            if permanent is not None:
                return "purged"
            active = self._open_fold(span_id, turn)
            if active is None:
                return "visible"
            return "quarantined" if active["reason"] == "poisoned" else "folded"
        row = self._db.execute(
            "SELECT state FROM span_state WHERE span_id = ?",
            (span_id,),
        ).fetchone()
        if row is None:
            raise self._unknown_span(span_id)
        return str(row["state"])

    def content(self, span_id: str) -> str | None:
        row = self._db.execute(
            "SELECT content FROM entries WHERE span_id = ? AND session_id = ?",
            (span_id, self.session_id),
        ).fetchone()
        if row is None:
            raise self._unknown_span(span_id)
        return row["content"]

    def fold_records(self, span_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT reason, unfolded_turn FROM folds WHERE span_id = ? "
            "ORDER BY fold_id",
            (span_id,),
        ).fetchall()
        return [
            {"reason": row["reason"], "unfolded_turn": row["unfolded_turn"]}
            for row in rows
        ]

    def reason(self, span_id: str) -> str:
        return str(self._latest_fold(span_id)["reason"])

    def note(self, span_id: str) -> str:
        return str(self._latest_fold(span_id)["note"])

    def begin_turn(self, messages: list[dict], tools: dict[str, Tool] | None = None) -> None:
        self.sync(messages, tools)
        self._turn_start_length = len(messages)
        self._turn_user_id = None
        self.turn += 1
        self._event_seq = 0
        self._current_notices = []
        self.checkpoint(reason="turn boundary")
        self._queue_pressure_notice()
        rows = self._db.execute(
            "SELECT notice_id, kind, content FROM notices n "
            "WHERE emitted_turn IS NULL AND created_turn < ? "
            "AND (span_id IS NULL OR EXISTS (SELECT 1 FROM entries e "
            "WHERE e.span_id = n.span_id AND e.active = 1)) ORDER BY notice_id",
            (self.turn,),
        ).fetchall()
        # Deferred notices describe decisions made before this boundary, so
        # present them before the checkpoint's newly-computed workspace map.
        self._current_notices[0:0] = [row["content"] for row in rows]
        if rows:
            ids = [row["notice_id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            with self._db:
                self._db.execute(
                    f"UPDATE notices SET emitted_turn = ? WHERE notice_id IN ({placeholders})",
                    [self.turn, *ids],
                )
            for row in rows:
                self._event(
                    "notice_emitted", kind=row["kind"], ref=row["notice_id"]
                )

    def _queue_pressure_notice(self) -> None:
        rows = self._db.execute(
            "SELECT e.span_id, e.tokens_est FROM entries e "
            "JOIN span_state s USING(span_id) WHERE e.session_id = ? "
            "AND e.active = 1 AND e.origin = 'tool' AND e.parent_id IS NULL "
            "AND e.created_turn < ? AND e.tokens_est >= ? AND s.state = 'visible' "
            "ORDER BY e.rowid",
            (self.session_id, self.turn, self.config.min_span_tokens),
        ).fetchall()
        if len(rows) < 3:
            return
        candidates = rows[:3]
        labels = ", ".join(
            f"{row['span_id']} ~{_token_label(row['tokens_est'])} tok"
            for row in candidates
        )
        content = (
            f"[workspace: {len(candidates)} spans look closed ({labels}). "
            "Fold with a verdict when at a pause.]"
        )
        exists = self._db.execute(
            "SELECT 1 FROM notices WHERE kind = 'pressure' AND content = ?",
            (content,),
        ).fetchone()
        if exists is None:
            with self._db:
                self._db.execute(
                    "INSERT INTO notices(kind, content, created_turn) "
                    "VALUES ('pressure', ?, ?)",
                    (content, self.turn - 1),
                )

    @staticmethod
    def _reference_terms(text: str) -> set[str]:
        stop = {
            "about",
            "after",
            "before",
            "earlier",
            "fully",
            "from",
            "have",
            "into",
            "that",
            "there",
            "this",
            "what",
            "when",
            "where",
            "with",
            "would",
        }
        return {
            term
            for term in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", text.lower())
            if term not in stop
        }

    def _surface_user_reference(self, message_id: str, content: str) -> None:
        user_terms = self._reference_terms(content)
        if not user_terms:
            return
        rows = self._db.execute(
            "SELECT f.span_id, f.reason, f.note FROM folds f "
            "JOIN entries e USING(span_id) JOIN span_state s USING(span_id) "
            "WHERE e.session_id = ? AND e.active = 1 AND s.state = 'folded' "
            "AND f.unfolded_turn IS NULL AND f.placement IS NOT NULL "
            "ORDER BY f.fold_id DESC",
            (self.session_id,),
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            span_id = row["span_id"]
            if span_id in seen:
                continue
            seen.add(span_id)
            if len(user_terms & self._reference_terms(row["note"])) < 2:
                continue
            gist = " ".join(row["note"].split())[:120]
            notice = (
                f"[user may be referring to folded {span_id} — "
                f"{gist}, {row['reason']}]"
            )
            exists = self._db.execute(
                "SELECT 1 FROM notices WHERE kind = 'reference' "
                "AND message_id = ? AND span_id = ?",
                (message_id, span_id),
            ).fetchone()
            if exists is not None:
                continue
            self._db.execute(
                "INSERT INTO notices(span_id, message_id, kind, content, "
                "created_turn, emitted_turn) VALUES (?, ?, 'reference', ?, ?, ?)",
                (span_id, message_id, notice, self.turn, self.turn),
            )
            self._current_notices.append(notice)
            self._event("notice_emitted", kind="reference", ref=span_id)

    def turn_notice(self) -> str:
        return "\n".join(self._current_notices)

    def should_checkpoint(self, projected_tokens: int) -> bool:
        row = self._db.execute(
            "SELECT COALESCE(SUM(e.tokens_est), 0) AS marked FROM folds f "
            "JOIN entries e USING(span_id) WHERE f.unfolded_turn IS NULL "
            "AND f.placement IS NULL AND e.active = 1"
        ).fetchone()
        marked = int(row["marked"])
        return marked > 0 and marked / max(projected_tokens, 1) >= self.config.checkpoint_ratio

    def shadow_messages(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT message_json FROM messages WHERE session_id = ? "
            "AND active = 1 ORDER BY ledger_order",
            (self.session_id,),
        ).fetchall()
        return [json.loads(row["message_json"]) for row in rows]

    def pin(self, span_id: str) -> str:
        entry = self._db.execute(
            "SELECT 1 FROM entries WHERE span_id = ? AND session_id = ? AND active = 1",
            (span_id, self.session_id),
        ).fetchone()
        if entry is None:
            raise self._unknown_span(span_id)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO pins(span_id, pinned_turn) VALUES (?, ?)",
                (span_id, self.turn),
            )
        self._event("user_pin", span=span_id, decider="user")
        return f"pinned {span_id}; automatic and agent folds are disabled"

    def delete(self, span_id: str) -> str:
        entry = self._db.execute(
            "SELECT * FROM entries WHERE span_id = ? AND session_id = ? AND active = 1",
            (span_id, self.session_id),
        ).fetchone()
        if entry is None:
            raise self._unknown_span(span_id)

        # A chunk is only an index into one stored result. Erasing it safely
        # therefore erases the owning result instead of retaining the bytes in
        # its parent entry or message snapshot.
        target = (
            entry["parent_id"]
            if entry["origin"] == "tool" and entry["parent_id"] is not None
            else span_id
        )
        entry = self._entry(target)
        targets = [target, *self.child_ids(target)]
        erased = [
            row["content"]
            for candidate in targets
            if (row := self._entry(candidate))["content"] is not None
        ]
        self._purge_session_log(erased)
        with self._db:
            for candidate in targets:
                self._db.execute(
                    "UPDATE entries SET content = NULL WHERE span_id = ?",
                    (candidate,),
                )
                self._db.execute(
                    "UPDATE span_state SET state = 'purged' WHERE span_id = ?",
                    (candidate,),
                )
                self._db.execute(
                    "INSERT INTO folds(span_id, reason, note, decider, folded_turn, "
                    "placement, applied_turn) VALUES (?, 'user_delete', "
                    "'deleted by user', 'user', ?, 'in_place', ?)",
                    (candidate, self.turn, self.turn),
                )
            self._rewrite_message_for_purge(target, "[deleted by user]")
        self._event("user_delete", span=target, decider="user")
        return f"deleted {target}; content is no longer recoverable"

    def _purge_session_log(self, erased: list[str]) -> None:
        path = self.session_log_path
        if path is None or not path.exists() or not erased:
            return

        def scrub(value: object) -> object:
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if not isinstance(value, str):
                return value
            if value in erased:
                return "[deleted by user]"
            cleaned = value
            for content in erased:
                if len(content) >= 20:
                    cleaned = cleaned.replace(content, "[deleted by user]")
            return cleaned

        try:
            lines = path.read_text().splitlines()
            rendered: list[str] = []
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    rendered.append(line)
                    continue
                rendered.append(json.dumps(scrub(event), ensure_ascii=False))
            temporary = path.with_suffix(path.suffix + ".purge.tmp")
            temporary.write_text("\n".join(rendered) + ("\n" if rendered else ""))
            os.replace(temporary, path)
        except OSError as error:
            raise FoldError(f"could not purge external session log: {error}") from error

    def _rewrite_message_for_purge(self, span_id: str, marker: str) -> None:
        message_id = span_id.split(".", 1)[0]
        row = self._db.execute(
            "SELECT message_json FROM messages WHERE message_id = ? AND active = 1",
            (message_id,),
        ).fetchone()
        if row is None:
            return
        message = json.loads(row["message_json"])
        entry = self._entry(span_id)
        if entry["origin"] == "tool_input":
            meta = json.loads(entry["meta_json"])
            for call in message.get("tool_calls") or []:
                if call.get("id") != meta.get("call_id"):
                    continue
                try:
                    arguments = json.loads(call["function"]["arguments"])
                except json.JSONDecodeError:
                    break
                arguments[meta["field"]] = marker
                call["function"]["arguments"] = _canonical(arguments)
                break
        else:
            message["content"] = marker
        raw = _canonical(message)
        self._db.execute(
            "UPDATE messages SET message_json = ?, content_sha = ? WHERE message_id = ?",
            (raw, _sha(raw), message_id),
        )
        if message_id in self._active_ids:
            index = self._active_ids.index(message_id)
            self._snapshots[index] = raw
            if self._shadow_ref is not None and index < len(self._shadow_ref):
                self._shadow_ref[index].clear()
                self._shadow_ref[index].update(deepcopy(message))

    def fold(
        self,
        span_id: str,
        reason: str,
        note: str,
        decider: str = "agent",
    ) -> str:
        self._event("fold_requested", span=span_id, decider=decider)
        try:
            entry = self._validate_fold(span_id, reason, note, decider)
        except FoldError as error:
            message = str(error)
            if "note" in message:
                if "generic" in message:
                    gate = "generic"
                elif "instruction-shaped" in message:
                    gate = "instruction"
                else:
                    gate = "length"
                self._event(
                    "note_rejected",
                    span=span_id,
                    gate=gate,
                    note_len=len(note),
                    note_hash=_sha(note),
                )
            self._event(
                "fold_rejected",
                span=span_id,
                cause=self._rejection_cause(message),
            )
            raise

        terminal = reason == "poisoned"
        new_state = "quarantined" if terminal else "folded"
        placement = "in_place" if terminal else None
        applied_turn = self.turn if terminal else None
        related_spans = (
            self._assistant_exchange_spans(span_id)
            if terminal and entry["origin"] == "assistant"
            else []
        )
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO folds(span_id, reason, note, decider, folded_turn, "
                "placement, applied_turn) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    span_id,
                    reason,
                    note,
                    decider,
                    self.turn,
                    placement,
                    applied_turn,
                ),
            )
            self._db.execute(
                "UPDATE span_state SET state = ? WHERE span_id = ?",
                (new_state, span_id),
            )
            for related_span in related_spans:
                related_state = self._db.execute(
                    "SELECT state FROM span_state WHERE span_id = ?",
                    (related_span,),
                ).fetchone()
                if related_state is None or related_state["state"] == "purged":
                    continue
                self._db.execute(
                    "UPDATE folds SET unfolded_turn = ?, "
                    "placement = COALESCE(placement, 'in_place') "
                    "WHERE span_id = ? AND unfolded_turn IS NULL",
                    (self.turn, related_span),
                )
                self._db.execute(
                    "INSERT INTO folds(span_id, reason, note, decider, folded_turn, "
                    "placement, applied_turn) VALUES (?, 'poisoned', ?, ?, ?, "
                    "'in_place', ?)",
                    (related_span, note, decider, self.turn, self.turn),
                )
                self._db.execute(
                    "UPDATE span_state SET state = 'quarantined' WHERE span_id = ?",
                    (related_span,),
                )
        self._event(
            "fold_marked",
            span=span_id,
            reason=reason,
            decider=decider,
            note_len=len(note),
            note_hash=_sha(note),
            record=cursor.lastrowid,
        )
        if terminal:
            return f"quarantined {span_id}; corrective marker applied immediately"
        return f"marked {span_id} as {reason}; applies at the next checkpoint"

    def _assistant_exchange_spans(self, message_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT span_id FROM entries WHERE session_id = ? AND active = 1 "
            "AND parent_id = ? ORDER BY rowid",
            (self.session_id, message_id),
        ).fetchall()
        spans = [row["span_id"] for row in rows]
        results = self._db.execute(
            "SELECT result_span FROM tool_calls WHERE message_id = ? AND active = 1 "
            "AND result_span IS NOT NULL ORDER BY rowid",
            (message_id,),
        ).fetchall()
        for result in results:
            result_span = result["result_span"]
            spans.append(result_span)
            spans.extend(self.child_ids(result_span))
        return list(dict.fromkeys(spans))

    def _validate_fold(
        self,
        span_id: str,
        reason: str,
        note: str,
        decider: str,
    ) -> sqlite3.Row:
        entry = self._db.execute(
            "SELECT * FROM entries WHERE span_id = ? AND session_id = ? AND active = 1",
            (span_id, self.session_id),
        ).fetchone()
        if entry is None:
            raise self._unknown_span(span_id)
        pinned = self._db.execute(
            "SELECT 1 FROM pins WHERE span_id = ?",
            (span_id,),
        ).fetchone()
        if pinned is not None:
            raise FoldError(f"{span_id} is pinned and cannot be folded")
        if reason not in AGENT_REASONS and not (
            decider in ("heuristic", "scanner", "user") and reason == "sensitive"
        ):
            raise FoldError(f"invalid fold reason {reason!r}")
        if decider == "agent":
            self._validate_note(note)
        elif not note:
            raise FoldError("fold note must not be empty")

        origin = entry["origin"]
        if origin in ("user", "system"):
            raise FoldError(f"{span_id} is protected ({origin} content)")
        if origin == "assistant" and reason != "poisoned":
            raise FoldError(
                f"{span_id} is protected; assistant turns allow only whole-turn poisoned folds"
            )
        current = self.state(span_id)
        if current != "visible":
            existing = self._db.execute(
                "SELECT fold_id, reason FROM folds WHERE span_id = ? AND "
                "unfolded_turn IS NULL ORDER BY fold_id DESC LIMIT 1",
                (span_id,),
            ).fetchone()
            detail = (
                f" (record #{existing['fold_id']}, reason: {existing['reason']})"
                if existing is not None
                else ""
            )
            raise FoldError(f"{span_id} is already {current}{detail}")

        parent_id = entry["parent_id"]
        while parent_id is not None:
            if self.state(parent_id) != "visible":
                raise FoldError(f"overlap with {parent_id}; unfold it first")
            parent = self._entry(parent_id)
            parent_id = parent["parent_id"]
        child = self._db.execute(
            "SELECT e.span_id FROM entries e JOIN span_state s USING(span_id) "
            "WHERE e.parent_id = ? AND e.active = 1 AND s.state != 'visible' "
            "ORDER BY e.rowid LIMIT 1",
            (span_id,),
        ).fetchone()
        if child is not None:
            raise FoldError(
                f"overlap with {child['span_id']}; fold remaining chunks or unfold it first"
            )

        if reason != "poisoned" and entry["tokens_est"] < self.config.min_span_tokens:
            raise FoldError(
                f"{span_id} is below the {self.config.min_span_tokens}-token minimum"
            )
        return entry

    @staticmethod
    def _validate_note(note: str) -> None:
        stripped = note.strip()
        if _GENERIC_NOTE.fullmatch(stripped):
            raise FoldError(
                "fold note is too generic; state what the evidence established"
            )
        if len(stripped) < 20 or len(stripped) > 1_500:
            raise FoldError("fold note must contain 20–1500 characters")
        if _INSTRUCTION_NOTE.search(stripped) or _IMPERATIVE_NOTE.search(stripped):
            raise FoldError("fold note is instruction-shaped; use declarative claims")

    def _unknown_span(self, span_id: str) -> FoldError:
        ids = self.span_ids()
        prefix = [candidate for candidate in ids if candidate.startswith(f"{span_id}.")]
        matches = prefix[:1] or get_close_matches(span_id, ids, n=1, cutoff=0.25)
        suggestion = f"; did you mean {matches[0]}?" if matches else ""
        return FoldError(f"unknown span {span_id}{suggestion}")

    @staticmethod
    def _rejection_cause(message: str) -> str:
        for cause in (
            "protected",
            "pinned",
            "overlap",
            "unknown",
            "already",
            "note",
            "minimum",
        ):
            if cause in message:
                if cause == "pinned":
                    return "protected"
                return "illegal_state" if cause == "already" else cause
        return "invalid"

    def checkpoint(self, turn: int | None = None, reason: str = "explicit") -> int:
        if turn is not None:
            self.turn = turn
        projection_hash: str | None = None
        parent_hash: str | None = None
        with self._db:
            cursor = self._db.execute(
                "UPDATE folds SET placement = 'in_place', applied_turn = ? "
                "WHERE unfolded_turn IS NULL AND placement IS NULL "
                "AND span_id IN (SELECT span_id FROM entries WHERE active = 1)",
                (self.turn,),
            )
            count = cursor.rowcount
            if count:
                projection = self.reconstruct()
                projection_hash = _sha(_canonical(projection))
                parent = self._db.execute(
                    "SELECT projection_hash FROM projections "
                    "ORDER BY projection_id DESC LIMIT 1"
                ).fetchone()
                parent_hash = parent["projection_hash"] if parent is not None else None
                self._db.execute(
                    "INSERT INTO projections(projection_hash, parent_hash, turn, tokens_est) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        projection_hash,
                        parent_hash,
                        self.turn,
                        estimate_tokens(projection),
                    ),
                )
        if count:
            open_row = self._db.execute(
                "SELECT COUNT(*) AS spans, COALESCE(SUM(e.tokens_est), 0) AS tokens "
                "FROM entries e JOIN span_state s USING(span_id) "
                "WHERE e.session_id = ? AND e.active = 1 AND e.origin = 'tool' "
                "AND e.parent_id IS NULL AND s.state = 'visible'",
                (self.session_id,),
            ).fetchone()
            folded_row = self._db.execute(
                "SELECT COUNT(*) AS spans FROM entries e JOIN span_state s USING(span_id) "
                "WHERE e.session_id = ? AND e.active = 1 AND e.origin = 'tool' "
                "AND e.parent_id IS NULL AND s.state != 'visible'",
                (self.session_id,),
            ).fetchone()
            workspace_notice = (
                f"[workspace after checkpoint: {open_row['spans']} open spans, "
                f"~{_token_label(open_row['tokens'])} tok; "
                f"{folded_row['spans']} folded]"
            )
            self._current_notices.append(workspace_notice)
            with self._db:
                self._db.execute(
                    "INSERT INTO notices(message_id, kind, content, created_turn, "
                    "emitted_turn) VALUES (?, 'workspace', ?, ?, ?)",
                    (self._turn_user_id, workspace_notice, self.turn, self.turn),
                )
            self._event(
                "checkpoint_rebuild",
                reason=reason,
                folds=count,
                projection_hash=projection_hash,
                parent_hash=parent_hash,
            )
        return count

    def projection_chain(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT projection_hash, parent_hash, turn, tokens_est "
            "FROM projections ORDER BY projection_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def unfold(self, span_id: str, decider: str = "agent") -> str:
        entry = self._db.execute(
            "SELECT * FROM entries WHERE span_id = ? AND session_id = ? AND active = 1",
            (span_id, self.session_id),
        ).fetchone()
        if entry is None:
            raise self._unknown_span(span_id)
        current = self.state(span_id)
        if current == "quarantined":
            raise FoldError(f"{span_id} is quarantined and cannot be unfolded")
        if current == "purged":
            raise FoldError(f"{span_id} was purged and cannot be unfolded")
        if current != "folded":
            raise FoldError(f"{span_id} is {current}; only folded spans can be unfolded")
        record = self._db.execute(
            "SELECT fold_id FROM folds WHERE span_id = ? AND unfolded_turn IS NULL "
            "ORDER BY fold_id DESC LIMIT 1",
            (span_id,),
        ).fetchone()
        if record is None:
            raise FoldError(f"{span_id} has no open fold record")
        with self._db:
            self._db.execute(
                "UPDATE folds SET unfolded_turn = ?, placement = 'tail' WHERE fold_id = ?",
                (self.turn, record["fold_id"]),
            )
            self._db.execute(
                "UPDATE span_state SET state = 'visible' WHERE span_id = ?",
                (span_id,),
            )
        self._event(
            "user_unfold" if decider == "user" else "unfold",
            span=span_id,
            record=record["fold_id"],
            decider=decider,
        )
        return f"reinstated {span_id} in full at the context tail"

    def project(
        self,
        messages: list[dict],
        turn: int | None = None,
    ) -> list[dict]:
        self.sync(messages)
        return self._project_with_ids(messages, self._active_ids, turn)

    def _project_with_ids(
        self,
        messages: list[dict],
        message_ids: list[str],
        turn: int | None,
    ) -> list[dict]:
        projected: list[dict] = []
        removed_call_ids: set[str] = set()
        for original, message_id in zip(messages, message_ids):
            message = deepcopy(original)
            role = message["role"]
            if role == "assistant" and self.state(message_id, turn) == "quarantined":
                fold = self._latest_fold(message_id, turn)
                removed_call_ids.update(
                    call["id"] for call in message.get("tool_calls") or []
                )
                message = {
                    "role": "assistant",
                    "content": self._marker(message_id, fold),
                }
                projected.append(message)
                continue
            if role == "assistant":
                self._project_input_payloads(message, message_id, turn)
            notice = ""
            if turn is not None and role == "user":
                historical_notices = self._db.execute(
                    "SELECT content FROM notices WHERE message_id = ? "
                    "AND emitted_turn <= ? ORDER BY notice_id",
                    (message_id, turn),
                ).fetchall()
                notice = "\n".join(row["content"] for row in historical_notices)
            elif (
                turn is None
                and role == "user"
                and message_id == self._turn_user_id
                and self._current_notices
            ):
                notice = self.turn_notice()
            if notice:
                message["content"] = f"{notice}\n\n{message.get('content') or ''}"
            if role == "tool":
                if message.get("tool_call_id") in removed_call_ids:
                    continue
                message["content"] = self._render_result(f"{message_id}.r0", turn)
            projected.append(message)

        for span_id in self._tail_spans(turn):
            entry = self._entry(span_id)
            meta = json.loads(entry["meta_json"])
            stale = (
                " — source may have changed since; re-read if freshness matters"
                if meta.get("refetchable")
                else ""
            )
            fold = self._latest_fold(span_id, turn)
            projected.append(
                {
                    "role": "user",
                    "content": (
                        f"[unfolded {span_id}, originally from turn "
                        f"{fold['folded_turn']}{stale}]\n{entry['content']}"
                    ),
                }
            )
        try:
            self._lint(projected)
        except ProjectionError:
            if turn is None:
                self._event("linter_fail")
            raise
        if turn is None:
            self._event("linter_pass")
        return projected

    def _project_input_payloads(
        self, message: dict, message_id: str, turn: int | None
    ) -> None:
        for call_index, call in enumerate(message.get("tool_calls") or []):
            span_id = f"{message_id}.i{call_index}"
            row = self._db.execute(
                "SELECT 1 FROM entries WHERE span_id = ? AND session_id = ? AND active = 1",
                (span_id, self.session_id),
            ).fetchone()
            if row is None:
                continue
            entry = self._entry(span_id)
            meta = json.loads(entry["meta_json"])
            try:
                arguments = json.loads(call["function"]["arguments"])
            except json.JSONDecodeError:
                continue
            state = self.state(span_id, turn)
            replacement: str | None = None
            if state == "purged":
                replacement = self._purge_marker(span_id, turn)
            elif state == "quarantined":
                replacement = self._marker(span_id, self._latest_fold(span_id, turn))
            elif self._is_tail_reinstated(span_id, turn):
                replacement = (
                    f"[unfolded {span_id} → tail, turn "
                    f"{self._latest_fold(span_id, turn)['unfolded_turn']}]"
                )
            elif state == "folded":
                fold = self._open_fold(span_id, turn)
                if fold is not None and fold["placement"] is not None:
                    replacement = self._marker(span_id, fold)
            if replacement is not None:
                arguments[meta["field"]] = replacement
                call["function"]["arguments"] = _canonical(arguments)

    def _render_result(self, span_id: str, turn: int | None = None) -> str:
        entry = self._entry(span_id)
        state = self.state(span_id, turn)
        if state == "purged":
            return self._purge_marker(span_id, turn)
        if state == "quarantined":
            return self._marker(span_id, self._latest_fold(span_id, turn))
        if self._is_tail_reinstated(span_id, turn):
            return (
                f"[unfolded {span_id} → tail, turn "
                f"{self._latest_fold(span_id, turn)['unfolded_turn']}]"
            )
        if state == "folded":
            open_fold = self._open_fold(span_id, turn)
            if open_fold is not None and open_fold["placement"] is not None:
                return self._marker(span_id, open_fold)

        children = self.child_ids(span_id)
        header = f"[{span_id} · ~{_token_label(entry['tokens_est'])} tok]"
        if not children:
            return f"{header}\n{entry['content']}"
        rendered = [header]
        for child_id in children:
            child = self._entry(child_id)
            child_state = self.state(child_id, turn)
            if child_state == "purged":
                body = self._purge_marker(child_id, turn)
            elif child_state == "quarantined":
                body = self._marker(child_id, self._latest_fold(child_id, turn))
            elif self._is_tail_reinstated(child_id, turn):
                body = (
                    f"[unfolded {child_id} → tail, turn "
                    f"{self._latest_fold(child_id, turn)['unfolded_turn']}]"
                )
            elif child_state == "folded":
                child_fold = self._open_fold(child_id, turn)
                body = (
                    self._marker(child_id, child_fold)
                    if child_fold is not None and child_fold["placement"] is not None
                    else f"[{child_id} · ~{_token_label(child['tokens_est'])} tok]\n"
                    f"{child['content']}"
                )
            else:
                body = (
                    f"[{child_id} · ~{_token_label(child['tokens_est'])} tok]\n"
                    f"{child['content']}"
                )
            rendered.append(body)
        return "\n".join(rendered)

    def _marker(self, span_id: str, fold: sqlite3.Row) -> str:
        reason = fold["reason"]
        note = fold["note"].replace('"', '\\"')
        if reason == "sensitive":
            return _REDACTION_MARKER
        if reason == "duplicate" and note.startswith("dup of "):
            return f"[{note}]"
        if reason == "poisoned":
            return f'[removed {span_id} — poisoned: "{note}"]'
        entry = self._entry(span_id)
        meta = json.loads(entry["meta_json"])
        provenance = (
            "provenance: untrusted tool output; " if meta.get("untrusted") else ""
        )
        return (
            f'[folded {span_id}, ~{_token_label(entry["tokens_est"])} tok — '
            f'{provenance}{reason}: "{note}" unfold available]'
        )

    def _purge_marker(self, span_id: str, turn: int | None = None) -> str:
        deleted = self._db.execute(
            "SELECT 1 FROM folds WHERE span_id = ? AND reason = 'user_delete' LIMIT 1",
            (span_id,),
        ).fetchone()
        return "[deleted by user]" if deleted is not None else _REDACTION_MARKER

    def _latest_fold(self, span_id: str, turn: int | None = None) -> sqlite3.Row:
        if turn is None:
            row = self._db.execute(
                "SELECT * FROM folds WHERE span_id = ? ORDER BY fold_id DESC LIMIT 1",
                (span_id,),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM folds WHERE span_id = ? AND folded_turn <= ? "
                "ORDER BY fold_id DESC LIMIT 1",
                (span_id, turn),
            ).fetchone()
        if row is None:
            raise ProjectionError(f"{span_id} has folded state without a fold record")
        return row

    def _open_fold(self, span_id: str, turn: int | None = None) -> sqlite3.Row | None:
        if turn is None:
            return self._db.execute(
                "SELECT * FROM folds WHERE span_id = ? AND unfolded_turn IS NULL "
                "ORDER BY fold_id DESC LIMIT 1",
                (span_id,),
            ).fetchone()
        return self._db.execute(
            "SELECT * FROM folds WHERE span_id = ? AND applied_turn IS NOT NULL "
            "AND applied_turn <= ? AND (unfolded_turn IS NULL OR unfolded_turn > ?) "
            "ORDER BY fold_id DESC LIMIT 1",
            (span_id, turn, turn),
        ).fetchone()

    def _is_tail_reinstated(self, span_id: str, turn: int | None = None) -> bool:
        if turn is None:
            open_fold = self._open_fold(span_id)
            if open_fold is not None and open_fold["placement"] is not None:
                return False
            tail = self._db.execute(
                "SELECT 1 FROM folds WHERE span_id = ? AND placement = 'tail' "
                "ORDER BY fold_id DESC LIMIT 1",
                (span_id,),
            ).fetchone()
        else:
            if self._open_fold(span_id, turn) is not None:
                return False
            tail = self._db.execute(
                "SELECT 1 FROM folds WHERE span_id = ? AND placement = 'tail' "
                "AND unfolded_turn <= ? ORDER BY fold_id DESC LIMIT 1",
                (span_id, turn),
            ).fetchone()
        return tail is not None

    def _tail_spans(self, turn: int | None = None) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT f.span_id FROM folds f JOIN entries e USING(span_id) "
            "WHERE f.placement = 'tail' AND e.active = 1 "
            "ORDER BY fold_id"
        ).fetchall()
        return [
            row["span_id"]
            for row in rows
            if self._is_tail_reinstated(row["span_id"], turn)
        ]

    def _entry(self, span_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM entries WHERE span_id = ? AND session_id = ?",
            (span_id, self.session_id),
        ).fetchone()
        if row is None:
            raise ProjectionError(f"ledger entry {span_id} is missing")
        return row

    @staticmethod
    def _lint(messages: list[dict]) -> None:
        pending: dict[str, int] = {}
        seen_results: set[str] = set()
        for index, message in enumerate(messages):
            role = message["role"]
            calls = message.get("tool_calls") or []
            if role == "tool":
                call_id = message.get("tool_call_id")
                if call_id not in pending:
                    raise ProjectionError(
                        f"orphaned tool result {call_id!r} at message {index}"
                    )
                if call_id in seen_results:
                    raise ProjectionError(f"tool call {call_id!r} has multiple results")
                seen_results.add(call_id)
                pending.pop(call_id)
                continue
            if pending:
                missing = ", ".join(sorted(pending))
                raise ProjectionError(f"tool call {missing} has no result")
            for call in calls:
                call_id = call["id"]
                if call_id in pending or call_id in seen_results:
                    raise ProjectionError(f"duplicate tool call id {call_id!r}")
                pending[call_id] = index
        if pending:
            missing = ", ".join(sorted(pending))
            raise ProjectionError(f"tool call {missing} has no result")

    def reconstruct(self, turn: int | None = None) -> list[dict]:
        query = (
            "SELECT message_id, message_json FROM messages "
            "WHERE session_id = ? AND active = 1"
        )
        params: list[object] = [self.session_id]
        if turn is not None:
            query += " AND created_turn <= ?"
            params.append(turn)
        query += " ORDER BY ledger_order"
        rows = self._db.execute(query, params).fetchall()
        messages = [json.loads(row["message_json"]) for row in rows]
        ids = [row["message_id"] for row in rows]
        return self._project_with_ids(messages, ids, turn)

    def projection_hash(
        self,
        messages: list[dict],
        turn: int | None = None,
    ) -> str:
        return _sha(_canonical(self.project(messages, turn)))
