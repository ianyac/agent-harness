"""Persistent, deterministic projection of a foldable conversation ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

from harness.compaction import count_text_tokens
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
    meta_json     TEXT NOT NULL DEFAULT '{}'
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
    UNIQUE(session_id, ledger_order)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id       TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL REFERENCES messages(message_id),
    call_index    INTEGER NOT NULL,
    tool_name     TEXT NOT NULL,
    args_json     TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    result_span   TEXT
);

CREATE TABLE IF NOT EXISTS projections (
    projection_hash  TEXT PRIMARY KEY,
    parent_hash      TEXT,
    turn             INTEGER NOT NULL,
    tokens_est       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pins (
    span_id      TEXT PRIMARY KEY REFERENCES entries(span_id),
    pinned_turn  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
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
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.decision_log_path = (
            Path(decision_log_path) if decision_log_path is not None else None
        )
        self.config = config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._active_ids: list[str] = []
        self._snapshots: list[str] = []
        self._event_seq = 0
        row = self._db.execute(
            "SELECT COALESCE(MAX(created_turn), 0) AS turn FROM messages "
            "WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()
        self.turn = int(row["turn"])

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
        serialized = [_canonical(message) for message in messages]
        common = 0
        for old, new in zip(self._snapshots, serialized):
            if old != new:
                break
            common += 1
        if common < len(self._snapshots):
            self._active_ids = self._active_ids[:common]
            self._snapshots = self._snapshots[:common]

        if not self._snapshots and serialized:
            persisted = self._db.execute(
                "SELECT message_id, message_json FROM messages "
                "WHERE session_id = ? ORDER BY ledger_order",
                (self.session_id,),
            ).fetchall()
            for row, current in zip(persisted, serialized):
                if row["message_json"] != current:
                    break
                self._active_ids.append(row["message_id"])
                self._snapshots.append(current)

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
        self._db.commit()

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
        if role == "assistant":
            for call_index, call in enumerate(message.get("tool_calls") or []):
                function = call["function"]
                args_json = function["arguments"]
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

    def _ingest_tool_result(
        self,
        message_id: str,
        message: dict,
        tools: dict[str, Tool],
    ) -> None:
        call_id = message["tool_call_id"]
        call = self._db.execute(
            "SELECT tool_name, args_json, canonical_key FROM tool_calls WHERE call_id = ?",
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
                "UPDATE tool_calls SET result_span = ? WHERE call_id = ?",
                (span_id, call_id),
            )

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

    def span_ids(self) -> list[str]:
        rows = self._db.execute(
            "SELECT span_id FROM entries WHERE session_id = ? "
            "ORDER BY rowid",
            (self.session_id,),
        ).fetchall()
        return [row["span_id"] for row in rows]

    def child_ids(self, parent_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT span_id FROM entries WHERE parent_id = ? ORDER BY rowid",
            (parent_id,),
        ).fetchall()
        return [row["span_id"] for row in rows]

    def state(self, span_id: str) -> str:
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

    def shadow_messages(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT message_json FROM messages WHERE session_id = ? "
            "ORDER BY ledger_order",
            (self.session_id,),
        ).fetchall()
        return [json.loads(row["message_json"]) for row in rows]

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
            self._event(
                "fold_rejected",
                span=span_id,
                cause=self._rejection_cause(str(error)),
            )
            raise

        terminal = reason == "poisoned"
        new_state = "quarantined" if terminal else "folded"
        placement = "in_place" if terminal else None
        applied_turn = self.turn if terminal else None
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

    def _validate_fold(
        self,
        span_id: str,
        reason: str,
        note: str,
        decider: str,
    ) -> sqlite3.Row:
        entry = self._db.execute(
            "SELECT * FROM entries WHERE span_id = ? AND session_id = ?",
            (span_id, self.session_id),
        ).fetchone()
        if entry is None:
            raise self._unknown_span(span_id)
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
            "WHERE e.parent_id = ? AND s.state != 'visible' ORDER BY e.rowid LIMIT 1",
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
        if _INSTRUCTION_NOTE.search(stripped):
            raise FoldError("fold note is instruction-shaped; use declarative claims")

    def _unknown_span(self, span_id: str) -> FoldError:
        ids = self.span_ids()
        prefix = [candidate for candidate in ids if candidate.startswith(f"{span_id}.")]
        matches = prefix[:1] or get_close_matches(span_id, ids, n=1, cutoff=0.25)
        suggestion = f"; did you mean {matches[0]}?" if matches else ""
        return FoldError(f"unknown span {span_id}{suggestion}")

    @staticmethod
    def _rejection_cause(message: str) -> str:
        for cause in ("protected", "overlap", "unknown", "already", "note", "minimum"):
            if cause in message:
                return "illegal_state" if cause == "already" else cause
        return "invalid"

    def checkpoint(self, turn: int | None = None, reason: str = "explicit") -> int:
        if turn is not None:
            self.turn = turn
        with self._db:
            cursor = self._db.execute(
                "UPDATE folds SET placement = 'in_place', applied_turn = ? "
                "WHERE unfolded_turn IS NULL AND placement IS NULL",
                (self.turn,),
            )
        count = cursor.rowcount
        if count:
            self._event("checkpoint_rebuild", reason=reason, folds=count)
        return count

    def unfold(self, span_id: str, decider: str = "agent") -> str:
        entry = self._db.execute(
            "SELECT * FROM entries WHERE span_id = ? AND session_id = ?",
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
            "unfold",
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
            if role == "assistant" and self.state(message_id) == "quarantined":
                fold = self._latest_fold(message_id)
                removed_call_ids.update(
                    call["id"] for call in message.get("tool_calls") or []
                )
                message = {
                    "role": "assistant",
                    "content": self._marker(message_id, fold),
                }
                projected.append(message)
                continue
            if role == "tool":
                if message.get("tool_call_id") in removed_call_ids:
                    continue
                message["content"] = self._render_result(f"{message_id}.r0")
            projected.append(message)

        for span_id in self._tail_spans():
            entry = self._entry(span_id)
            meta = json.loads(entry["meta_json"])
            stale = (
                " — source may have changed since; re-read if freshness matters"
                if meta.get("refetchable")
                else ""
            )
            fold = self._latest_fold(span_id)
            projected.append(
                {
                    "role": "user",
                    "content": (
                        f"[unfolded {span_id}, originally from turn "
                        f"{fold['folded_turn']}{stale}]\n{entry['content']}"
                    ),
                }
            )
        self._lint(projected)
        return projected

    def _render_result(self, span_id: str) -> str:
        entry = self._entry(span_id)
        state = self.state(span_id)
        if state == "purged":
            return _REDACTION_MARKER
        if state == "quarantined":
            return self._marker(span_id, self._latest_fold(span_id))
        if self._is_tail_reinstated(span_id):
            return f"[unfolded {span_id} → tail, turn {self._latest_fold(span_id)['unfolded_turn']}]"
        if state == "folded":
            open_fold = self._open_fold(span_id)
            if open_fold is not None and open_fold["placement"] is not None:
                return self._marker(span_id, open_fold)

        children = self.child_ids(span_id)
        header = f"[{span_id} · ~{_token_label(entry['tokens_est'])} tok]"
        if not children:
            return f"{header}\n{entry['content']}"
        rendered = [header]
        for child_id in children:
            child = self._entry(child_id)
            child_state = self.state(child_id)
            if child_state == "purged":
                body = _REDACTION_MARKER
            elif child_state == "quarantined":
                body = self._marker(child_id, self._latest_fold(child_id))
            elif self._is_tail_reinstated(child_id):
                body = (
                    f"[unfolded {child_id} → tail, turn "
                    f"{self._latest_fold(child_id)['unfolded_turn']}]"
                )
            elif child_state == "folded":
                child_fold = self._open_fold(child_id)
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
        if reason == "poisoned":
            return f'[removed {span_id} — poisoned: "{note}"]'
        entry = self._entry(span_id)
        return (
            f'[folded {span_id}, ~{_token_label(entry["tokens_est"])} tok — '
            f'{reason}: "{note}" unfold available]'
        )

    def _latest_fold(self, span_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM folds WHERE span_id = ? ORDER BY fold_id DESC LIMIT 1",
            (span_id,),
        ).fetchone()
        if row is None:
            raise ProjectionError(f"{span_id} has folded state without a fold record")
        return row

    def _open_fold(self, span_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM folds WHERE span_id = ? AND unfolded_turn IS NULL "
            "ORDER BY fold_id DESC LIMIT 1",
            (span_id,),
        ).fetchone()

    def _is_tail_reinstated(self, span_id: str) -> bool:
        latest = self._latest_fold(span_id) if self.fold_records(span_id) else None
        if latest is None or latest["placement"] != "tail":
            return False
        # A newly-applied refold supersedes the prior tail. A pending refold
        # deliberately keeps the content visible until its checkpoint.
        open_fold = self._open_fold(span_id)
        return open_fold is None or open_fold["placement"] is None

    def _tail_spans(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT span_id FROM folds WHERE placement = 'tail' "
            "ORDER BY fold_id"
        ).fetchall()
        return [row["span_id"] for row in rows if self._is_tail_reinstated(row["span_id"])]

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
            "SELECT message_id, message_json FROM messages WHERE session_id = ?"
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
