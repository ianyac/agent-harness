"""Session discovery, runtime ownership, and authoritative transcript access."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import secrets
from typing import Callable

from harness.llm import LLMClient
from harness.session import SessionLog, lock, unlock
from server.metadata import MetadataStore, NewSession, SessionRecord
from server.runtime import HarnessRuntime, RuntimeConfig


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FOLD_DECISION_SUFFIX = ".fold-decisions.jsonl"


class InvalidSessionId(ValueError):
    pass


class SessionNotFound(LookupError):
    pass


class InvalidWorkspace(ValueError):
    pass


class InvalidTitle(ValueError):
    pass


class CredentialPrerequisite(RuntimeError):
    pass


class SessionResumeError(RuntimeError):
    pass


def validate_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise InvalidSessionId("invalid session id")
    return session_id


class SessionManager:
    """Own metadata and one reviewed harness runtime per open session."""

    def __init__(
        self,
        metadata: MetadataStore,
        base_workspace: Path,
        llm_factory: Callable[[], LLMClient],
        *,
        compact_threshold: int | None = None,
    ) -> None:
        self.metadata = metadata
        self.base_workspace = self._validated_workspace(base_workspace)
        self._llm_factory = llm_factory
        self._compact_threshold = compact_threshold
        self._runtimes: dict[str, HarnessRuntime] = {}
        self._runtime_lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _validated_workspace(workspace: Path | str) -> Path:
        candidate = Path(workspace)
        if not candidate.is_absolute():
            raise InvalidWorkspace("workspace must be an absolute directory")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise InvalidWorkspace("workspace must be an existing directory") from error
        if not resolved.is_dir():
            raise InvalidWorkspace("workspace must be an existing directory")
        return resolved

    @staticmethod
    def _new_session_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        return f"{timestamp}-{secrets.token_hex(6)}"

    @staticmethod
    def _session_path(workspace: Path, session_id: object) -> Path:
        validated = validate_session_id(session_id)
        return workspace / ".agent" / "sessions" / f"{validated}.jsonl"

    @staticmethod
    def _context_mode_for_discovery(session_path: Path) -> str:
        mode_path = session_path.with_suffix(".context-mode")
        try:
            stored = mode_path.read_text().strip()
        except FileNotFoundError:
            stored = ""
        except OSError:
            stored = ""
        if stored == "folding" or session_path.with_suffix(".folds.sqlite3").exists():
            return "folding"
        return "compaction"

    def _required_record(
        self, session_id: object, *, include_archived: bool = False
    ) -> SessionRecord:
        validated = validate_session_id(session_id)
        record = self.metadata.get_session(validated)
        if record is None or (record.archived_at is not None and not include_archived):
            raise SessionNotFound("session not found")
        return record

    def _known_workspaces(self) -> tuple[Path, ...]:
        workspaces = {self.base_workspace}
        for record in self.metadata.list_sessions(include_archived=True):
            try:
                workspace = Path(record.workspace).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if workspace.is_dir():
                workspaces.add(workspace)
        return tuple(sorted(workspaces, key=str))

    async def discover(self) -> None:
        """Rebuild missing rows from explicitly known workspace roots only."""
        known_ids = {
            record.session_id
            for record in self.metadata.list_sessions(include_archived=True)
        }
        for workspace in self._known_workspaces():
            sessions_dir = workspace / ".agent" / "sessions"
            if not sessions_dir.is_dir():
                continue
            for candidate in sorted(sessions_dir.glob("*.jsonl")):
                if (
                    candidate.name.endswith(_FOLD_DECISION_SUFFIX)
                    or candidate.is_symlink()
                    or not candidate.is_file()
                ):
                    continue
                session_id = candidate.stem
                try:
                    validate_session_id(session_id)
                except InvalidSessionId:
                    continue
                if session_id in known_ids:
                    continue
                defaults = NewSession.defaults(session_id, workspace)
                self.metadata.upsert_discovered_session(
                    NewSession(
                        session_id=defaults.session_id,
                        workspace=defaults.workspace,
                        title=defaults.title,
                        mode=defaults.mode,
                        context_mode=self._context_mode_for_discovery(candidate),
                    )
                )
                known_ids.add(session_id)

    def list_sessions(self) -> list[SessionRecord]:
        return self.metadata.list_sessions()

    def get_session(self, session_id: object) -> SessionRecord:
        return self._required_record(session_id)

    @staticmethod
    def _credential_failure(error: BaseException) -> bool:
        if isinstance(error, (FileNotFoundError, json.JSONDecodeError, KeyError)):
            return True
        if isinstance(error, RuntimeError):
            text = str(error).casefold()
            return "codex login" in text or (
                "codex" in text and ("credential" in text or "auth" in text)
            )
        return False

    def _build_llm(self) -> LLMClient:
        try:
            return self._llm_factory()
        except Exception as error:
            if self._credential_failure(error):
                raise CredentialPrerequisite(
                    "Codex credentials are required. Run `codex login` and retry."
                ) from None
            raise

    def _runtime_config(self, record: NewSession | SessionRecord) -> RuntimeConfig:
        return RuntimeConfig(
            session_id=record.session_id,
            workspace=record.workspace,
            mode=record.mode,
            context_mode=record.context_mode,
            compact_threshold=self._compact_threshold,
        )

    def _construct_runtime(
        self, record: NewSession | SessionRecord
    ) -> HarnessRuntime:
        session_path = self._session_path(record.workspace, record.session_id)
        try:
            return HarnessRuntime(
                self._runtime_config(record),
                self._build_llm(),
                session_path,
                resuming=session_path.exists(),
            )
        except CredentialPrerequisite:
            raise
        except (RuntimeError, ValueError, OSError) as error:
            raise SessionResumeError(str(error)) from None

    async def create_session(
        self,
        *,
        workspace: Path | str,
        mode: str,
        context_mode: str,
        title: str,
    ) -> SessionRecord:
        resolved_workspace = self._validated_workspace(workspace)
        normalized_title = title.strip()
        if not normalized_title:
            raise InvalidTitle("title must not be blank")
        async with self._runtime_lock:
            while True:
                session_id = self._new_session_id()
                if self.metadata.get_session(session_id) is not None:
                    continue
                session_path = self._session_path(resolved_workspace, session_id)
                if not session_path.exists():
                    break
            provisional = NewSession(
                session_id=session_id,
                workspace=resolved_workspace,
                title=normalized_title,
                mode=mode,
                context_mode=context_mode,
            )
            # The public RuntimeConfig contract validates constructible modes
            # before HarnessRuntime opens any session artifacts.
            self._runtime_config(provisional)
            runtime = self._construct_runtime(provisional)
            try:
                record = self.metadata.create_session(provisional)
            except BaseException:
                runtime.close()
                raise
            self._runtimes[session_id] = runtime
            return record

    async def open_runtime(self, session_id: object) -> HarnessRuntime:
        record = self._required_record(session_id)
        async with self._runtime_lock:
            existing = self._runtimes.get(record.session_id)
            if existing is not None:
                return existing
            runtime = self._construct_runtime(record)
            self._runtimes[record.session_id] = runtime
            self.metadata.touch_session(record.session_id)
            return runtime

    async def transcript(self, session_id: object) -> list[dict]:
        record = self._required_record(session_id)
        session_path = self._session_path(record.workspace, record.session_id)
        async with self._runtime_lock:
            runtime = self._runtimes.get(record.session_id)
            if runtime is not None:
                return copy.deepcopy(runtime.messages)
            try:
                lock(session_path)
            except RuntimeError as error:
                raise SessionResumeError(str(error)) from None
            try:
                return SessionLog(session_path).load()
            except (KeyError, OSError, ValueError) as error:
                raise SessionResumeError(str(error)) from None
            finally:
                unlock(session_path)

    async def safety(self, session_id: object) -> dict:
        runtime = await self.open_runtime(session_id)
        return asdict(runtime.safety_snapshot())

    def rename_session(self, session_id: object, title: str) -> SessionRecord:
        record = self._required_record(session_id)
        normalized = title.strip()
        if not normalized:
            raise InvalidTitle("title must not be blank")
        return self.metadata.rename_session(record.session_id, normalized)

    async def archive_session(self, session_id: object) -> None:
        record = self._required_record(session_id)
        async with self._runtime_lock:
            runtime = self._runtimes.get(record.session_id)
            if runtime is not None:
                runtime.close()
                self._runtimes.pop(record.session_id, None)
            self.metadata.archive_session(record.session_id)

    async def close(self) -> None:
        if self._closed:
            return
        errors: list[Exception] = []
        async with self._runtime_lock:
            try:
                for runtime in list(self._runtimes.values()):
                    try:
                        runtime.close()
                    except Exception as error:
                        errors.append(error)
            finally:
                self._runtimes.clear()
                try:
                    self.metadata.close()
                except Exception as error:
                    errors.append(error)
                self._closed = True
        if errors:
            raise ExceptionGroup("session manager close failed", errors)
