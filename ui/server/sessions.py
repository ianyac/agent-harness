"""Session discovery, runtime ownership, and authoritative transcript access."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
from datetime import UTC, datetime
import errno
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
_SESSION_COMPANION_SUFFIXES = (
    ".context-mode",
    ".folds.sqlite3",
    ".fold-decisions.jsonl",
    ".lock",
)
_CREDENTIAL_KEYS = frozenset({"tokens", "access_token", "account_id"})


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


class SessionManagerClosed(RuntimeError):
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
        if candidate.is_symlink():
            raise InvalidWorkspace("workspace must not be a symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise InvalidWorkspace("workspace must be an existing directory") from error
        if not resolved.is_dir():
            raise InvalidWorkspace("workspace must be an existing directory")
        return resolved

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionManagerClosed("session manager is closed")

    @staticmethod
    def _new_session_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        return f"{timestamp}-{secrets.token_hex(6)}"

    @staticmethod
    def _within_workspace(path: Path, workspace: Path) -> bool:
        try:
            path.relative_to(workspace)
        except ValueError:
            return False
        return True

    @classmethod
    def _safe_directory(
        cls,
        path: Path,
        workspace: Path,
        *,
        create: bool,
    ) -> Path:
        if path.is_symlink():
            raise SessionResumeError("session directory is unsafe")
        if not path.exists():
            if not create:
                raise SessionResumeError("session directory is missing")
            try:
                path.mkdir()
            except OSError as error:
                raise SessionResumeError("cannot create session directory") from error
        if not path.is_dir():
            raise SessionResumeError("session directory is unsafe")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SessionResumeError("session directory is unsafe") from error
        if not cls._within_workspace(resolved, workspace):
            raise SessionResumeError("session directory escapes workspace")
        return resolved

    @classmethod
    def _safe_sessions_dir(cls, workspace: Path | str, *, create: bool) -> Path:
        try:
            canonical = cls._validated_workspace(workspace)
        except InvalidWorkspace as error:
            raise SessionResumeError("workspace is missing or unsafe") from error
        agent_dir = cls._safe_directory(
            canonical / ".agent", canonical, create=create
        )
        return cls._safe_directory(
            agent_dir / "sessions", canonical, create=create
        )

    @classmethod
    def _safe_session_path(
        cls,
        workspace: Path | str,
        session_id: object,
        *,
        require_file: bool,
        create_parents: bool,
    ) -> Path:
        validated = validate_session_id(session_id)
        try:
            canonical = cls._validated_workspace(workspace)
        except InvalidWorkspace as error:
            raise SessionResumeError("workspace is missing or unsafe") from error
        sessions_dir = cls._safe_sessions_dir(canonical, create=create_parents)
        session_path = sessions_dir / f"{validated}.jsonl"
        if session_path.is_symlink():
            raise SessionResumeError("session file is unsafe")
        if session_path.exists():
            if not session_path.is_file():
                raise SessionResumeError("session file is unsafe")
            try:
                resolved = session_path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise SessionResumeError("session file is unsafe") from error
            if not cls._within_workspace(resolved, canonical):
                raise SessionResumeError("session file escapes workspace")
        elif require_file:
            raise SessionResumeError("session file is missing")
        for suffix in _SESSION_COMPANION_SUFFIXES:
            artifact = session_path.with_suffix(suffix)
            if artifact.is_symlink():
                raise SessionResumeError("session artifact is unsafe")
            if not artifact.exists():
                continue
            if not artifact.is_file():
                raise SessionResumeError("session artifact is unsafe")
            try:
                resolved = artifact.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise SessionResumeError("session artifact is unsafe") from error
            if not cls._within_workspace(resolved, canonical):
                raise SessionResumeError("session artifact escapes workspace")
        return session_path

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
                workspace = self._validated_workspace(record.workspace)
            except InvalidWorkspace:
                continue
            workspaces.add(workspace)
        return tuple(sorted(workspaces, key=str))

    async def discover(self) -> None:
        """Rebuild missing rows from explicitly known workspace roots only."""
        async with self._runtime_lock:
            self._ensure_open()
            known_ids = {
                record.session_id
                for record in self.metadata.list_sessions(include_archived=True)
            }
            for workspace in self._known_workspaces():
                try:
                    sessions_dir = self._safe_sessions_dir(workspace, create=False)
                except SessionResumeError:
                    continue
                for candidate in sorted(sessions_dir.glob("*.jsonl")):
                    if candidate.name.endswith(_FOLD_DECISION_SUFFIX):
                        continue
                    session_id = candidate.stem
                    try:
                        safe_path = self._safe_session_path(
                            workspace,
                            session_id,
                            require_file=True,
                            create_parents=False,
                        )
                    except (InvalidSessionId, SessionResumeError):
                        continue
                    if safe_path != candidate or session_id in known_ids:
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
        self._ensure_open()
        return self.metadata.list_sessions()

    def get_session(self, session_id: object) -> SessionRecord:
        self._ensure_open()
        return self._required_record(session_id)

    @staticmethod
    def _credential_failure(error: BaseException) -> bool:
        if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
            return True
        if isinstance(error, OSError):
            return isinstance(
                error,
                (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError),
            ) or error.errno in {
                errno.ENOENT,
                errno.EACCES,
                errno.EPERM,
                errno.EISDIR,
                errno.ENOTDIR,
            }
        if isinstance(error, KeyError):
            return len(error.args) == 1 and error.args[0] in _CREDENTIAL_KEYS
        if isinstance(error, TypeError):
            text = str(error)
            return "object is not subscriptable" in text or (
                "indices must be integers" in text and "str" in text
            )
        if isinstance(error, RuntimeError):
            text = str(error)
            return text.startswith("no codex credentials") and "codex login" in text
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
        self,
        record: NewSession | SessionRecord,
        *,
        creating: bool = False,
    ) -> HarnessRuntime:
        config = self._runtime_config(record)
        llm = self._build_llm()
        session_path = self._safe_session_path(
            record.workspace,
            record.session_id,
            require_file=False,
            create_parents=creating,
        )
        if creating and session_path.exists():
            raise SessionResumeError("generated session id already exists")
        if (
            not creating
            and not session_path.exists()
            and not session_path.with_suffix(".context-mode").exists()
        ):
            raise SessionResumeError("session file is missing")
        try:
            return HarnessRuntime(
                config,
                llm,
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
        async with self._runtime_lock:
            self._ensure_open()
            normalized_title = title.strip()
            if not normalized_title:
                raise InvalidTitle("title must not be blank")
            resolved_workspace = self._validated_workspace(workspace)
            while True:
                session_id = self._new_session_id()
                if self.metadata.get_session(session_id) is not None:
                    continue
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
            runtime = self._construct_runtime(provisional, creating=True)
            try:
                record = self.metadata.create_session(provisional)
            except BaseException as error:
                self._close_after_failure(runtime, error)
                raise
            self._runtimes[session_id] = runtime
            return record

    async def open_runtime(self, session_id: object) -> HarnessRuntime:
        async with self._runtime_lock:
            self._ensure_open()
            record = self._required_record(session_id)
            existing = self._runtimes.get(record.session_id)
            if existing is not None:
                self._safe_session_path(
                    record.workspace,
                    record.session_id,
                    require_file=False,
                    create_parents=False,
                )
                return existing
            runtime = self._construct_runtime(record)
            try:
                self.metadata.touch_session(record.session_id)
            except BaseException as error:
                self._close_after_failure(runtime, error)
                raise
            self._runtimes[record.session_id] = runtime
            return runtime

    async def transcript(self, session_id: object) -> list[dict]:
        async with self._runtime_lock:
            self._ensure_open()
            record = self._required_record(session_id)
            session_path = self._safe_session_path(
                record.workspace,
                record.session_id,
                require_file=True,
                create_parents=False,
            )
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
        self._ensure_open()
        record = self._required_record(session_id)
        normalized = title.strip()
        if not normalized:
            raise InvalidTitle("title must not be blank")
        return self.metadata.rename_session(record.session_id, normalized)

    async def archive_session(self, session_id: object) -> None:
        async with self._runtime_lock:
            self._ensure_open()
            record = self._required_record(session_id)
            runtime = self._runtimes.get(record.session_id)
            if runtime is not None:
                runtime.close()
                self._runtimes.pop(record.session_id, None)
            self.metadata.archive_session(record.session_id)

    @staticmethod
    def _close_after_failure(runtime: HarnessRuntime, original: BaseException) -> None:
        try:
            runtime.close()
        except BaseException as cleanup_error:
            original.add_note(
                "runtime cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            original.cleanup_errors = (cleanup_error,)  # type: ignore[attr-defined]

    async def close(self) -> None:
        errors: list[Exception] = []
        async with self._runtime_lock:
            if self._closed:
                return
            self._closed = True
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
        if errors:
            raise ExceptionGroup("session manager close failed", errors)
