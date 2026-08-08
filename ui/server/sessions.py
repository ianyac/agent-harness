"""Session discovery, runtime ownership, and authoritative transcript access."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, cast

from harness.llm import LLMClient
from harness.session import SessionLog
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
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_WRITE_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
)
_CREATE_READ_WRITE_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
_CREDENTIAL_MESSAGE = "Codex credentials are required. Run `codex login` and retry."


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


class CodexCredentialFactory:
    """Run credential parsing in an explicit boundary before building an LLM."""

    def __init__(
        self,
        llm_factory: Callable[[], LLMClient],
        *,
        credential_path: Path,
        read_text: Callable[[Path], str] | None = None,
    ) -> None:
        self._llm_factory = llm_factory
        self._credential_path = Path(credential_path)
        self._read_text = read_text or (lambda path: path.read_text())

    @staticmethod
    def _known_runtime_failure(error: RuntimeError) -> bool:
        text = str(error)
        return text.startswith("no codex credentials") and "codex login" in text

    def __call__(self) -> LLMClient:
        try:
            raw = self._read_text(self._credential_path)
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise TypeError("credential document must be an object")
            tokens = document["tokens"]
            if not isinstance(tokens, dict):
                raise TypeError("credential tokens must be an object")
            access_token = tokens["access_token"]
            account_id = tokens["account_id"]
            if not isinstance(access_token, str) or not access_token:
                raise TypeError("credential access token must be a non-empty string")
            if not isinstance(account_id, str) or not account_id:
                raise TypeError("credential account id must be a non-empty string")
        except RuntimeError as error:
            if not self._known_runtime_failure(error):
                raise
            raise CredentialPrerequisite(_CREDENTIAL_MESSAGE) from None
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise CredentialPrerequisite(_CREDENTIAL_MESSAGE) from None
        try:
            return self._llm_factory()
        except RuntimeError as error:
            if self._known_runtime_failure(error):
                raise CredentialPrerequisite(_CREDENTIAL_MESSAGE) from None
            raise


class _OpenedSessionPath:
    """The small Path surface SessionLog.load uses, backed by one pinned fd."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def read_text(self) -> str:
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(self._descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks).decode()

    def write_text(self, text: str) -> int:
        payload = text.encode()
        os.ftruncate(self._descriptor, 0)
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        written = 0
        while written < len(payload):
            written += os.write(self._descriptor, payload[written:])
        return written


class _DescriptorSessionLog(SessionLog):
    """SessionLog whose reads, healing, and appends stay on one opened inode."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        super().__init__(cast(Path, _OpenedSessionPath(descriptor)))

    def _append(self, payload: str) -> None:
        encoded = payload.encode()
        os.lseek(self._descriptor, 0, os.SEEK_END)
        written = 0
        while written < len(encoded):
            written += os.write(self._descriptor, encoded[written:])


class _SecureSessionLease:
    """Stable advisory lock plus a pinned authoritative session descriptor."""

    def __init__(
        self,
        directory_descriptors: tuple[int, int, int],
        session_descriptor: int,
        session_name: str,
        lock_descriptor: int,
        created_session_identity: tuple[int, int] | None,
    ) -> None:
        self._directory_descriptors = list(directory_descriptors)
        self._session_descriptor: int | None = session_descriptor
        self._session_name = session_name
        self._lock_descriptor: int | None = lock_descriptor
        self._created_session_identity = created_session_identity
        self.session_log = _DescriptorSessionLog(session_descriptor)

    @staticmethod
    def _identity(descriptor: int) -> tuple[int, int]:
        opened = os.fstat(descriptor)
        return opened.st_dev, opened.st_ino

    def _close_session(self) -> None:
        if self._session_descriptor is None:
            return
        os.close(self._session_descriptor)
        self._session_descriptor = None

    def _remove_created_session(self) -> None:
        identity = self._created_session_identity
        if identity is None:
            return
        sessions_descriptor = self._directory_descriptors[-1]
        try:
            current = os.open(
                self._session_name,
                _READ_FLAGS,
                dir_fd=sessions_descriptor,
            )
        except FileNotFoundError:
            self._created_session_identity = None
            return
        except OSError:
            # The name no longer resolves to the inode this lease created.
            # Preserve the replacement and relinquish deletion ownership.
            self._created_session_identity = None
            return
        try:
            if self._identity(current) == identity:
                os.unlink(self._session_name, dir_fd=sessions_descriptor)
        finally:
            os.close(current)
        self._created_session_identity = None

    def _release_lock(self) -> None:
        if self._lock_descriptor is None:
            return
        os.ftruncate(self._lock_descriptor, 0)
        os.lseek(self._lock_descriptor, 0, os.SEEK_SET)
        os.close(self._lock_descriptor)
        self._lock_descriptor = None

    def _close_directories(self) -> None:
        while self._directory_descriptors:
            descriptor = self._directory_descriptors[-1]
            os.close(descriptor)
            self._directory_descriptors.pop()

    def close(self) -> None:
        self._close_session()
        self._release_lock()
        self._close_directories()

    def abort(self) -> None:
        self._close_session()
        self._remove_created_session()
        self._release_lock()
        self._close_directories()


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
    def _close_descriptors(descriptors: tuple[int, ...] | list[int]) -> None:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    @classmethod
    def _open_session_directory_descriptors(
        cls, workspace: Path | str
    ) -> tuple[int, int, int]:
        try:
            canonical = cls._validated_workspace(workspace)
        except InvalidWorkspace as error:
            raise SessionResumeError("workspace is missing or unsafe") from error
        opened: list[int] = []
        try:
            workspace_descriptor = os.open(canonical, _DIRECTORY_FLAGS)
            opened.append(workspace_descriptor)
            agent_descriptor = os.open(
                ".agent", _DIRECTORY_FLAGS, dir_fd=workspace_descriptor
            )
            opened.append(agent_descriptor)
            sessions_descriptor = os.open(
                "sessions", _DIRECTORY_FLAGS, dir_fd=agent_descriptor
            )
            opened.append(sessions_descriptor)
            for descriptor in opened:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise SessionResumeError("session directory is unsafe")
            return cast(tuple[int, int, int], tuple(opened))
        except SessionResumeError:
            cls._close_descriptors(opened)
            raise
        except OSError as error:
            cls._close_descriptors(opened)
            raise SessionResumeError("session directory is missing or unsafe") from error

    @staticmethod
    def _identity(descriptor: int) -> tuple[int, int]:
        opened = os.fstat(descriptor)
        return opened.st_dev, opened.st_ino

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _acquire_secure_lock(cls, sessions_descriptor: int, session_id: str) -> int:
        try:
            descriptor = os.open(
                f"{session_id}.lock",
                _LOCK_FLAGS,
                0o600,
                dir_fd=sessions_descriptor,
            )
        except OSError as error:
            raise SessionResumeError("session lock is unsafe") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionResumeError("session lock is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SessionResumeError("session is already in use") from error
            os.lseek(descriptor, 0, os.SEEK_SET)
            raw_holder = os.read(descriptor, 128).decode().strip()
            try:
                holder = int(raw_holder)
            except ValueError:
                holder = None
            if holder is not None and cls._pid_alive(holder):
                raise SessionResumeError("session is already in use")
            payload = str(os.getpid()).encode()
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def _acquire_session_lease(
        cls,
        workspace: Path,
        session_id: str,
        *,
        create_session: bool,
    ) -> _SecureSessionLease:
        directory_descriptors = cls._open_session_directory_descriptors(workspace)
        sessions_descriptor = directory_descriptors[-1]
        lock_descriptor: int | None = None
        session_descriptor: int | None = None
        created_identity: tuple[int, int] | None = None
        try:
            lock_descriptor = cls._acquire_secure_lock(
                sessions_descriptor, session_id
            )
            flags = (
                _CREATE_READ_WRITE_FLAGS
                if create_session
                else _READ_WRITE_FLAGS
            )
            try:
                session_descriptor = os.open(
                    f"{session_id}.jsonl",
                    flags,
                    0o600,
                    dir_fd=sessions_descriptor,
                )
            except FileExistsError as error:
                raise SessionResumeError(
                    "generated session id already exists"
                ) from error
            except OSError as error:
                raise SessionResumeError(
                    "session file is missing or unsafe"
                ) from error
            if not stat.S_ISREG(os.fstat(session_descriptor).st_mode):
                raise SessionResumeError("session file is unsafe")
            if create_session:
                created_identity = cls._identity(session_descriptor)
            lease = _SecureSessionLease(
                directory_descriptors,
                session_descriptor,
                f"{session_id}.jsonl",
                lock_descriptor,
                created_identity,
            )
            session_descriptor = None
            lock_descriptor = None
            return lease
        except BaseException:
            if session_descriptor is not None:
                os.close(session_descriptor)
            if lock_descriptor is not None:
                try:
                    os.ftruncate(lock_descriptor, 0)
                finally:
                    os.close(lock_descriptor)
            cls._close_descriptors(directory_descriptors)
            raise

    @classmethod
    def _load_verified_transcript(
        cls, workspace: Path, session_id: str
    ) -> list[dict]:
        lease = cls._acquire_session_lease(
            workspace,
            session_id,
            create_session=False,
        )
        try:
            return lease.session_log.load()
        finally:
            lease.close()

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

    def _build_llm(self) -> LLMClient:
        return self._llm_factory()

    def _runtime_config(self, record: NewSession | SessionRecord) -> RuntimeConfig:
        return RuntimeConfig(
            session_id=record.session_id,
            workspace=record.workspace,
            mode=record.mode,
            context_mode=record.context_mode,
            compact_threshold=self._compact_threshold,
        )

    @staticmethod
    def _instantiate_runtime(
        config: RuntimeConfig,
        llm: LLMClient,
        session_path: Path,
        session_lease: _SecureSessionLease,
        *,
        resuming: bool,
    ) -> HarnessRuntime:
        try:
            return HarnessRuntime(
                config,
                llm,
                session_path,
                resuming=resuming,
                session_lease=session_lease,
            )
        except (RuntimeError, ValueError, OSError) as error:
            cleanup = getattr(error, "runtime_abort", session_lease.abort)
            cleanup_errors: list[BaseException] = []
            for _attempt in range(2):
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    continue
                break
            else:
                cleanup_error = cleanup_errors[-1]
                error.add_note(
                    "runtime construction cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise SessionResumeError(str(error)) from None

    def _construct_runtime(
        self,
        record: NewSession | SessionRecord,
    ) -> HarnessRuntime:
        config = self._runtime_config(record)
        llm = self._build_llm()
        session_path = self._safe_session_path(
            record.workspace,
            record.session_id,
            require_file=True,
            create_parents=False,
        )
        lease = self._acquire_session_lease(
            record.workspace,
            record.session_id,
            create_session=False,
        )
        return self._instantiate_runtime(
            config,
            llm,
            session_path,
            lease,
            resuming=True,
        )

    def _construct_new_runtime(
        self, record: NewSession
    ) -> HarnessRuntime:
        config = self._runtime_config(record)
        llm = self._build_llm()
        session_path = self._safe_session_path(
            record.workspace,
            record.session_id,
            require_file=False,
            create_parents=True,
        )
        if session_path.exists():
            raise SessionResumeError("generated session id already exists")
        lease = self._acquire_session_lease(
            record.workspace,
            record.session_id,
            create_session=True,
        )
        return self._instantiate_runtime(
            config,
            llm,
            session_path,
            lease,
            resuming=False,
        )

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
            runtime = self._construct_new_runtime(provisional)
            try:
                record = self.metadata.create_session(provisional)
            except BaseException as error:
                self._abort_after_failure(runtime, error)
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
                self._abort_after_failure(runtime, error)
                raise
            self._runtimes[record.session_id] = runtime
            return runtime

    async def transcript(self, session_id: object) -> list[dict]:
        async with self._runtime_lock:
            self._ensure_open()
            record = self._required_record(session_id)
            self._safe_session_path(
                record.workspace,
                record.session_id,
                require_file=True,
                create_parents=False,
            )
            runtime = self._runtimes.get(record.session_id)
            if runtime is not None:
                return copy.deepcopy(runtime.messages)
            try:
                return self._load_verified_transcript(
                    record.workspace, record.session_id
                )
            except SessionResumeError:
                raise
            except (KeyError, OSError, ValueError) as error:
                raise SessionResumeError(str(error)) from None

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
    def _abort_after_failure(
        runtime: HarnessRuntime,
        original: BaseException,
    ) -> None:
        cleanup_errors: list[BaseException] = []
        operation = getattr(runtime, "abort", runtime.close)
        for _attempt in range(2):
            try:
                operation()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
                continue
            return
        for cleanup_error in cleanup_errors:
            original.add_note(
                "runtime abort failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        existing = tuple(getattr(original, "cleanup_errors", ()))
        original.cleanup_errors = (  # type: ignore[attr-defined]
            *existing,
            *cleanup_errors,
        )

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
