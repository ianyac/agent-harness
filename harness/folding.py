"""Persistent, deterministic projection of a foldable conversation ledger."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from contextlib import nullcontext
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
    r"(?:^|[.!?;:]\s+)(?:please\s+)?(?:always\s+|never\s+)?"
    r"(?:answer|call|change|delete|"
    r"download|execute|ignore|open|remove|replace|return|send|upload|"
    r"(?:install|read|run|write)(?!\s+(?:completed|confirmed|failed|found|returned|"
    r"showed|succeeded)\b))\b|\b(?:you|the agent|the assistant)\s+"
    r"(?:must|should|need to)\b|"
    r"[\[\]]|<\|(?:system|assistant|user|tool)",
    re.IGNORECASE | re.MULTILINE,
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"(?P<secret>"
    r"-----BEGIN (?P<private_key_label>"
    r"(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY"
    r")-----"
    r".*?"
    r"-----END (?P=private_key_label)-----"
    r")",
    re.DOTALL,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    _PRIVATE_KEY_PATTERN,
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
        r"[\"']?(?P<secret>[A-Za-z0-9_./+=-]{20,})"
    ),
)
_IDENTIFIER_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
)
_REDACTION_MARKER = "[redacted — credential detected in tool output]"
_SCANNER_ALIAS_PATTERN = re.compile(r"redacted_[a-z2-7]{52}")
_SECRET_SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_VALUE_KEYS = frozenset(
    {"call_id", "id", "name", "tool_call_id", "tool_name"}
)
_SCHEMA_VERSION = 3


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
    scrubbed      INTEGER NOT NULL DEFAULT 0,
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
    kind             TEXT NOT NULL,
    turn             INTEGER NOT NULL,
    tokens_est       INTEGER NOT NULL,
    projection_json  TEXT NOT NULL,
    source_ids_json  TEXT NOT NULL,
    redacted         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_config (
    session_id   TEXT PRIMARY KEY,
    config_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    version  INTEGER NOT NULL
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
        self._db.execute("PRAGMA secure_delete = ON")
        tables = {
            row["name"]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if tables and "schema_meta" not in tables:
            self._db.close()
            raise FoldError(
                "folding ledger schema is incompatible; start a new session "
                "or migrate the ledger explicitly"
            )
        if "scanner_aliases" in tables:
            self._db.close()
            raise FoldError(
                "folding ledger schema is incompatible: schema v3 contains the "
                "undeclared scanner_aliases table; start a new session or "
                "migrate the ledger explicitly"
            )
        if "schema_meta" in tables:
            version = self._db.execute(
                "SELECT version FROM schema_meta LIMIT 1"
            ).fetchone()
            if version is None or version["version"] != _SCHEMA_VERSION:
                self._db.close()
                raise FoldError(
                    "folding ledger schema version is incompatible with this harness"
                )
        self._db.executescript(_SCHEMA)
        if "schema_meta" not in tables:
            self._db.execute(
                "INSERT INTO schema_meta(version) VALUES (?)", (_SCHEMA_VERSION,)
            )
            self._db.commit()
        snapshot = _canonical(
            {
                "harness_version": "0.1.0",
                "schema_version": _SCHEMA_VERSION,
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
        self._last_projection_sources: tuple[str, list[str]] | None = None
        self._shadow_ref: list[dict] | None = None
        self._purge_paths: set[Path] = set()
        self._vacuum_pending = False
        self._sync_in_progress = False
        if self.session_log_path is not None:
            self._purge_paths.add(self.session_log_path)
        if self.decision_log_path is not None:
            self._purge_paths.add(self.decision_log_path)
        self._event_seq = 0
        self._current_notices: list[str] = []
        self._current_notice_ids: list[int | None] = []
        self._turn_start_length = 0
        self._turn_user_id: str | None = None
        self._scanner_identifier_replacements: dict[str, str] = {}
        (
            self._scanner_aliases,
            self._scanner_alias_lengths,
            self._legacy_scanner_tool_names,
            self._reserved_scanner_tool_names,
        ) = self._load_scanner_metadata()
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
        message_snapshot = deepcopy(messages)
        active_ids = self._active_ids.copy()
        snapshots = self._snapshots.copy()
        last_projection_sources = deepcopy(self._last_projection_sources)
        shadow_ref = self._shadow_ref
        vacuum_pending = self._vacuum_pending
        turn_user_id = self._turn_user_id
        current_notices = self._current_notices.copy()
        current_notice_ids = self._current_notice_ids.copy()
        event_seq = self._event_seq
        scanner_identifier_replacements = self._scanner_identifier_replacements.copy()
        scanner_aliases = self._scanner_aliases.copy()
        scanner_alias_lengths = self._scanner_alias_lengths.copy()
        legacy_scanner_tool_names = self._legacy_scanner_tool_names.copy()
        reserved_scanner_tool_names = self._reserved_scanner_tool_names.copy()
        sync_in_progress = self._sync_in_progress
        self._sync_in_progress = True
        try:
            self._sync_pending(messages, tools)
            self._db.commit()
        except BaseException:
            self._db.rollback()
            messages[:] = message_snapshot
            self._active_ids = active_ids
            self._snapshots = snapshots
            self._last_projection_sources = last_projection_sources
            self._shadow_ref = shadow_ref
            self._vacuum_pending = vacuum_pending
            self._turn_user_id = turn_user_id
            self._current_notices = current_notices
            self._current_notice_ids = current_notice_ids
            self._event_seq = event_seq
            self._scanner_identifier_replacements = scanner_identifier_replacements
            self._scanner_aliases = scanner_aliases
            self._scanner_alias_lengths = scanner_alias_lengths
            self._legacy_scanner_tool_names = legacy_scanner_tool_names
            self._reserved_scanner_tool_names = reserved_scanner_tool_names
            self._sync_in_progress = sync_in_progress
            raise
        self._sync_in_progress = sync_in_progress
        if self._vacuum_pending:
            self._db.execute("VACUUM")
            self._vacuum_pending = False

    def _sync_pending(
        self,
        messages: list[dict],
        tools: dict[str, Tool] | None,
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
            abandoned = self._active_ids[common:]
            self._active_ids = self._active_ids[:common]
            self._snapshots = self._snapshots[:common]
            self._deactivate_messages(abandoned)

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
                self._deactivate_messages(abandoned)

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
                    "UPDATE messages SET message_json = ?, content_sha = ?, "
                    "scrubbed = 1 "
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
    def _deactivate_messages(self, message_ids: list[str]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        self._db.execute(
            f"UPDATE messages SET active = 0 WHERE message_id IN ({placeholders})",
            message_ids,
        )
        self._db.execute(
            f"UPDATE entries SET active = 0 WHERE "
            f"substr(span_id, 1, instr(span_id || '.', '.') - 1) "
            f"IN ({placeholders})",
            message_ids,
        )
        self._db.execute(
            f"UPDATE tool_calls SET active = 0 WHERE message_id IN ({placeholders})",
            message_ids,
        )

    def _restore_purged_messages(self, messages: list[dict]) -> None:
        """Keep a raw SessionLog from resurrecting locally-erased bytes."""
        rows = self._db.execute(
            "SELECT m.message_json, m.scrubbed, EXISTS ("
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
            if row["scrubbed"] or row["has_purge"]:
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
            "SELECT tool_call_id, tool_name, args_json, canonical_key FROM tool_calls "
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
        secrets = self._secret_values(content)
        if secrets:
            identifier_replacements = self._sensitive_identifier_replacements(
                secrets,
                tuple(tool.name for tool in tools.values()),
            )
            self._scanner_identifier_replacements.update(identifier_replacements)
            self._scanner_identifier_replacements = dict(
                sorted(
                    self._scanner_identifier_replacements.items(),
                    key=lambda item: len(item[0]),
                    reverse=True,
                )
            )
            self._purge_session_log(
                secrets,
                _REDACTION_MARKER,
                replace_substrings=True,
                exhaustive=True,
                identifier_replacements=identifier_replacements,
            )
            for secret, replacement in identifier_replacements.items():
                secret_sha = _sha(secret)
                self._scanner_aliases[secret_sha] = replacement
                self._scanner_alias_lengths[secret_sha] = len(secret)
            sanitized_meta = dict(
                self._scrub_structured(
                    meta,
                    secrets,
                    _REDACTION_MARKER,
                    replace_substrings=True,
                    exhaustive=True,
                    identifier_replacements=identifier_replacements,
                )
            )
            sanitized_meta["scanner_aliases"] = {
                _sha(secret): replacement
                for secret, replacement in identifier_replacements.items()
            }
            sanitized_meta["scanner_alias_lengths"] = {
                _sha(secret): len(secret) for secret in identifier_replacements
            }
            sanitized_tool_name = sanitized_meta.get("tool_name")
            if (
                isinstance(sanitized_tool_name, str)
                and any(
                    replacement in sanitized_tool_name
                    for replacement in identifier_replacements.values()
                )
            ):
                self._reserved_scanner_tool_names.add(sanitized_tool_name)
            self._scrub_sqlite(
                secrets,
                _REDACTION_MARKER,
                replace_substrings=True,
                exhaustive=True,
                identifier_replacements=identifier_replacements,
            )
            self._redact_sensitive_projections(secrets, identifier_replacements)
            self._scrub_live_shadow(
                secrets,
                _REDACTION_MARKER,
                replace_substrings=True,
                exhaustive=True,
                identifier_replacements=identifier_replacements,
            )
            self._current_notices = [
                str(
                    self._scrub_data(
                        notice,
                        secrets,
                        _REDACTION_MARKER,
                        replace_substrings=True,
                    )
                )
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
                "UPDATE tool_calls SET result_span = ? WHERE tool_call_id = ?",
                (span_id, call["tool_call_id"]),
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
        return bool(FoldingContext._secret_values(content))

    @staticmethod
    def _secret_values(content: str) -> tuple[str, ...]:
        values: list[str] = []
        for pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(content):
                value = match.groupdict().get("secret") or match.group(0)
                if value and value not in values:
                    values.append(value)
        return tuple(sorted(values, key=len, reverse=True))

    @classmethod
    def _identifier_secret_values(cls, identifier: str) -> tuple[str, ...]:
        values = list(cls._secret_values(identifier))
        for pattern in _IDENTIFIER_SECRET_PATTERNS:
            for match in pattern.finditer(identifier):
                value = match.group(0)
                if value not in values:
                    values.append(value)
        return tuple(sorted(values, key=len, reverse=True))

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
        if not self._sync_in_progress:
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
        self._current_notice_ids = []
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
        self._current_notice_ids[0:0] = [int(row["notice_id"]) for row in rows]
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
            cursor = self._db.execute(
                "INSERT INTO notices(span_id, message_id, kind, content, "
                "created_turn, emitted_turn) VALUES (?, ?, 'reference', ?, ?, ?)",
                (span_id, message_id, notice, self.turn, self.turn),
            )
            self._current_notices.append(notice)
            self._current_notice_ids.append(int(cursor.lastrowid))
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

    def register_purge_path(self, path: Path) -> None:
        """Register a local JSONL artifact that may mirror foldable payloads."""
        self._purge_paths.add(Path(path))

    def _load_scanner_metadata(
        self,
    ) -> tuple[dict[str, str], dict[str, int], set[str], set[str]]:
        aliases: dict[str, str] = {}
        alias_lengths: dict[str, int] = {}
        legacy_tool_names: set[str] = set()
        reserved_tool_names: set[str] = set()
        rows = self._db.execute(
            "SELECT e.meta_json FROM entries e JOIN folds f USING(span_id) "
            "WHERE e.session_id = ? AND f.reason = 'sensitive' "
            "ORDER BY f.fold_id",
            (self.session_id,),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["meta_json"])
            except (json.JSONDecodeError, TypeError):
                raise FoldError("stored scanner metadata is invalid; refusing resume")
            if not isinstance(metadata, dict):
                raise FoldError("stored scanner metadata is invalid; refusing resume")
            tool_name = metadata.get("tool_name")
            stored_aliases = metadata.get("scanner_aliases", {})
            if not isinstance(stored_aliases, dict):
                raise FoldError("stored scanner alias metadata is invalid; refusing resume")
            stored_lengths = metadata.get("scanner_alias_lengths", {})
            if not isinstance(stored_lengths, dict):
                raise FoldError("stored scanner alias metadata is invalid; refusing resume")
            for secret_sha, replacement in stored_aliases.items():
                if not (
                    isinstance(secret_sha, str)
                    and _SECRET_SHA_PATTERN.fullmatch(secret_sha)
                    and isinstance(replacement, str)
                    and _SCANNER_ALIAS_PATTERN.fullmatch(replacement)
                ):
                    raise FoldError(
                        "stored scanner alias metadata is invalid; refusing resume"
                    )
                prior = aliases.get(secret_sha)
                if prior is not None and prior != replacement:
                    raise FoldError(
                        "stored scanner alias metadata conflicts; refusing resume"
                    )
                other_owner = next(
                    (
                        owner
                        for owner, alias in aliases.items()
                        if alias == replacement and owner != secret_sha
                    ),
                    None,
                )
                if other_owner is not None:
                    raise FoldError(
                        "stored scanner alias metadata conflicts; refusing resume"
                    )
                aliases[secret_sha] = replacement
            for secret_sha, secret_length in stored_lengths.items():
                if not (
                    isinstance(secret_sha, str)
                    and _SECRET_SHA_PATTERN.fullmatch(secret_sha)
                    and isinstance(secret_length, int)
                    and not isinstance(secret_length, bool)
                    and secret_length >= 20
                ):
                    raise FoldError(
                        "stored scanner alias metadata is invalid; refusing resume"
                    )
                prior_length = alias_lengths.get(secret_sha)
                if prior_length is not None and prior_length != secret_length:
                    raise FoldError(
                        "stored scanner alias metadata conflicts; refusing resume"
                    )
                alias_lengths[secret_sha] = secret_length
            if isinstance(tool_name, str):
                if stored_aliases and any(
                    replacement in tool_name
                    for replacement in stored_aliases.values()
                ):
                    reserved_tool_names.add(tool_name)
                elif not stored_aliases and _SCANNER_ALIAS_PATTERN.search(tool_name):
                    legacy_tool_names.add(tool_name)
        return aliases, alias_lengths, legacy_tool_names, reserved_tool_names

    def _persisted_scanner_secret_substrings(self, tool_name: str) -> set[str]:
        if not self._scanner_aliases:
            return set()
        matches: dict[str, set[str]] = {}
        known_lengths = {
            secret_sha: secret_length
            for secret_sha, secret_length in self._scanner_alias_lengths.items()
            if secret_sha in self._scanner_aliases
        }
        for secret_sha, secret_length in known_lengths.items():
            for start in range(0, len(tool_name) - secret_length + 1):
                candidate = tool_name[start : start + secret_length]
                if _sha(candidate) == secret_sha:
                    matches.setdefault(secret_sha, set()).add(candidate)

        unknown_hashes = set(self._scanner_aliases) - set(known_lengths)
        fallback_maximum_length = 256
        minimum_length = 20
        for start in range(len(tool_name)):
            last = min(len(tool_name), start + fallback_maximum_length)
            for end in range(start + minimum_length, last + 1):
                candidate = tool_name[start:end]
                secret_sha = _sha(candidate)
                if secret_sha in unknown_hashes:
                    matches.setdefault(secret_sha, set()).add(candidate)
        if len(tool_name) > fallback_maximum_length and unknown_hashes - set(matches):
            raise FoldError(
                "tool name exceeds the safe recovery bound for stored scanner "
                "metadata; refusing tool dispatch"
            )
        if any(len(candidates) != 1 for candidates in matches.values()):
            raise FoldError(
                "stored scanner alias matches multiple tool-name substrings; "
                "refusing tool dispatch"
            )
        return {
            next(iter(candidates)) for candidates in matches.values() if candidates
        }

    def _scanner_secret_candidates(self, tool_name: str) -> tuple[str, ...]:
        candidates = set(self._identifier_secret_values(tool_name))
        candidates.update(self._persisted_scanner_secret_substrings(tool_name))
        candidates.update(
            secret
            for secret in self._scanner_identifier_replacements
            if secret in tool_name
        )
        return tuple(sorted(candidates, key=len, reverse=True))

    def _recover_legacy_scanner_aliases(
        self,
        candidates_by_tool: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        missing = {
            secret
            for _tool_name, secrets in candidates_by_tool
            for secret in secrets
            if _sha(secret) not in self._scanner_aliases
        }
        if not missing:
            return
        offered_raw_names = {tool_name for tool_name, _secrets in candidates_by_tool}
        historical_alias_owners: dict[str, set[str]] = {}
        for tool_name in self._legacy_scanner_tool_names:
            for match in _SCANNER_ALIAS_PATTERN.finditer(tool_name):
                historical_alias_owners.setdefault(match.group(0), set()).add(
                    tool_name
                )
        historical_aliases = set(historical_alias_owners)
        unowned_aliases = historical_aliases - set(self._scanner_aliases.values())
        if not unowned_aliases:
            return

        recovered: dict[str, str] = {}
        alias_owners: dict[str, str] = {}
        for secret in sorted(missing):
            matches = {
                candidate
                for nonce in range(10_000)
                if (candidate := self._identifier_alias(secret, nonce))
                in unowned_aliases
            }
            if len(matches) > 1:
                raise FoldError(
                    "cannot safely recover stored scanner alias; refusing tool dispatch"
                )
            if not matches:
                continue
            replacement = matches.pop()
            owner = alias_owners.get(replacement)
            if owner is not None and owner != secret:
                raise FoldError(
                    "cannot safely recover stored scanner alias; refusing tool dispatch"
                )
            recovered[secret] = replacement
            alias_owners[replacement] = secret

        unmatched_aliases = unowned_aliases - set(recovered.values())
        unchanged_ordinary_aliases = {
            alias
            for alias in unmatched_aliases
            if historical_alias_owners[alias] & offered_raw_names
        }
        unresolved_aliases = unmatched_aliases - unchanged_ordinary_aliases
        effective_replacements = {
            secret: self._scanner_aliases.get(_sha(secret), recovered.get(secret, ""))
            for _tool_name, secrets in candidates_by_tool
            for secret in secrets
            if _sha(secret) in self._scanner_aliases or secret in recovered
        }
        effective_replacements = dict(
            sorted(
                effective_replacements.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )
        uncovered_secrets = {
            secret
            for tool_name, secrets in candidates_by_tool
            for secret in secrets
            if secret in missing
            and secret not in recovered
            and secret
            in self._replace_identifiers(tool_name, effective_replacements)
        }
        if unresolved_aliases or (uncovered_secrets and historical_aliases):
            raise FoldError(
                "cannot safely recover every stored scanner alias; "
                "refusing tool dispatch"
            )
        for secret, replacement in recovered.items():
            self._scanner_aliases[_sha(secret)] = replacement
            self._reserved_scanner_tool_names.update(
                tool_name
                for tool_name in self._legacy_scanner_tool_names
                if replacement in tool_name
            )

    def model_tool_names(self, tools: dict[str, Tool]) -> dict[str, str]:
        """Return scanner-safe names for definitions offered to the model."""
        aliases: dict[str, str] = {}
        offered_names: set[str] = set()
        registered_tools = list(tools.values())
        candidates_by_tool = [
            (tool.name, self._scanner_secret_candidates(tool.name))
            for tool in registered_tools
        ]
        self._recover_legacy_scanner_aliases(candidates_by_tool)
        reserved_aliases = {
            replacement: secret_sha
            for secret_sha, replacement in self._scanner_aliases.items()
        }
        for tool, (_tool_name, secret_candidates) in zip(
            registered_tools, candidates_by_tool, strict=True
        ):
            if tool.name in self._reserved_scanner_tool_names:
                raise FoldError(
                    "scanner tool alias conflicts: registered tool name uses a "
                    "reserved scanner alias; "
                    "start a new session with non-conflicting tool names"
                )
            if any(alias in tool.name for alias in reserved_aliases):
                raise FoldError(
                    "scanner tool alias conflicts: registered tool name uses a "
                    "reserved scanner alias; "
                    "start a new session with non-conflicting tool names"
                )
            replacements = dict(self._scanner_identifier_replacements)
            for secret in secret_candidates:
                replacement = self._scanner_aliases.get(_sha(secret))
                if replacement is not None:
                    replacements[secret] = replacement
            replacements = dict(
                sorted(
                    replacements.items(),
                    key=lambda item: len(item[0]),
                    reverse=True,
                )
            )
            cleaned = self._replace_identifiers(tool.name, replacements)
            if cleaned in offered_names:
                raise FoldError(
                    "scanner tool alias conflicts with another registered tool; "
                    "start a new session with non-conflicting tool names"
                )
            offered_names.add(cleaned)
            if cleaned != tool.name:
                aliases[tool.name] = cleaned
        return aliases

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

    def _overlapping_pin(self, span_id: str) -> str | None:
        current: str | None = span_id
        while current is not None:
            pinned = self._db.execute(
                "SELECT 1 FROM pins WHERE span_id = ?", (current,)
            ).fetchone()
            if pinned is not None:
                return current
            entry = self._db.execute(
                "SELECT parent_id FROM entries WHERE span_id = ? AND active = 1",
                (current,),
            ).fetchone()
            current = entry["parent_id"] if entry is not None else None
        descendant = self._db.execute(
            "WITH RECURSIVE descendants(span_id) AS ("
            "SELECT span_id FROM entries WHERE parent_id = ? AND active = 1 "
            "UNION ALL SELECT e.span_id FROM entries e "
            "JOIN descendants d ON e.parent_id = d.span_id WHERE e.active = 1"
            ") SELECT p.span_id FROM pins p JOIN descendants d USING(span_id) LIMIT 1",
            (span_id,),
        ).fetchone()
        return descendant["span_id"] if descendant is not None else None

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
        payload = self._entry(target)["content"]
        if payload is None:
            raise self._unknown_span(target)
        span_ids, _owner_ids = self._user_delete_aliases(target)
        indexed_parents = self._user_delete_indexed_parents(payload) - span_ids
        indexed_span_ids = indexed_parents | self._user_delete_descendants(indexed_parents)
        indexed_target_ids = indexed_parents | self._user_delete_indexed_children(
            indexed_parents, payload
        )
        indexed_owner_ids = {span_id.split(".", 1)[0] for span_id in indexed_parents}
        input_aliases = self._user_delete_input_aliases(span_ids)
        metadata_span_ids = (
            span_ids
            | indexed_span_ids
            | self._user_delete_linked_result_spans(input_aliases)
        )
        root_owner_ids = self._user_delete_root_owners(span_ids)

        # Mounted artifacts do not carry ledger provenance. Exact-value
        # replacement clears independent copies without rewriting unrelated
        # prose that merely contains the same payload.
        self._purge_session_log([payload], replace_substrings=False)
        with self._db:
            self._scrub_delete_entry_metadata(
                metadata_span_ids, input_aliases, payload
            )
            self._scrub_delete_tool_calls(input_aliases, payload)
            projection_operations = self._user_projection_operations(
                span_ids,
                root_owner_ids,
                indexed_owner_ids,
                indexed_span_ids,
                indexed_target_ids,
                input_aliases,
                payload,
            )
            self._add_user_projection_notice_operations(
                projection_operations, metadata_span_ids, [payload]
            )
            self._redact_user_projections(projection_operations)
            notice_updates = self._scrub_delete_folds_and_notices(
                metadata_span_ids, [payload]
            )
            for affected in span_ids:
                self._mark_entry_purged(affected)
            self._replace_indexed_child_aliases(indexed_parents, payload)
            indexed_owner_contents = self._reconcile_indexed_parent_contents(
                indexed_parents
            )
            self._scrub_delete_messages(
                root_owner_ids, indexed_owner_contents, input_aliases, payload
            )
            self._scrub_delete_live_shadow(
                root_owner_ids, indexed_owner_contents, input_aliases, payload
            )
            self._current_notices = [
                notice_updates.get(
                    self._current_notice_ids[index]
                    if index < len(self._current_notice_ids)
                    else None,
                    notice,
                )
                for index, notice in enumerate(self._current_notices)
            ]
        # secure_delete overwrites changed cells; VACUUM also eliminates free
        # pages that could retain a pre-purge copy after variable-size updates.
        self._db.execute("VACUUM")
        self._event("user_delete", span=target, decider="user")
        return f"deleted {target}; content is no longer recoverable"

    @staticmethod
    def _user_projection_operations(
        span_ids: set[str],
        root_owner_ids: set[str],
        indexed_owner_ids: set[str],
        indexed_span_ids: set[str],
        indexed_target_ids: set[str],
        input_aliases: list[dict[str, str]],
        payload: str,
    ) -> dict[str, list[dict[str, object]]]:
        marker = "[deleted by user]"
        operations: dict[str, list[dict[str, object]]] = {}

        def add(source_id: str, operation: dict[str, object]) -> None:
            operations.setdefault(source_id, []).append(operation)

        for owner_id in root_owner_ids:
            add(owner_id, {"kind": "content", "marker": marker})
        for owner_id in indexed_owner_ids:
            add(
                owner_id,
                {
                    "kind": "indexed_content",
                    "marker": marker,
                    "payload": payload,
                    "target_span_ids": sorted(
                        span_id
                        for span_id in indexed_target_ids
                        if span_id.split(".", 1)[0] == owner_id
                    ),
                    "render_span_ids": sorted(
                        span_id
                        for span_id in indexed_span_ids
                        if span_id.split(".", 1)[0] == owner_id
                    ),
                },
            )
        for source_id in indexed_target_ids:
            add(
                source_id,
                {
                    "kind": "indexed_content",
                    "marker": marker,
                    "payload": payload,
                    "target_span_ids": [source_id],
                },
            )
        for span_id in span_ids:
            add(span_id, {"kind": "result", "marker": marker})
        for alias in input_aliases:
            add(
                alias["message_id"],
                {
                    "kind": "tool_input",
                    "call_id": alias["call_id"],
                    "field": alias["field"],
                    "marker": marker,
                },
            )
        return operations

    def _add_user_projection_notice_operations(
        self,
        operations: dict[str, list[dict[str, object]]],
        span_ids: set[str],
        erased: list[str],
    ) -> None:
        if not span_ids:
            return
        placeholders = ",".join("?" for _ in span_ids)
        rows = self._db.execute(
            f"SELECT message_id, content FROM notices WHERE message_id IS NOT NULL "
            f"AND span_id IN ({placeholders}) ORDER BY notice_id",
            list(span_ids),
        ).fetchall()
        for row in rows:
            cleaned = self._scrub_text(
                row["content"],
                erased,
                "[deleted by user]",
                mode="data",
                replace_substrings=True,
            )
            if cleaned == row["content"]:
                continue
            operations.setdefault(str(row["message_id"]), []).append(
                {
                    "kind": "notice",
                    "marker": "[deleted by user]",
                    "content": row["content"],
                    "replacement": cleaned,
                }
            )

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
        span_ids.update(self._user_delete_descendants(span_ids))
        owner_ids = {span_id.split(".", 1)[0] for span_id in span_ids}
        return span_ids, owner_ids

    def _user_delete_descendants(self, span_ids: set[str]) -> set[str]:
        descendants: set[str] = set()
        pending = list(span_ids)
        while pending:
            placeholders = ",".join("?" for _ in pending)
            rows = self._db.execute(
                f"SELECT span_id FROM entries WHERE session_id = ? "
                f"AND parent_id IN ({placeholders})",
                [self.session_id, *pending],
            ).fetchall()
            pending = []
            for row in rows:
                child_id = str(row["span_id"])
                if child_id not in descendants and child_id not in span_ids:
                    descendants.add(child_id)
                    pending.append(child_id)
        return descendants

    def _user_delete_indexed_parents(self, payload: str) -> set[str]:
        """Find result parents only when a stored child exactly aliases payload."""
        rows = self._db.execute(
            "SELECT DISTINCT parent_id FROM entries WHERE session_id = ? "
            "AND parent_id IS NOT NULL AND origin = 'tool' AND content = ?",
            (self.session_id, payload),
        ).fetchall()
        return {str(row["parent_id"]) for row in rows}

    def _user_delete_indexed_children(
        self, parent_ids: set[str], payload: str
    ) -> set[str]:
        if not parent_ids:
            return set()
        placeholders = ",".join("?" for _ in parent_ids)
        rows = self._db.execute(
            f"SELECT span_id FROM entries WHERE parent_id IN ({placeholders}) "
            "AND content = ?",
            [*parent_ids, payload],
        ).fetchall()
        return {str(row["span_id"]) for row in rows}

    def _user_delete_input_aliases(self, span_ids: set[str]) -> list[dict[str, str]]:
        if not span_ids:
            return []
        rows = self._db.execute(
            "SELECT span_id, parent_id, meta_json FROM entries WHERE span_id IN ("
            + ",".join("?" for _ in span_ids)
            + ") AND origin = 'tool_input' AND parent_id IS NOT NULL",
            list(span_ids),
        ).fetchall()
        aliases: list[dict[str, str]] = []
        for row in rows:
            meta = json.loads(row["meta_json"])
            call_id = meta.get("call_id")
            field = meta.get("field")
            if isinstance(call_id, str) and isinstance(field, str):
                aliases.append(
                    {
                        "span_id": str(row["span_id"]),
                        "message_id": str(row["parent_id"]),
                        "call_id": call_id,
                        "field": field,
                    }
                )
        return aliases

    def _user_delete_root_owners(self, span_ids: set[str]) -> set[str]:
        if not span_ids:
            return set()
        rows = self._db.execute(
            "SELECT span_id FROM entries WHERE span_id IN ("
            + ",".join("?" for _ in span_ids)
            + ") AND parent_id IS NULL",
            list(span_ids),
        ).fetchall()
        return {str(row["span_id"]).split(".", 1)[0] for row in rows}

    def _user_delete_linked_result_spans(
        self, input_aliases: list[dict[str, str]]
    ) -> set[str]:
        """Find metadata copies on results causally linked to deleted input."""
        if not input_aliases:
            return set()
        result_ids: set[str] = set()
        for alias in input_aliases:
            row = self._db.execute(
                "SELECT result_span FROM tool_calls WHERE message_id = ? "
                "AND call_id = ? AND result_span IS NOT NULL",
                (alias["message_id"], alias["call_id"]),
            ).fetchone()
            if row is None:
                continue
            result_id = str(row["result_span"])
            result_ids.add(result_id)
            result_ids.update(self._user_delete_descendants({result_id}))
        return result_ids

    def _replace_indexed_child_aliases(
        self, parent_ids: set[str], payload: str
    ) -> None:
        if not parent_ids:
            return
        placeholders = ",".join("?" for _ in parent_ids)
        rows = self._db.execute(
            f"SELECT span_id, content FROM entries WHERE parent_id IN ({placeholders}) "
            "AND content = ?",
            [*parent_ids, payload],
        ).fetchall()
        marker = "[deleted by user]"
        for row in rows:
            self._db.execute(
                "UPDATE entries SET content = ?, content_sha = ?, tokens_est = ? "
                "WHERE span_id = ?",
                (marker, _sha(marker), count_text_tokens(marker), row["span_id"]),
            )

    def _reconcile_indexed_parent_contents(
        self, parent_ids: set[str]
    ) -> dict[str, str]:
        """Rebuild only affected result parents from their stable child rows."""
        owner_contents: dict[str, str] = {}
        for parent_id in parent_ids:
            children = self._db.execute(
                "SELECT content FROM entries WHERE parent_id = ? AND session_id = ? "
                "AND role = 'tool_result' ORDER BY rowid",
                (parent_id, self.session_id),
            ).fetchall()
            content = "".join(str(child["content"] or "") for child in children)
            self._db.execute(
                "UPDATE entries SET content = ?, content_sha = ?, tokens_est = ? "
                "WHERE span_id = ?",
                (content, _sha(content), count_text_tokens(content), parent_id),
            )
            owner_contents[parent_id.split(".", 1)[0]] = content
        return owner_contents

    def _scrub_delete_entry_metadata(
        self,
        span_ids: set[str],
        input_aliases: list[dict[str, str]],
        payload: str,
    ) -> None:
        if not span_ids:
            return
        placeholders = ",".join("?" for _ in span_ids)
        rows = self._db.execute(
            f"SELECT span_id, meta_json FROM entries WHERE span_id IN ({placeholders})",
            list(span_ids),
        ).fetchall()
        aliases_by_span = {
            alias["span_id"]: alias for alias in input_aliases
        }
        for alias in input_aliases:
            linked = self._db.execute(
                "SELECT result_span FROM tool_calls WHERE message_id = ? "
                "AND call_id = ? AND result_span IS NOT NULL",
                (alias["message_id"], alias["call_id"]),
            ).fetchone()
            if linked is None:
                continue
            result_span = str(linked["result_span"])
            aliases_by_span[result_span] = alias
            for descendant in self._user_delete_descendants({result_span}):
                aliases_by_span[descendant] = alias
        for row in rows:
            metadata = json.loads(row["meta_json"])
            alias = aliases_by_span.get(str(row["span_id"]))
            if alias is None:
                continue
            try:
                arguments = json.loads(metadata.get("args_json", ""))
            except json.JSONDecodeError:
                continue
            if not isinstance(arguments, dict) or arguments.get(alias["field"]) != payload:
                continue
            arguments[alias["field"]] = "[deleted by user]"
            args_json = _canonical(arguments)
            metadata["args_json"] = args_json
            tool_name = metadata.get("tool_name")
            if isinstance(tool_name, str):
                metadata["canonical_key"] = f"{tool_name}:{args_json}"
            cleaned = _canonical(metadata)
            if cleaned != row["meta_json"]:
                self._db.execute(
                    "UPDATE entries SET meta_json = ? WHERE span_id = ?",
                    (cleaned, row["span_id"]),
                )

    def _scrub_delete_tool_calls(
        self, input_aliases: list[dict[str, str]], payload: str
    ) -> None:
        for alias in input_aliases:
            row = self._db.execute(
                "SELECT tool_call_id, tool_name, args_json FROM tool_calls "
                "WHERE message_id = ? AND call_id = ?",
                (alias["message_id"], alias["call_id"]),
            ).fetchone()
            if row is None:
                continue
            try:
                args = json.loads(row["args_json"])
            except json.JSONDecodeError:
                continue
            if not isinstance(args, dict) or args.get(alias["field"]) != payload:
                continue
            args[alias["field"]] = "[deleted by user]"
            args_json = _canonical(args)
            self._db.execute(
                "UPDATE tool_calls SET args_json = ?, canonical_key = ? "
                "WHERE tool_call_id = ?",
                (args_json, f"{row['tool_name']}:{args_json}", row["tool_call_id"]),
            )

    @staticmethod
    def _scrub_delete_message(
        message: dict,
        *,
        root_owner: bool,
        indexed_content: str | None,
        input_aliases: list[dict[str, str]],
        payload: str,
    ) -> dict:
        cleaned = deepcopy(message)
        content = cleaned.get("content")
        if root_owner and content == payload:
            cleaned["content"] = "[deleted by user]"
        elif indexed_content is not None:
            cleaned["content"] = indexed_content
        for alias in input_aliases:
            for call in cleaned.get("tool_calls") or []:
                if call.get("id") != alias["call_id"]:
                    continue
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments", ""))
                except json.JSONDecodeError:
                    continue
                if isinstance(arguments, dict) and arguments.get(alias["field"]) == payload:
                    arguments[alias["field"]] = "[deleted by user]"
                    function["arguments"] = _canonical(arguments)
        return cleaned

    def _scrub_delete_messages(
        self,
        root_owner_ids: set[str],
        indexed_owner_contents: dict[str, str],
        input_aliases: list[dict[str, str]],
        payload: str,
    ) -> None:
        owner_ids = set(root_owner_ids) | set(indexed_owner_contents) | {
            alias["message_id"] for alias in input_aliases
        }
        if not owner_ids:
            return
        placeholders = ",".join("?" for _ in owner_ids)
        rows = self._db.execute(
            f"SELECT rowid, message_id, message_json FROM messages WHERE session_id = ? "
            f"AND message_id IN ({placeholders})",
            [self.session_id, *owner_ids],
        ).fetchall()
        for row in rows:
            message_id = str(row["message_id"])
            message = json.loads(row["message_json"])
            cleaned_message = self._scrub_delete_message(
                message,
                root_owner=message_id in root_owner_ids,
                indexed_content=indexed_owner_contents.get(message_id),
                input_aliases=[
                    alias
                    for alias in input_aliases
                    if alias["message_id"] == message_id
                ],
                payload=payload,
            )
            cleaned = _canonical(cleaned_message)
            if cleaned != row["message_json"]:
                self._db.execute(
                    "UPDATE messages SET message_json = ?, content_sha = ?, scrubbed = 1 "
                    "WHERE rowid = ?",
                    (cleaned, _sha(cleaned), row["rowid"]),
                )

    def _scrub_delete_folds_and_notices(
        self, span_ids: set[str], erased: list[str]
    ) -> dict[int, str]:
        if span_ids:
            placeholders = ",".join("?" for _ in span_ids)
            rows = self._db.execute(
                f"SELECT fold_id, note FROM folds WHERE span_id IN ({placeholders})",
                list(span_ids),
            ).fetchall()
            for row in rows:
                cleaned = self._scrub_text(
                    row["note"], erased, "[deleted by user]", mode="data",
                    replace_substrings=True,
                )
                if cleaned != row["note"]:
                    self._db.execute(
                        "UPDATE folds SET note = ? WHERE fold_id = ?",
                        (cleaned, row["fold_id"]),
                    )
        if not span_ids:
            return {}
        placeholders = ",".join("?" for _ in span_ids)
        rows = self._db.execute(
            f"SELECT notice_id, content FROM notices WHERE span_id IN ({placeholders})",
            list(span_ids),
        ).fetchall()
        updates: dict[int, str] = {}
        for row in rows:
            cleaned = self._scrub_text(
                row["content"], erased, "[deleted by user]", mode="data",
                replace_substrings=True,
            )
            if cleaned != row["content"]:
                updates[int(row["notice_id"])] = cleaned
                self._db.execute(
                    "UPDATE notices SET content = ? WHERE notice_id = ?",
                    (cleaned, row["notice_id"]),
                )
        return updates

    def _scrub_delete_live_shadow(
        self,
        root_owner_ids: set[str],
        indexed_owner_contents: dict[str, str],
        input_aliases: list[dict[str, str]],
        payload: str,
    ) -> None:
        if self._shadow_ref is None:
            return
        owner_ids = set(root_owner_ids) | set(indexed_owner_contents) | {
            alias["message_id"] for alias in input_aliases
        }
        for index, message_id in enumerate(self._active_ids):
            if message_id not in owner_ids or index >= len(self._shadow_ref):
                continue
            message = self._shadow_ref[index]
            cleaned = self._scrub_delete_message(
                message,
                root_owner=message_id in root_owner_ids,
                indexed_content=indexed_owner_contents.get(message_id),
                input_aliases=[
                    alias
                    for alias in input_aliases
                    if alias["message_id"] == message_id
                ],
                payload=payload,
            )
            if cleaned == message or not isinstance(cleaned, dict):
                continue
            message.clear()
            message.update(cleaned)
            if index < len(self._snapshots):
                self._snapshots[index] = _canonical(message)

    def _purge_entry_copies(
        self, erased: list[str], marker: str, *, replace_substrings: bool
    ) -> None:
        rows = self._db.execute(
            "SELECT span_id, parent_id, role, content FROM entries "
            "WHERE content IS NOT NULL"
        ).fetchall()
        for row in rows:
            content = row["content"]
            cleaned = str(
                self._scrub_data(
                    content, erased, marker, replace_substrings=replace_substrings
                )
            )
            if cleaned == content:
                continue
            span_id = row["span_id"]
            if content in erased:
                if row["parent_id"] is not None and row["role"] == "tool_result":
                    # A chunk is an index into its parent, not an independent
                    # copy. Sanitizing it here lets reconciliation repartition
                    # the cleaned parent coherently. If this is a target chunk,
                    # its purged parent terminally purges it below.
                    self._db.execute(
                        "UPDATE entries SET content = ?, content_sha = ?, "
                        "tokens_est = ? WHERE span_id = ?",
                        (marker, _sha(marker), count_text_tokens(marker), span_id),
                    )
                else:
                    self._mark_entry_purged(span_id)
            else:
                self._db.execute(
                    "UPDATE entries SET content = ?, content_sha = ?, tokens_est = ? "
                    "WHERE span_id = ?",
                    (cleaned, _sha(cleaned), count_text_tokens(cleaned), span_id),
                )
        self._reconcile_child_copies()

    def _mark_entry_purged(self, span_id: str) -> None:
        state = self._db.execute(
            "SELECT state FROM span_state WHERE span_id = ?", (span_id,)
        ).fetchone()
        self._db.execute(
            "UPDATE entries SET content = NULL WHERE span_id = ?",
            (span_id,),
        )
        self._db.execute(
            "UPDATE span_state SET state = 'purged' WHERE span_id = ?",
            (span_id,),
        )
        if state is None or state["state"] != "purged":
            self._db.execute(
                "INSERT INTO folds(span_id, reason, note, decider, folded_turn, "
                "placement, applied_turn) VALUES (?, 'user_delete', "
                "'deleted by user', 'user', ?, 'in_place', ?)",
                (span_id, self.turn, self.turn),
            )

    def _reconcile_child_copies(self, parent_ids: set[str] | None = None) -> None:
        """Keep chunk storage from retaining bytes removed from its parent."""
        query = (
            "SELECT DISTINCT p.span_id, p.content, s.state FROM entries p "
            "JOIN entries c ON c.parent_id = p.span_id "
            "JOIN span_state s ON s.span_id = p.span_id "
            "WHERE p.session_id = ? "
            "AND p.role = 'tool_result' AND c.role = 'tool_result' "
        )
        parameters: list[object] = [self.session_id]
        if parent_ids is not None:
            if not parent_ids:
                return
            query += "AND p.span_id IN (" + ",".join("?" for _ in parent_ids) + ") "
            parameters.extend(parent_ids)
        parents = self._db.execute(query + "ORDER BY p.rowid", parameters).fetchall()
        for parent in parents:
            children = self._db.execute(
                "SELECT e.span_id, e.content, s.state FROM entries e "
                "JOIN span_state s USING(span_id) "
                "WHERE e.parent_id = ? AND e.session_id = ? "
                "AND e.role = 'tool_result' ORDER BY e.rowid",
                (parent["span_id"], self.session_id),
            ).fetchall()
            if parent["state"] == "purged":
                for child in children:
                    self._mark_entry_purged(child["span_id"])
                continue

            content = parent["content"] or ""
            if "".join(child["content"] or "" for child in children) == content:
                continue
            pieces = _split_span(content, self.config.chunk_tokens)
            if len(pieces) > len(children):
                pieces = [
                    *pieces[: len(children) - 1],
                    "".join(pieces[len(children) - 1 :]),
                ]
            else:
                pieces.extend("" for _ in range(len(children) - len(pieces)))
            for child, piece in zip(children, pieces):
                if child["state"] == "purged":
                    continue
                self._db.execute(
                    "UPDATE entries SET content = ?, content_sha = ?, tokens_est = ? "
                    "WHERE span_id = ?",
                    (
                        piece,
                        _sha(piece),
                        count_text_tokens(piece),
                        child["span_id"],
                    ),
                )

    def _scrub_live_shadow(
        self,
        erased: tuple[str, ...] | list[str],
        marker: str,
        *,
        replace_substrings: bool,
        exhaustive: bool = False,
        identifier_replacements: dict[str, str] | None = None,
    ) -> None:
        if self._shadow_ref is None:
            return
        for index, message in enumerate(self._shadow_ref):
            cleaned = self._scrub_structured(
                deepcopy(message),
                erased,
                marker,
                replace_substrings=replace_substrings,
                exhaustive=exhaustive,
                identifier_replacements=identifier_replacements,
            )
            if cleaned == message or not isinstance(cleaned, dict):
                continue
            message.clear()
            message.update(cleaned)
            if index < len(self._snapshots):
                self._snapshots[index] = _canonical(message)

    @classmethod
    def _scrub_structured(
        cls,
        value: object,
        erased: tuple[str, ...] | list[str],
        marker: str,
        *,
        replace_substrings: bool,
        exhaustive: bool = False,
        identifier_replacements: dict[str, str] | None = None,
    ) -> object:
        """Scrub user data without changing the surrounding protocol shape."""
        if isinstance(value, dict):
            cleaned: dict[object, object] = {}
            for key, item in value.items():
                # Keys define message, event, and tool schemas. Rewriting one
                # can make an otherwise valid transcript impossible to replay.
                if key == "role":
                    cleaned[key] = item
                elif exhaustive and key in _IDENTIFIER_VALUE_KEYS:
                    cleaned[key] = cls._scrub_identifier(
                        item, erased, identifier_replacements
                    )
                elif key in {"args", "arguments", "args_json"}:
                    cleaned[key] = cls._scrub_arguments(
                        item, erased, marker, replace_substrings=replace_substrings
                    )
                elif key == "canonical_key":
                    cleaned[key] = cls._scrub_canonical_key(
                        item,
                        erased,
                        marker,
                        replace_substrings=replace_substrings,
                        exhaustive=exhaustive,
                        identifier_replacements=identifier_replacements,
                    )
                elif key in {
                    "content",
                    "note",
                    "output",
                    "payload",
                    "result",
                    "summary_text",
                    "text",
                }:
                    cleaned[key] = cls._scrub_data(
                        item, erased, marker, replace_substrings=replace_substrings
                    )
                elif not exhaustive and key in {
                    "actor",
                    "call_id",
                    "decider",
                    "ev",
                    "event",
                    "field",
                    "id",
                    "message_id",
                    "name",
                    "origin",
                    "parent_id",
                    "placement",
                    "reason",
                    "role",
                    "session_id",
                    "span",
                    "span_id",
                    "state",
                    "tool_call_id",
                    "tool_name",
                    "type",
                }:
                    cleaned[key] = item
                else:
                    cleaned[key] = cls._scrub_structured(
                        item,
                        erased,
                        marker,
                        replace_substrings=replace_substrings,
                        exhaustive=exhaustive,
                        identifier_replacements=identifier_replacements,
                    )
            return cleaned
        if isinstance(value, list):
            return [
                cls._scrub_structured(
                    item,
                    erased,
                    marker,
                    replace_substrings=replace_substrings,
                    exhaustive=exhaustive,
                    identifier_replacements=identifier_replacements,
                )
                for item in value
            ]
        if not isinstance(value, str):
            return value
        if exhaustive:
            return cls._scrub_data(
                value, erased, marker, replace_substrings=replace_substrings
            )
        # Unknown scalar fields may be copies of the payload, but partial
        # replacement is unsafe: they may also be protocol identifiers.
        return marker if value in erased else value

    @staticmethod
    def _identifier_alias(secret: str, nonce: int = 0) -> str:
        source = secret.encode()
        if nonce:
            source += b"\0" + str(nonce).encode()
        digest = hashlib.sha256(source).digest()
        replacement = base64.b32encode(digest).decode().lower().rstrip("=")
        return f"redacted_{replacement}"

    @staticmethod
    def _replace_identifiers(value: str, replacements: dict[str, str]) -> str:
        cleaned = value
        for secret, replacement in replacements.items():
            if secret:
                cleaned = cleaned.replace(secret, replacement)
        return cleaned

    @classmethod
    def _scrub_identifier(
        cls,
        value: object,
        erased: tuple[str, ...] | list[str],
        replacements: dict[str, str] | None = None,
    ) -> object:
        if not isinstance(value, str):
            return value
        if replacements is None:
            replacements = {
                secret: cls._identifier_alias(secret) for secret in erased if secret
            }
        return cls._replace_identifiers(value, replacements)

    @classmethod
    def _collect_identifier_values(cls, value: object, found: set[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "role":
                    continue
                if key in _IDENTIFIER_VALUE_KEYS:
                    if isinstance(item, str):
                        found.add(item)
                    continue
                if key == "canonical_key" and isinstance(item, str):
                    tool_name, separator, _arguments = item.partition(":")
                    found.add(tool_name if separator else item)
                    continue
                if key in {
                    "args",
                    "arguments",
                    "args_json",
                    "content",
                    "note",
                    "output",
                    "payload",
                    "result",
                    "summary_text",
                    "text",
                }:
                    continue
                cls._collect_identifier_values(item, found)
        elif isinstance(value, list):
            for item in value:
                cls._collect_identifier_values(item, found)

    def _sensitive_identifier_replacements(
        self,
        secrets: tuple[str, ...],
        offered_tool_names: tuple[str, ...] = (),
    ) -> dict[str, str]:
        identifiers = set(offered_tool_names)
        identifiers.update({
            str(value)
            for row in self._db.execute(
                "SELECT call_id, tool_name FROM tool_calls"
            ).fetchall()
            for value in (row["call_id"], row["tool_name"])
        })
        structured_columns = (
            ("entries", "meta_json"),
            ("messages", "message_json"),
            ("projections", "projection_json"),
        )
        for table, column in structured_columns:
            rows = self._db.execute(
                f"SELECT {column} AS value FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    decoded = json.loads(row["value"])
                except json.JSONDecodeError:
                    continue
                self._collect_identifier_values(decoded, identifiers)
        if self._shadow_ref is not None:
            self._collect_identifier_values(self._shadow_ref, identifiers)
        for path in self._purge_paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                # The purge pass retains responsibility for surfacing artifact
                # failures or using its scanner-only byte fallback; inventory
                # collection must not change that behavior.
                continue
            for line in lines:
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._collect_identifier_values(decoded, identifiers)

        replacements = {
            secret: self._scanner_aliases[_sha(secret)]
            for secret in secrets
            if _sha(secret) in self._scanner_aliases
        }
        for secret in secrets:
            if secret in replacements:
                continue
            current_identifiers = {
                self._replace_identifiers(original, replacements)
                for original in identifiers
            }
            nonce = 0
            while True:
                candidate = self._identifier_alias(secret, nonce)
                trial = {secret: candidate}
                transformed: dict[str, str] = {}
                collision = any(
                    candidate in original for original in current_identifiers
                )
                for original in current_identifiers:
                    if collision:
                        break
                    cleaned = self._replace_identifiers(original, trial)
                    if cleaned != original and cleaned in current_identifiers:
                        collision = True
                        break
                    prior = transformed.get(cleaned)
                    if prior is not None and prior != original:
                        collision = True
                        break
                    transformed[cleaned] = original
                if not collision:
                    replacements[secret] = candidate
                    break
                nonce += 1
        return replacements

    @classmethod
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
                    item, erased, marker, replace_substrings=replace_substrings
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                cls._scrub_data(
                    item, erased, marker, replace_substrings=replace_substrings
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

    @classmethod
    def _scrub_arguments(
        cls,
        value: object,
        erased: tuple[str, ...] | list[str],
        marker: str,
        *,
        replace_substrings: bool,
    ) -> object:
        if isinstance(value, str):
            if value in erased:
                return marker
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return cls._scrub_data(
                    value, erased, marker, replace_substrings=replace_substrings
                )
            else:
                cleaned_decoded = cls._scrub_data(
                    decoded, erased, marker, replace_substrings=replace_substrings
                )
                return (
                    value
                    if cleaned_decoded == decoded
                    else _canonical(cleaned_decoded)
                )
        return cls._scrub_data(
            value, erased, marker, replace_substrings=replace_substrings
        )

    @classmethod
    def _scrub_canonical_key(
        cls,
        value: object,
        erased: tuple[str, ...] | list[str],
        marker: str,
        *,
        replace_substrings: bool,
        exhaustive: bool = False,
        identifier_replacements: dict[str, str] | None = None,
    ) -> object:
        if not isinstance(value, str):
            return value
        if value in erased:
            return marker
        tool_name, separator, arguments = value.partition(":")
        if not separator:
            return (
                cls._scrub_identifier(value, erased, identifier_replacements)
                if exhaustive
                else value
            )
        cleaned = cls._scrub_arguments(
            arguments, erased, marker, replace_substrings=replace_substrings
        )
        cleaned_name = (
            cls._scrub_identifier(tool_name, erased, identifier_replacements)
            if exhaustive
            else tool_name
        )
        return (
            value
            if cleaned == arguments and cleaned_name == tool_name
            else f"{cleaned_name}:{cleaned}"
        )

    @classmethod
    def _scrub_text(
        cls,
        value: str,
        erased: tuple[str, ...] | list[str],
        marker: str,
        *,
        mode: str,
        replace_substrings: bool,
        exhaustive: bool = False,
        identifier_replacements: dict[str, str] | None = None,
    ) -> str:
        if mode in {"arguments", "structured"}:
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return value
            if mode == "arguments":
                cleaned_decoded = cls._scrub_data(
                    decoded, erased, marker, replace_substrings=replace_substrings
                )
            else:
                cleaned_decoded = cls._scrub_structured(
                    decoded,
                    erased,
                    marker,
                    replace_substrings=replace_substrings,
                    exhaustive=exhaustive,
                    identifier_replacements=identifier_replacements,
                )
            return value if cleaned_decoded == decoded else _canonical(cleaned_decoded)
        if mode == "canonical_key":
            return str(
                cls._scrub_canonical_key(
                    value,
                    erased,
                    marker,
                    replace_substrings=replace_substrings,
                    exhaustive=exhaustive,
                    identifier_replacements=identifier_replacements,
                )
            )
        if mode == "identifier":
            return (
                str(cls._scrub_identifier(value, erased, identifier_replacements))
                if exhaustive
                else value
            )
        return str(
            cls._scrub_data(
                value, erased, marker, replace_substrings=replace_substrings
            )
        )

    def _scrub_sqlite(
        self,
        erased: tuple[str, ...] | list[str],
        marker: str,
        *,
        replace_substrings: bool,
        exhaustive: bool = False,
        identifier_replacements: dict[str, str] | None = None,
    ) -> None:
        columns = (
            ("entries", "content", "data"),
            ("entries", "meta_json", "structured"),
            ("folds", "note", "data"),
            ("messages", "message_json", "structured"),
            ("tool_calls", "call_id", "identifier"),
            ("tool_calls", "tool_name", "identifier"),
            ("tool_calls", "args_json", "arguments"),
            ("tool_calls", "canonical_key", "canonical_key"),
            ("notices", "content", "data"),
        )
        for table, column, mode in columns:
            rows = self._db.execute(
                f"SELECT rowid AS scrub_rowid, {column} AS value FROM {table} "
                f"WHERE {column} IS NOT NULL"
            ).fetchall()
            for row in rows:
                cleaned = self._scrub_text(
                    row["value"],
                    erased,
                    marker,
                    mode=mode,
                    replace_substrings=replace_substrings,
                    exhaustive=exhaustive,
                    identifier_replacements=identifier_replacements,
                )
                if cleaned != row["value"]:
                    self._db.execute(
                        f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                        (cleaned, row["scrub_rowid"]),
                    )
                    if table == "messages" and column == "message_json":
                        self._db.execute(
                            "UPDATE messages SET content_sha = ?, scrubbed = 1 "
                            "WHERE rowid = ?",
                            (_sha(cleaned), row["scrub_rowid"]),
                        )

    def _redact_sensitive_projections(
        self, secrets: tuple[str, ...], identifier_replacements: dict[str, str]
    ) -> None:
        rows = self._db.execute(
            "SELECT projection_id, projection_json FROM projections"
        ).fetchall()
        for row in rows:
            try:
                decoded = json.loads(row["projection_json"])
            except json.JSONDecodeError as error:
                raise ProjectionError(
                    f"projection {row['projection_id']} is malformed"
                ) from error
            cleaned = self._scrub_structured(
                decoded,
                secrets,
                _REDACTION_MARKER,
                replace_substrings=True,
                exhaustive=True,
                identifier_replacements=identifier_replacements,
            )
            if cleaned != decoded:
                self._db.execute(
                    "UPDATE projections SET projection_json = ?, redacted = 1 "
                    "WHERE projection_id = ?",
                    (_canonical(cleaned), row["projection_id"]),
                )

    @staticmethod
    def _redact_rendered_result_bodies(
        content: str,
        target_span_ids: list[str],
        render_span_ids: list[str],
        payload: str,
        marker: str,
    ) -> str:
        headers: dict[str, list[int]] = {
            span_id: [] for span_id in target_span_ids
        }
        offset = 0
        for line in content.splitlines(keepends=True):
            header = line.removesuffix("\n")
            for span_id in headers:
                if header.startswith(f"[{span_id} · ~") and header.endswith(" tok]"):
                    headers[span_id].append(offset + len(line))
            offset += len(line)

        replacements: list[tuple[int, int]] = []
        for body_offsets in headers.values():
            candidates: list[tuple[int, int]] = []
            for start in body_offsets:
                if not content.startswith(payload, start):
                    continue
                end = start + len(payload)
                if end != len(content):
                    if not content.startswith("\n", end):
                        continue
                    next_line = content[end + 1 :].partition("\n")[0]
                    generated = next_line in {
                        marker,
                        _REDACTION_MARKER,
                    } or next_line.startswith("[dup of ")
                    for span_id in render_span_ids:
                        generated = generated or (
                            next_line.startswith(f"[{span_id} · ~")
                            and next_line.endswith(" tok]")
                        )
                        generated = generated or next_line.startswith(
                            (
                                f"[folded {span_id},",
                                f"[unfolded {span_id} →",
                                f"[removed {span_id} —",
                            )
                        )
                    if not generated:
                        continue
                candidates.append((start, end))
            if len(candidates) == 1:
                replacements.append(candidates[0])
        for start, end in sorted(replacements, reverse=True):
            content = f"{content[:start]}{marker}{content[end:]}"
        return content

    def _redact_user_projections(
        self, operations: dict[str, list[dict[str, object]]]
    ) -> None:
        if not operations:
            return
        rows = self._db.execute(
            "SELECT projection_id, projection_json, source_ids_json FROM projections"
        ).fetchall()
        for row in rows:
            try:
                messages = json.loads(row["projection_json"])
                sources = json.loads(row["source_ids_json"])
            except json.JSONDecodeError as error:
                raise ProjectionError(
                    f"projection {row['projection_id']} is malformed"
                ) from error
            if not isinstance(messages, list) or not isinstance(sources, list):
                raise ProjectionError(f"projection {row['projection_id']} is malformed")
            changed = False
            for message, source in zip(messages, sources):
                if not isinstance(message, dict) or not isinstance(source, str):
                    continue
                source_id = source.split(":", 1)[1] if ":" in source else source
                source_operations = operations.get(source_id)
                if source_operations is None:
                    continue
                for operation in source_operations:
                    marker = str(operation["marker"])
                    if operation["kind"] == "notice":
                        content = message.get("content")
                        if not isinstance(content, str):
                            continue
                        notice_block, separator, prose = content.partition("\n\n")
                        old_notice = str(operation["content"])
                        if not separator or old_notice not in notice_block:
                            continue
                        cleaned_block = notice_block.replace(
                            old_notice, str(operation["replacement"]), 1
                        )
                        message["content"] = f"{cleaned_block}{separator}{prose}"
                        changed = True
                        continue
                    if operation["kind"] == "indexed_content":
                        content = message.get("content")
                        if source.startswith("span:"):
                            if not isinstance(content, str):
                                continue
                            header, separator, target = content.partition("\n")
                            if not separator:
                                continue
                            cleaned = self._scrub_data(
                                target,
                                [str(operation["payload"])],
                                marker,
                                replace_substrings=True,
                            )
                            if cleaned == target:
                                continue
                            message["content"] = f"{header}{separator}{cleaned}"
                            changed = True
                            continue
                        if not source.startswith("message:") or not isinstance(
                            content, str
                        ):
                            continue
                        cleaned = self._redact_rendered_result_bodies(
                            content,
                            [str(value) for value in operation["target_span_ids"]],
                            [str(value) for value in operation["render_span_ids"]],
                            str(operation["payload"]),
                            marker,
                        )
                        if cleaned != content:
                            message["content"] = cleaned
                            changed = True
                        continue
                    if operation["kind"] in {"content", "result"}:
                        if message.get("content") != marker:
                            message["content"] = marker
                            changed = True
                        continue
                    call_id = operation["call_id"]
                    field = str(operation["field"])
                    for call in message.get("tool_calls") or []:
                        if not isinstance(call, dict) or call.get("id") != call_id:
                            continue
                        function = call.get("function")
                        if not isinstance(function, dict):
                            continue
                        try:
                            arguments = json.loads(function.get("arguments", ""))
                        except json.JSONDecodeError:
                            continue
                        if (
                            not isinstance(arguments, dict)
                            or arguments.get(field) == marker
                        ):
                            continue
                        arguments[field] = marker
                        function["arguments"] = _canonical(arguments)
                        changed = True
            if changed:
                self._db.execute(
                    "UPDATE projections SET projection_json = ?, redacted = 1 "
                    "WHERE projection_id = ?",
                    (_canonical(messages), row["projection_id"]),
                )

    def _purge_session_log(
        self,
        erased: tuple[str, ...] | list[str],
        marker: str = "[deleted by user]",
        *,
        replace_substrings: bool,
        exhaustive: bool = False,
        identifier_replacements: dict[str, str] | None = None,
    ) -> None:
        if not erased:
            return

        def scrub(value: object) -> object:
            return self._scrub_structured(
                value,
                erased,
                marker,
                replace_substrings=replace_substrings,
                exhaustive=exhaustive,
                identifier_replacements=identifier_replacements,
            )

        for path in self._purge_paths:
            if not path.exists():
                continue
            try:
                try:
                    lines = path.read_text().splitlines()
                except UnicodeDecodeError:
                    if not exhaustive:
                        raise
                    raw = path.read_bytes()
                    cleaned = raw
                    marker_bytes = marker.encode()
                    for content in erased:
                        if content:
                            cleaned = cleaned.replace(content.encode(), marker_bytes)
                    temporary = path.with_suffix(path.suffix + ".purge.tmp")
                    temporary.write_bytes(cleaned)
                    os.replace(temporary, path)
                    continue
                rendered: list[str] = []
                for line in lines:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        rendered.append(
                            str(
                                self._scrub_data(
                                    line,
                                    erased,
                                    marker,
                                    replace_substrings=True,
                                )
                            )
                            if exhaustive
                            else line
                        )
                        continue
                    rendered.append(json.dumps(scrub(event), ensure_ascii=False))
                temporary = path.with_suffix(path.suffix + ".purge.tmp")
                temporary.write_text("\n".join(rendered) + ("\n" if rendered else ""))
                os.replace(temporary, path)
            except OSError as error:
                raise FoldError(f"could not purge external artifact {path}: {error}") from error

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
        transaction = nullcontext() if self._sync_in_progress else self._db
        with transaction:
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
        pinned = self._overlapping_pin(span_id)
        if pinned is not None:
            raise FoldError(f"{span_id} overlaps pinned span {pinned} and cannot be folded")
        if reason not in AGENT_REASONS:
            raise FoldError(f"invalid fold reason {reason!r}")
        if reason == "poisoned" and entry["origin"] == "assistant":
            for related in self._assistant_exchange_spans(span_id):
                pinned = self._overlapping_pin(related)
                if pinned is not None:
                    raise FoldError(
                        f"{span_id} poison cascade overlaps pinned span {pinned}"
                    )
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
                projection_hash, parent_hash = self._record_projection(
                    projection, kind="checkpoint"
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
            with self._db:
                cursor = self._db.execute(
                    "INSERT INTO notices(message_id, kind, content, created_turn, "
                    "emitted_turn) VALUES (?, 'workspace', ?, ?, ?)",
                    (self._turn_user_id, workspace_notice, self.turn, self.turn),
                )
            self._current_notices.append(workspace_notice)
            self._current_notice_ids.append(int(cursor.lastrowid))
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
            "SELECT projection_id, projection_hash, parent_hash, kind, turn, tokens_est, "
            "redacted "
            "FROM projections ORDER BY projection_id"
        ).fetchall()
        return [{**dict(row), "redacted": bool(row["redacted"])} for row in rows]

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
        except (TypeError, json.JSONDecodeError) as error:
            raise ProjectionError(f"projection {projection_id} is malformed") from error
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            raise ProjectionError(f"projection {projection_id} is malformed")
        if not row["redacted"] and _sha(_canonical(messages)) != row["projection_hash"]:
            raise ProjectionError(f"projection {projection_id} hash mismatch")
        return messages

    def _record_projection(
        self, messages: list[dict], *, kind: str
    ) -> tuple[str, str | None]:
        projection_json = _canonical(messages)
        projection_hash = _sha(projection_json)
        if (
            self._last_projection_sources is not None
            and self._last_projection_sources[0] == projection_hash
            and len(self._last_projection_sources[1]) == len(messages)
        ):
            source_ids = self._last_projection_sources[1]
        else:
            source_ids = ["unknown"] * len(messages)
        parent = self._db.execute(
            "SELECT projection_hash FROM projections ORDER BY projection_id DESC LIMIT 1"
        ).fetchone()
        parent_hash = parent["projection_hash"] if parent is not None else None
        self._db.execute(
            "INSERT INTO projections(projection_hash, parent_hash, kind, turn, "
            "tokens_est, projection_json, source_ids_json, redacted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                projection_hash,
                parent_hash,
                kind,
                self.turn,
                estimate_tokens(messages),
                projection_json,
                _canonical(source_ids),
            ),
        )
        return projection_hash, parent_hash

    def record_request(self, messages: list[dict]) -> str:
        """Persist the exact request array and aligned projection sources."""
        with self._db:
            projection_hash, parent_hash = self._record_projection(
                messages, kind="request"
            )
        self._event(
            "request_dispatch",
            projection_hash=projection_hash,
            parent_hash=parent_hash,
        )
        return projection_hash

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
        projected, source_ids = self._build_projection(messages, message_ids, turn)
        self._last_projection_sources = (_sha(_canonical(projected)), source_ids)
        return projected

    def _build_projection(
        self,
        messages: list[dict],
        message_ids: list[str],
        turn: int | None,
    ) -> tuple[list[dict], list[str]]:
        projected: list[dict] = []
        source_ids: list[str] = []

        def append(message: dict, source: str) -> None:
            projected.append(message)
            source_ids.append(source)

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
                append(message, f"message:{message_id}")
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
            append(message, f"message:{message_id}")

        for span_id in self._tail_spans(turn):
            entry = self._entry(span_id)
            meta = json.loads(entry["meta_json"])
            stale = (
                " — source may have changed since; re-read if freshness matters"
                if meta.get("refetchable")
                else ""
            )
            fold = self._latest_fold(span_id, turn)
            append(
                {
                    "role": "user",
                    "content": (
                        f"[unfolded {span_id}, originally from turn "
                        f"{fold['folded_turn']}{stale}]\n{entry['content']}"
                    ),
                },
                f"span:{span_id}",
            )
        try:
            self._lint(projected)
        except ProjectionError:
            if turn is None:
                self._event("linter_fail")
            raise
        if turn is None:
            self._event("linter_pass")
        return projected, source_ids

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
