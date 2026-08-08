"""Session discovery, runtime ownership, and authoritative transcript access."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import sqlite3
import stat
import struct
import sys
import threading
from typing import Callable, cast
import uuid

from harness.llm import LLMClient
from harness.permissions import STARTUP_MODES
from harness.session import SessionLog
from server.bridge import CancellationToken, DecisionBroker, EventSink
from server.metadata import MetadataStore, NewSession, SessionRecord
from server.protocol import (
    CancelTurn,
    ClearQueuedMessage,
    ClientEvent,
    PermissionAnswer,
    PlanAnswer,
    QueuedMessage,
    SetSessionMode,
    UserMessage,
    validate_unicode_scalars,
)
from server.runner import TurnRunner
from server.runtime import HarnessRuntime, RuntimeConfig


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FOLD_DECISION_SUFFIX = ".fold-decisions.jsonl"
_SESSION_COMPANION_SUFFIXES = (
    ".context-mode",
    ".folds.sqlite3",
    ".fold-decisions.jsonl",
    ".folds.sqlite3-journal",
    ".folds.sqlite3-wal",
    ".folds.sqlite3-shm",
    ".lock",
)
_STAGED_CONTEXT_SUFFIXES = _SESSION_COMPANION_SUFFIXES[:-1]
_SQLITE_RECOVERY_SUFFIXES = (
    ".folds.sqlite3-journal",
    ".folds.sqlite3-wal",
    ".folds.sqlite3-shm",
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
_PROCESS_LEASES_GUARD = threading.Lock()
_PROCESS_LEASES: dict[tuple[int, int, int, str], object] = {}


def _coordination_root_path() -> Path:
    """Return an environment-independent per-user coordination domain."""

    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    if not home.is_absolute():
        raise SessionResumeError("session coordination home is unsafe")
    if sys.platform == "darwin":
        parent = home / "Library" / "Caches"
    else:
        parent = home / ".cache"
    return parent / "agent-harness-ui-session-locks-v1"


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


class ClientStateViolation(ValueError):
    """A valid client event that is not legal in the current turn state."""


class _EventRelay:
    """Route worker events only to the current connection generation."""

    _TERMINAL_TYPES = frozenset(
        {"turn_completed", "turn_cancelled", "turn_failed"}
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target: EventSink | None = None
        self._terminal: tuple[str, dict[str, object]] | None = None

    def bind_with_snapshot(self, target: EventSink, **snapshot: object) -> None:
        with self._lock:
            target.emit("session_snapshot", **snapshot)
            self._target = target

    def unbind(self, target: EventSink) -> None:
        with self._lock:
            if self._target is target:
                self._target = None

    def emit(self, event_type: str, **payload: object) -> None:
        with self._lock:
            if event_type in self._TERMINAL_TYPES:
                if self._terminal is not None:
                    raise RuntimeError("a turn terminal event is already pending")
                self._terminal = (event_type, payload)
                return
            target = self._target
        if target is not None:
            target.emit(event_type, **payload)

    def flush_terminal(self) -> None:
        with self._lock:
            terminal = self._terminal
            self._terminal = None
            target = self._target
        if terminal is not None and target is not None:
            event_type, payload = terminal
            target.emit(event_type, **payload)


class SessionConnection:
    """One authenticated WebSocket generation attached to a live session."""

    def __init__(
        self,
        channel: _SessionChannel,
        generation: int,
        sink: EventSink,
    ) -> None:
        self._channel = channel
        self.generation = generation
        self.sink = sink
        self.stop = asyncio.Event()
        self.superseded = False

    @property
    def session_id(self) -> str:
        return self._channel.session_id

    async def next_event(self):
        return await self.sink.next()

    def dispatch(self, event: ClientEvent) -> None:
        self._channel.dispatch(self, event)

    def mark_superseded(self) -> None:
        self.superseded = True
        self.stop.set()

    def disconnected(self) -> None:
        self.stop.set()
        self._channel.release(self)


class _SessionChannel:
    """Session-scoped connection, turn, decision, and queue ownership."""

    def __init__(
        self,
        session_id: str,
        runtime: HarnessRuntime,
        metadata: MetadataStore,
    ) -> None:
        self.session_id = session_id
        self.runtime = runtime
        self.metadata = metadata
        self.generation = 0
        self.current: SessionConnection | None = None
        self.relay = _EventRelay()
        self.messages = copy.deepcopy(runtime.messages)
        self.safety = asdict(runtime.safety_snapshot())
        self.running = False
        self.stopping = False
        self.queued_message: QueuedMessage | None = None
        self.turn_id: str | None = None
        self.turn_owner_generation: int | None = None
        self.token: CancellationToken | None = None
        self.runner: TurnRunner | None = None
        self.worker: asyncio.Task[None] | None = None
        self.shutting_down = False
        self.lifecycle = "active"

    def connect(self, loop: asyncio.AbstractEventLoop) -> SessionConnection:
        if getattr(self.runtime, "_ui_durability_failed", False):
            raise SessionResumeError("session transcript authority is unavailable")
        if self.shutting_down:
            raise SessionManagerClosed("session is shutting down")
        previous = self.current
        self.generation += 1
        sink = EventSink(self.session_id, self.generation, loop)
        connection = SessionConnection(self, self.generation, sink)
        self.current = connection
        if previous is not None:
            previous.mark_superseded()
        if self.running and self.runner is not None:
            self.runner.decisions.disconnect()
            self.runner.decisions = DecisionBroker()
            self.turn_owner_generation = connection.generation
        self.relay.bind_with_snapshot(
            sink,
            messages=copy.deepcopy(self.messages),
            running=self.running,
            turn_id=self.turn_id,
            queued_message=(
                self.queued_message.model_copy(deep=True)
                if self.queued_message is not None
                else None
            ),
            safety=copy.deepcopy(self.safety),
        )
        return connection

    def release(self, connection: SessionConnection) -> None:
        if self.current is connection:
            self.current = None
            self.relay.unbind(connection.sink)
        if (
            self.running
            and self.turn_owner_generation == connection.generation
            and self.runner is not None
        ):
            self.runner.decisions.disconnect()

    def dispatch(self, connection: SessionConnection, event: ClientEvent) -> None:
        if self.current is not connection or self.shutting_down:
            raise ClientStateViolation("connection generation is no longer current")
        if isinstance(event, UserMessage):
            if self.running:
                raise ClientStateViolation("a turn is already running")
            self._start_turn(event.text, event.mode, connection.generation)
        elif isinstance(event, QueuedMessage):
            if not self.running:
                raise ClientStateViolation("a follow-up requires a running turn")
            self.queued_message = event.model_copy(deep=True)
        elif isinstance(event, ClearQueuedMessage):
            self.queued_message = None
        elif isinstance(event, CancelTurn):
            if (
                self.running
                and event.turn_id == self.turn_id
                and self.token is not None
                and not self.stopping
            ):
                self.stopping = True
                self.relay.emit("turn_stopping", turn_id=self.turn_id)
                self.token.cancel()
        elif isinstance(event, PermissionAnswer):
            if self.runner is not None:
                self.runner.decisions.answer_permission(
                    event.request_id, event.answer
                )
        elif isinstance(event, PlanAnswer):
            if self.runner is not None:
                self.runner.decisions.answer_plan(
                    event.request_id, event.approved, event.feedback
                )
        elif isinstance(event, SetSessionMode):
            self._set_session_mode(event.mode)
        else:
            raise ClientStateViolation("unsupported client event")

    def _set_session_mode(self, mode: str) -> None:
        if mode not in STARTUP_MODES:
            raise ClientStateViolation("invalid base mode")
        self.metadata.set_session_mode(self.session_id, mode)
        self.runtime.policy.base_mode = mode
        if not self.running:
            self.runtime.policy.mode = mode
        self.safety = asdict(self.runtime.safety_snapshot())
        self.relay.emit("safety_updated", safety=copy.deepcopy(self.safety))

    def _start_turn(
        self,
        text: str,
        mode: str,
        owner_generation: int | None,
    ) -> None:
        if (
            self.running
            or self.shutting_down
            or getattr(self.runtime, "_ui_durability_failed", False)
        ):
            raise ClientStateViolation("a turn cannot start in the current state")
        turn_id = uuid.uuid4().hex
        token = CancellationToken()
        runner = TurnRunner(self.runtime)
        if owner_generation is None:
            runner.decisions.disconnect()
        self.running = True
        self.stopping = False
        self.turn_id = turn_id
        self.turn_owner_generation = owner_generation
        self.token = token
        self.runner = runner
        self.worker = asyncio.create_task(
            self._run_turn(runner, text, mode, turn_id, token),
            name=f"session-turn-{self.session_id}-{turn_id}",
        )

    async def _run_turn(
        self,
        runner: TurnRunner,
        text: str,
        mode: str,
        turn_id: str,
        token: CancellationToken,
    ) -> None:
        try:
            await asyncio.to_thread(
                runner.run,
                text,
                mode,
                turn_id,
                self.relay,
                token,
            )
        finally:
            durability_failed = getattr(
                self.runtime, "_ui_durability_failed", False
            )
            if durability_failed:
                self.lifecycle = "durability_failed"
                self.shutting_down = True
                self.queued_message = None
            else:
                self.messages = copy.deepcopy(self.runtime.messages)
            self.safety = asdict(self.runtime.safety_snapshot())
            self.running = False
            self.stopping = False
            self.turn_id = None
            self.turn_owner_generation = None
            self.token = None
            self.runner = None
            self.worker = None
            self.relay.flush_terminal()
            queued = self.queued_message
            self.queued_message = None
            if queued is not None and not self.shutting_down:
                owner = self.current.generation if self.current is not None else None
                self._start_turn(queued.text, queued.mode, owner)

    def begin_shutdown(self) -> None:
        if self.lifecycle == "active":
            self.lifecycle = "draining"
        self.shutting_down = True
        self.queued_message = None
        if self.current is not None:
            self.current.mark_superseded()
            self.relay.unbind(self.current.sink)
            self.current = None
        if self.runner is not None:
            self.runner.decisions.disconnect()
        if self.token is not None:
            self.token.cancel()

    def mark_cleanup_failed(self) -> None:
        self.lifecycle = "cleanup_failed"
        self.shutting_down = True

    async def wait_for_worker(self) -> None:
        worker = self.worker
        if worker is not None:
            await asyncio.shield(worker)


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

    def replace_descriptor(self, descriptor: int) -> None:
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
        self._io_lock = threading.RLock()
        self._descriptor = descriptor
        self._opened_path = _OpenedSessionPath(descriptor)
        super().__init__(cast(Path, self._opened_path))

    def replace_descriptor(self, descriptor: int) -> None:
        with self._io_lock:
            self._descriptor = descriptor
            self._opened_path.replace_descriptor(descriptor)

    def load(self) -> list[dict]:
        with self._io_lock:
            messages = super().load()
            validate_unicode_scalars(messages)
            return messages

    def record_turn(self, messages: list[dict]) -> None:
        with self._io_lock:
            validate_unicode_scalars(messages)
            super().record_turn(messages)

    def record_compaction(self, cut: int, summary: dict) -> None:
        with self._io_lock:
            validate_unicode_scalars(summary)
            super().record_compaction(cut, summary)

    def _append(self, payload: str) -> None:
        with self._io_lock:
            encoded = payload.encode()
            os.lseek(self._descriptor, 0, os.SEEK_END)
            written = 0
            while written < len(encoded):
                written += os.write(self._descriptor, encoded[written:])


@dataclass
class _Publication:
    name: str
    identity: tuple[int, int]
    backup_name: str | None = None
    capture_name: str | None = None


@dataclass
class _ProcessLeaseClaim:
    """One in-process workspace/session owner layered over advisory locking."""

    key: tuple[int, int, int, str]
    token: object
    released: bool = False

    @classmethod
    def acquire(
        cls,
        workspace_descriptor: int,
        session_id: str,
    ) -> _ProcessLeaseClaim:
        workspace = os.fstat(workspace_descriptor)
        key = (os.getpid(), workspace.st_dev, workspace.st_ino, session_id)
        token = object()
        with _PROCESS_LEASES_GUARD:
            if key in _PROCESS_LEASES:
                raise SessionResumeError("session is already in use")
            _PROCESS_LEASES[key] = token
        return cls(key, token)

    def release(self) -> None:
        if self.released:
            return
        with _PROCESS_LEASES_GUARD:
            current = _PROCESS_LEASES.get(self.key)
            if current is self.token:
                del _PROCESS_LEASES[self.key]
            elif current is not None:
                raise RuntimeError("process session lease ownership changed")
        self.released = True


@dataclass
class _CoordinationLeaseClaim:
    """OS-backed authority for one canonical workspace/session pair."""

    descriptor: int | None
    authority_descriptor: int | None

    @staticmethod
    def _acquire_authority(
        workspace: os.stat_result,
        session_id: str,
    ) -> int:
        material = (
            f"ofd-v1\0{os.geteuid()}\0{workspace.st_dev}\0"
            f"{workspace.st_ino}\0{session_id}"
        ).encode("utf-8")
        offset = int.from_bytes(
            hashlib.sha256(material).digest()[:8], "big"
        ) & ((1 << 63) - 1)
        command = getattr(fcntl, "F_OFD_SETLK", None)
        if command is None or sys.platform != "darwin":
            raise SessionResumeError(
                "session coordination authority is unavailable"
            )

        device_descriptor: int | None = None
        descriptor: int | None = None

        def close_owned_descriptors(primary: BaseException) -> None:
            cleanup_errors: list[BaseException] = []
            for open_descriptor in (descriptor, device_descriptor):
                if open_descriptor is None:
                    continue
                for _attempt in range(2):
                    try:
                        os.close(open_descriptor)
                    except BaseException as error:
                        cleanup_errors.append(error)
                        continue
                    break
            if cleanup_errors:
                primary.add_note(
                    "coordination authority cleanup encountered: "
                    + "; ".join(
                        f"{type(error).__name__}: {error}"
                        for error in cleanup_errors
                    )
                )
                existing = tuple(getattr(primary, "cleanup_errors", ()))
                primary.cleanup_errors = (  # type: ignore[attr-defined]
                    *existing,
                    *cleanup_errors,
                )

        try:
            device_descriptor = os.open("/dev", _DIRECTORY_FLAGS)
            device = os.fstat(device_descriptor)
            if (
                not stat.S_ISDIR(device.st_mode)
                or device.st_uid != 0
                or device.st_mode & 0o022
            ):
                raise SessionResumeError(
                    "session coordination authority is unsafe"
                )
            descriptor = os.open(
                "null",
                _READ_WRITE_FLAGS,
                dir_fd=device_descriptor,
            )
            opened = os.fstat(descriptor)
            anchor = os.stat(
                "null",
                dir_fd=device_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISCHR(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_nlink != 1
                or not stat.S_ISCHR(anchor.st_mode)
                or (anchor.st_dev, anchor.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise SessionResumeError(
                    "session coordination authority is unsafe"
                )
            lock = struct.pack(
                "@qqihh",
                offset,
                1,
                0,
                fcntl.F_WRLCK,
                os.SEEK_SET,
            )
            try:
                fcntl.fcntl(descriptor, command, lock)
            except BlockingIOError as error:
                raise SessionResumeError("session is already in use") from error
        except SessionResumeError as primary:
            close_owned_descriptors(primary)
            raise
        except (OSError, struct.error) as error:
            primary = SessionResumeError(
                "session coordination authority is unavailable"
            )
            close_owned_descriptors(primary)
            raise primary from error
        except BaseException as primary:
            close_owned_descriptors(primary)
            raise

        try:
            os.close(device_descriptor)
        except BaseException as primary:
            close_owned_descriptors(primary)
            raise
        device_descriptor = None
        result = descriptor
        descriptor = None
        return result

    @staticmethod
    def _open_root() -> int:
        root = _coordination_root_path()
        try:
            root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_descriptor = os.open(root.parent, _DIRECTORY_FLAGS)
        except OSError as error:
            raise SessionResumeError(
                "session coordination root is unavailable or unsafe"
            ) from error
        try:
            try:
                os.mkdir(root.name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            try:
                descriptor = os.open(
                    root.name,
                    _DIRECTORY_FLAGS,
                    dir_fd=parent_descriptor,
                )
            except OSError as error:
                raise SessionResumeError(
                    "session coordination root is unavailable or unsafe"
                ) from error
        finally:
            os.close(parent_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
        ):
            os.close(descriptor)
            raise SessionResumeError("session coordination root is unsafe")
        try:
            os.fchmod(descriptor, 0o700)
        except OSError:
            os.close(descriptor)
            raise
        return descriptor

    @classmethod
    def acquire(
        cls,
        workspace_descriptor: int,
        session_id: str,
    ) -> _CoordinationLeaseClaim:
        workspace = os.fstat(workspace_descriptor)
        material = (
            f"v1\0{workspace.st_dev}\0{workspace.st_ino}\0{session_id}"
        ).encode("utf-8")
        name = f"{hashlib.sha256(material).hexdigest()}.lock"
        authority_descriptor = cls._acquire_authority(workspace, session_id)
        root_descriptor: int | None = None
        descriptor: int | None = None
        try:
            root_descriptor = cls._open_root()
            try:
                descriptor = os.open(
                    name,
                    _LOCK_FLAGS,
                    0o600,
                    dir_fd=root_descriptor,
                )
            except OSError as error:
                raise SessionResumeError(
                    "session coordination lock is unavailable or unsafe"
                ) from error
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
            ):
                raise SessionResumeError("session coordination lock is unsafe")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SessionResumeError("session is already in use") from error
            anchor = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(anchor.st_mode)
                or (anchor.st_dev, anchor.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise SessionResumeError("session coordination lock changed")
            payload = (
                f"pid={os.getpid()} workspace={workspace.st_dev}:{workspace.st_ino} "
                f"session={session_id}\n"
            ).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.close(root_descriptor)
            root_descriptor = None
            claim = cls(descriptor, authority_descriptor)
            descriptor = None
            authority_descriptor = None
            return claim
        except BaseException as primary:
            cleanup_errors: list[BaseException] = []
            for open_descriptor in (
                descriptor,
                root_descriptor,
                authority_descriptor,
            ):
                if open_descriptor is None:
                    continue
                for _attempt in range(2):
                    try:
                        os.close(open_descriptor)
                    except BaseException as error:
                        cleanup_errors.append(error)
                        continue
                    break
            if cleanup_errors:
                primary.add_note(
                    "coordination acquisition cleanup encountered: "
                    + "; ".join(
                        f"{type(error).__name__}: {error}"
                        for error in cleanup_errors
                    )
                )
                existing = tuple(getattr(primary, "cleanup_errors", ()))
                primary.cleanup_errors = (  # type: ignore[attr-defined]
                    *existing,
                    *cleanup_errors,
                )
            raise

    def release(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self.authority_descriptor is not None:
            os.close(self.authority_descriptor)
            self.authority_descriptor = None


class _SecureSessionLease:
    """Private live artifacts plus atomic public publication and one lock."""

    def __init__(
        self,
        directory_descriptors: tuple[int, int, int],
        stage_descriptor: int,
        stage_path: Path,
        session_descriptor: int,
        session_name: str,
        session_anchor_name: str,
        lock_descriptor: int,
        lock_name: str,
        process_claim: _ProcessLeaseClaim,
        coordination_claim: _CoordinationLeaseClaim,
    ) -> None:
        self._directory_descriptors = list(directory_descriptors)
        self._stage_descriptor: int | None = stage_descriptor
        self._stage_path = stage_path
        self._stage_name: str | None = stage_path.name
        self._stage_directory_identity: tuple[int, int] | None = self._identity(
            stage_descriptor
        )
        self._session_descriptor: int | None = session_descriptor
        self._session_name = session_name
        self._session_anchor_name = session_anchor_name
        self._lock_descriptor: int | None = lock_descriptor
        self._lock_name = lock_name
        self._process_claim: _ProcessLeaseClaim | None = process_claim
        self._coordination_claim: _CoordinationLeaseClaim | None = (
            coordination_claim
        )
        self._publications: dict[str, _Publication] = {}
        self._committed = False
        self.session_path = stage_path / session_name
        self.session_log = _DescriptorSessionLog(session_descriptor)

    @staticmethod
    def _identity(descriptor: int) -> tuple[int, int]:
        opened = os.fstat(descriptor)
        return opened.st_dev, opened.st_ino

    @staticmethod
    def _descriptor_path(descriptor: int) -> Path:
        raw = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))

    @staticmethod
    def _copy_descriptor(source: int, destination: int) -> None:
        os.ftruncate(destination, 0)
        os.lseek(source, 0, os.SEEK_SET)
        os.lseek(destination, 0, os.SEEK_SET)
        while chunk := os.read(source, 64 * 1024):
            written = 0
            while written < len(chunk):
                written += os.write(destination, chunk[written:])

    def _close_session(self) -> None:
        if self._session_descriptor is None:
            return
        os.close(self._session_descriptor)
        self._session_descriptor = None

    @property
    def _sessions_descriptor(self) -> int:
        return self._directory_descriptors[-1]

    @property
    def _owned_stage_descriptor(self) -> int:
        if self._stage_descriptor is None:
            raise RuntimeError("session stage is closed")
        return self._stage_descriptor

    def _private_name(self, purpose: str, name: str) -> str:
        return f".{purpose}-{Path(name).name}-{secrets.token_hex(8)}"

    def _stage_identity(self, name: str) -> tuple[int, int] | None:
        try:
            descriptor = os.open(
                name,
                _READ_FLAGS,
                dir_fd=self._owned_stage_descriptor,
            )
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionResumeError("private session artifact is unsafe")
            return self._identity(descriptor)
        finally:
            os.close(descriptor)

    def _backup_public(
        self,
        name: str,
        identity: tuple[int, int],
    ) -> tuple[str | None, bool]:
        backup_name = self._private_name("backup", name)
        try:
            os.link(
                name,
                backup_name,
                src_dir_fd=self._sessions_descriptor,
                dst_dir_fd=self._owned_stage_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None, False
        except OSError as error:
            raise SessionResumeError("session artifact publication is unsafe") from error
        backup = os.stat(
            backup_name,
            dir_fd=self._owned_stage_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISREG(backup.st_mode) and (backup.st_dev, backup.st_ino) == identity:
            os.unlink(backup_name, dir_fd=self._owned_stage_descriptor)
            return None, True
        return backup_name, False

    def _publish_artifact(self, name: str, identity: tuple[int, int]) -> None:
        backup_name, already_published = self._backup_public(name, identity)
        if already_published:
            publication = self._publications.get(name)
            if publication is None:
                self._publications[name] = _Publication(name, identity)
            else:
                publication.identity = identity
            return
        publication = _Publication(name, identity, backup_name)
        self._publications[name] = publication
        publish_name = self._private_name("publish", name)
        try:
            os.link(
                name,
                publish_name,
                src_dir_fd=self._owned_stage_descriptor,
                dst_dir_fd=self._sessions_descriptor,
                follow_symlinks=False,
            )
            os.replace(
                publish_name,
                name,
                src_dir_fd=self._sessions_descriptor,
                dst_dir_fd=self._sessions_descriptor,
            )
        except BaseException:
            try:
                os.unlink(publish_name, dir_fd=self._sessions_descriptor)
            except OSError:
                pass
            raise
        if self._committed and publication.backup_name is not None:
            self._discard_backup(publication)

    def _discard_backup(self, publication: _Publication) -> None:
        if publication.backup_name is None:
            return
        try:
            os.unlink(
                publication.backup_name,
                dir_fd=self._owned_stage_descriptor,
            )
        except FileNotFoundError:
            publication.backup_name = None
        except OSError:
            # The private stage remains owned until close, which retries by
            # removing every remaining name. Publication is already durable.
            return
        else:
            publication.backup_name = None

    def _publish_initial_session(self) -> None:
        identity = self._stage_identity(self._session_name)
        if identity is None:
            raise SessionResumeError("private session file is missing")
        try:
            os.link(
                self._session_name,
                self._session_name,
                src_dir_fd=self._owned_stage_descriptor,
                dst_dir_fd=self._sessions_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise SessionResumeError("generated session id already exists") from error
        self._publications[self._session_name] = _Publication(
            self._session_name,
            identity,
        )

    def _restore_private(self, private_name: str, public_name: str) -> None:
        try:
            os.link(
                private_name,
                public_name,
                src_dir_fd=self._owned_stage_descriptor,
                dst_dir_fd=self._sessions_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            private = os.stat(
                private_name,
                dir_fd=self._owned_stage_descriptor,
                follow_symlinks=False,
            )
            public = os.stat(
                public_name,
                dir_fd=self._sessions_descriptor,
                follow_symlinks=False,
            )
            if (private.st_dev, private.st_ino) != (
                public.st_dev,
                public.st_ino,
            ):
                raise OSError(
                    f"cannot restore replacement at {public_name}"
                ) from error
        os.unlink(private_name, dir_fd=self._owned_stage_descriptor)

    def _rollback_publication(self, publication: _Publication) -> None:
        capture_name = publication.capture_name
        if capture_name is None:
            candidate = self._private_name("quarantine", publication.name)
            try:
                os.rename(
                    publication.name,
                    candidate,
                    src_dir_fd=self._sessions_descriptor,
                    dst_dir_fd=self._owned_stage_descriptor,
                )
            except FileNotFoundError:
                pass
            else:
                publication.capture_name = candidate
                capture_name = candidate
        if capture_name is not None:
            try:
                captured = os.stat(
                    capture_name,
                    dir_fd=self._owned_stage_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                publication.capture_name = None
            else:
                captured_identity = (captured.st_dev, captured.st_ino)
                if (
                    stat.S_ISREG(captured.st_mode)
                    and captured_identity == publication.identity
                ):
                    os.unlink(capture_name, dir_fd=self._owned_stage_descriptor)
                    publication.capture_name = None
                else:
                    self._restore_private(capture_name, publication.name)
                    publication.capture_name = None
                    if publication.backup_name is not None:
                        self._discard_backup(publication)
                    return
        if publication.backup_name is not None:
            self._restore_private(publication.backup_name, publication.name)

    def _rollback_publications(self) -> None:
        errors: list[BaseException] = []
        for name, publication in reversed(tuple(self._publications.items())):
            try:
                self._rollback_publication(publication)
            except BaseException as error:
                errors.append(error)
            else:
                self._publications.pop(name, None)
        if errors:
            raise ExceptionGroup("session publication rollback failed", errors)

    def _restore_lock_name(self) -> None:
        probe_name = self._private_name("lock-probe", self._lock_name)
        try:
            os.link(
                self._lock_name,
                probe_name,
                src_dir_fd=self._sessions_descriptor,
                dst_dir_fd=self._owned_stage_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            probe = os.stat(
                probe_name,
                dir_fd=self._owned_stage_descriptor,
                follow_symlinks=False,
            )
            os.unlink(probe_name, dir_fd=self._owned_stage_descriptor)
            if stat.S_ISREG(probe.st_mode) and (
                probe.st_dev,
                probe.st_ino,
            ) == self._identity(self._lock_descriptor):
                return
        publish_name = self._private_name("lock", self._lock_name)
        try:
            os.link(
                self._lock_name,
                publish_name,
                src_dir_fd=self._owned_stage_descriptor,
                dst_dir_fd=self._sessions_descriptor,
                follow_symlinks=False,
            )
            os.replace(
                publish_name,
                self._lock_name,
                src_dir_fd=self._sessions_descriptor,
                dst_dir_fd=self._sessions_descriptor,
            )
        except FileNotFoundError:
            return
        except BaseException:
            try:
                os.unlink(publish_name, dir_fd=self._sessions_descriptor)
            except OSError:
                pass
            raise

    def publish(self) -> None:
        """Atomically expose staged runtime artifacts under documented names."""
        self._restore_lock_name()
        session_id = Path(self._session_name).stem
        names = (
            self._session_name,
            f"{session_id}.context-mode",
            f"{session_id}.folds.sqlite3",
            f"{session_id}.fold-decisions.jsonl",
        )
        for name in names:
            identity = self._stage_identity(name)
            if identity is not None:
                self._publish_artifact(name, identity)

    def reconcile_artifacts(self) -> None:
        """Atomically publish path-replaced private files to the live log."""
        if self._session_descriptor is None:
            raise RuntimeError("session descriptor is closed")
        pinned_identity = self._identity(self._session_descriptor)
        staged_identity = self._stage_identity(self._session_name)
        if staged_identity is None:
            raise SessionResumeError("private session file is missing")
        if staged_identity != pinned_identity:
            replacement = os.open(
                self._session_name,
                _READ_WRITE_FLAGS,
                dir_fd=self._owned_stage_descriptor,
            )
            try:
                if not stat.S_ISREG(os.fstat(replacement).st_mode):
                    raise SessionResumeError("private session file is unsafe")
                self._restore_lock_name()
                self._publish_artifact(self._session_name, staged_identity)
                os.close(self._session_descriptor)
                self._session_descriptor = replacement
                self.session_log.replace_descriptor(replacement)
                replacement = -1
                self._replace_session_anchor()
                self.publish()
            finally:
                if replacement >= 0:
                    os.close(replacement)
            return
        self.publish()

    def _replace_session_anchor(self) -> None:
        anchor_name = self._private_name("anchor", self._session_name)
        try:
            os.link(
                self._session_name,
                anchor_name,
                src_dir_fd=self._owned_stage_descriptor,
                dst_dir_fd=self._owned_stage_descriptor,
                follow_symlinks=False,
            )
            os.replace(
                anchor_name,
                self._session_anchor_name,
                src_dir_fd=self._owned_stage_descriptor,
                dst_dir_fd=self._owned_stage_descriptor,
            )
        except BaseException:
            try:
                os.unlink(anchor_name, dir_fd=self._owned_stage_descriptor)
            except OSError:
                pass
            raise

    def commit(self) -> None:
        """Finalize publication; later aborts become ordinary closes."""
        self._committed = True
        for publication in self._publications.values():
            self._discard_backup(publication)

    def _release_lock(self) -> None:
        if self._lock_descriptor is not None:
            self._restore_lock_name()
            os.ftruncate(self._lock_descriptor, 0)
            os.lseek(self._lock_descriptor, 0, os.SEEK_SET)
            os.close(self._lock_descriptor)
            self._lock_descriptor = None
        if self._process_claim is not None:
            self._process_claim.release()
            self._process_claim = None

    def _release_coordination(self) -> None:
        if self._coordination_claim is None:
            return
        self._coordination_claim.release()
        self._coordination_claim = None

    def _remove_stage(self) -> None:
        if self._stage_descriptor is not None:
            errors: list[BaseException] = []
            for name in os.listdir(self._stage_descriptor):
                try:
                    os.unlink(name, dir_fd=self._stage_descriptor)
                except BaseException as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup("private session stage cleanup failed", errors)
            os.close(self._stage_descriptor)
            self._stage_descriptor = None
        if self._stage_name is None:
            return
        try:
            current = os.stat(
                self._stage_name,
                dir_fd=self._sessions_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            self._stage_name = None
            self._stage_directory_identity = None
            return
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self._stage_directory_identity
        ):
            self._stage_name = None
            self._stage_directory_identity = None
            return
        try:
            os.rmdir(self._stage_name, dir_fd=self._sessions_descriptor)
        except FileNotFoundError:
            pass
        self._stage_name = None
        self._stage_directory_identity = None

    def _close_directories(self) -> None:
        while self._directory_descriptors:
            descriptor = self._directory_descriptors[-1]
            os.close(descriptor)
            self._directory_descriptors.pop()

    def close(self) -> None:
        if (
            self._committed
            and self._stage_descriptor is not None
            and self._lock_descriptor is not None
        ):
            self.publish()
        self._close_session()
        self._release_lock()
        self._remove_stage()
        self._close_directories()
        self._release_coordination()

    def abort(self) -> None:
        if (
            self._committed
            and self._stage_descriptor is not None
            and self._lock_descriptor is not None
        ):
            self.publish()
        self._close_session()
        if not self._committed:
            self._rollback_publications()
        self._release_lock()
        self._remove_stage()
        self._close_directories()
        self._release_coordination()


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
        self._channels: dict[str, _SessionChannel] = {}
        self._session_lifecycle: dict[str, str] = {}
        self._runtime_lock = asyncio.Lock()
        self._closed = False
        self._metadata_closed = False

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
    def _regular_identity_at(
        cls,
        directory_descriptor: int,
        name: str,
    ) -> tuple[int, int] | None:
        try:
            descriptor = os.open(
                name,
                _READ_FLAGS,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SessionResumeError("stale runtime stage is unsafe") from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionResumeError("stale runtime stage is unsafe")
            return cls._identity(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _publish_stale_artifact(
        sessions_descriptor: int,
        stage_descriptor: int,
        name: str,
    ) -> None:
        publish_name = f".recover-{Path(name).name}-{secrets.token_hex(8)}"
        try:
            os.link(
                name,
                publish_name,
                src_dir_fd=stage_descriptor,
                dst_dir_fd=sessions_descriptor,
                follow_symlinks=False,
            )
            os.replace(
                publish_name,
                name,
                src_dir_fd=sessions_descriptor,
                dst_dir_fd=sessions_descriptor,
            )
        except BaseException:
            try:
                os.unlink(publish_name, dir_fd=sessions_descriptor)
            except OSError:
                pass
            raise

    @classmethod
    def _recover_stale_stage(
        cls,
        sessions_descriptor: int,
        lock_descriptor: int,
        session_id: str,
        stage_name: str,
    ) -> None:
        try:
            stage_descriptor = os.open(
                stage_name,
                _DIRECTORY_FLAGS,
                dir_fd=sessions_descriptor,
            )
        except FileNotFoundError:
            return
        except OSError as error:
            raise SessionResumeError("stale runtime stage is unsafe") from error

        remove_stage = False
        primary: BaseException | None = None
        try:
            lock_name = f"{session_id}.lock"
            lock_identity = cls._regular_identity_at(
                stage_descriptor,
                lock_name,
            )
            if lock_identity == cls._identity(lock_descriptor):
                remove_stage = True

                session_name = f"{session_id}.jsonl"
                staged_session_identity = cls._regular_identity_at(
                    stage_descriptor,
                    session_name,
                )
                anchor_identity = cls._regular_identity_at(
                    stage_descriptor,
                    ".session-anchor",
                )
                public_session_identity = cls._regular_identity_at(
                    sessions_descriptor,
                    session_name,
                )
                if (
                    staged_session_identity is not None
                    and anchor_identity is not None
                    and staged_session_identity != anchor_identity
                    and public_session_identity == anchor_identity
                ):
                    cls._publish_stale_artifact(
                        sessions_descriptor,
                        stage_descriptor,
                        session_name,
                    )

                ledger_name = f"{session_id}.folds.sqlite3"
                recovery_names = tuple(
                    f"{session_id}{suffix}"
                    for suffix in _SQLITE_RECOVERY_SUFFIXES
                )
                recovery_identities = tuple(
                    cls._regular_identity_at(stage_descriptor, name)
                    for name in recovery_names
                )
                has_recovery_file = any(
                    identity is not None for identity in recovery_identities
                )
                if has_recovery_file:
                    staged_identity = cls._regular_identity_at(
                        stage_descriptor,
                        ledger_name,
                    )
                    public_identity = cls._regular_identity_at(
                        sessions_descriptor,
                        ledger_name,
                    )
                    if (
                        staged_identity is not None
                        and staged_identity == public_identity
                    ):
                        stage_path = _SecureSessionLease._descriptor_path(
                            stage_descriptor
                        )
                        try:
                            connection = sqlite3.connect(stage_path / ledger_name)
                            try:
                                connection.execute(
                                    "PRAGMA schema_version"
                                ).fetchone()
                            finally:
                                connection.close()
                        except sqlite3.Error as error:
                            raise SessionResumeError(
                                "cannot recover folding ledger after a crash"
                            ) from error

                cleanup_errors: list[BaseException] = []
                for name in os.listdir(stage_descriptor):
                    try:
                        os.unlink(name, dir_fd=stage_descriptor)
                    except BaseException as error:
                        cleanup_errors.append(error)
                if cleanup_errors:
                    raise SessionResumeError(
                        "cannot clean stale runtime stage"
                    ) from ExceptionGroup(
                        "stale runtime stage cleanup failed",
                        cleanup_errors,
                    )
        except BaseException as error:
            primary = error

        try:
            os.close(stage_descriptor)
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(
                "stale stage descriptor cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
            existing = tuple(getattr(primary, "cleanup_errors", ()))
            primary.cleanup_errors = (  # type: ignore[attr-defined]
                *existing,
                cleanup_error,
            )
        if primary is not None:
            raise primary
        if remove_stage:
            try:
                os.rmdir(stage_name, dir_fd=sessions_descriptor)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise SessionResumeError(
                    "cannot clean stale runtime stage"
                ) from error

    @classmethod
    def _recover_stale_stages(
        cls,
        sessions_descriptor: int,
        lock_descriptor: int,
        session_id: str,
    ) -> None:
        prefix = f".runtime-{session_id}-"
        try:
            candidates = sorted(
                name
                for name in os.listdir(sessions_descriptor)
                if name.startswith(prefix)
            )
        except OSError as error:
            raise SessionResumeError("cannot inspect stale runtime stages") from error
        for stage_name in candidates:
            cls._recover_stale_stage(
                sessions_descriptor,
                lock_descriptor,
                session_id,
                stage_name,
            )

    @classmethod
    def _acquire_session_lease(
        cls,
        workspace: Path,
        session_id: str,
        *,
        create_session: bool,
    ) -> _SecureSessionLease:
        session_id = validate_session_id(session_id)
        directory_descriptors = cls._open_session_directory_descriptors(workspace)
        sessions_descriptor = directory_descriptors[-1]
        process_claim: _ProcessLeaseClaim | None = None
        coordination_claim: _CoordinationLeaseClaim | None = None
        lock_descriptor: int | None = None
        stage_descriptor: int | None = None
        stage_name: str | None = None
        stage_created = False
        session_descriptor: int | None = None
        source_descriptor: int | None = None
        staged_descriptor: int | None = None
        cleanup_errors: list[BaseException] = []
        try:
            process_claim = _ProcessLeaseClaim.acquire(
                directory_descriptors[0],
                session_id,
            )
            coordination_claim = _CoordinationLeaseClaim.acquire(
                directory_descriptors[0],
                session_id,
            )
            lock_descriptor = cls._acquire_secure_lock(
                sessions_descriptor, session_id
            )
            cls._recover_stale_stages(
                sessions_descriptor,
                lock_descriptor,
                session_id,
            )
            lock_name = f"{session_id}.lock"
            stage_name = f".runtime-{session_id}-{secrets.token_hex(8)}"
            os.mkdir(stage_name, 0o700, dir_fd=sessions_descriptor)
            stage_created = True
            stage_descriptor = os.open(
                stage_name,
                _DIRECTORY_FLAGS,
                dir_fd=sessions_descriptor,
            )
            stage_path = _SecureSessionLease._descriptor_path(stage_descriptor)
            os.link(
                lock_name,
                lock_name,
                src_dir_fd=sessions_descriptor,
                dst_dir_fd=stage_descriptor,
                follow_symlinks=False,
            )
            lock_anchor = os.stat(
                lock_name,
                dir_fd=stage_descriptor,
                follow_symlinks=False,
            )
            if (lock_anchor.st_dev, lock_anchor.st_ino) != cls._identity(
                lock_descriptor
            ):
                raise SessionResumeError("session lock changed during acquisition")

            session_name = f"{session_id}.jsonl"
            if create_session:
                try:
                    os.stat(
                        session_name,
                        dir_fd=sessions_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise SessionResumeError("generated session id already exists")
                session_descriptor = os.open(
                    session_name,
                    _CREATE_READ_WRITE_FLAGS,
                    0o600,
                    dir_fd=stage_descriptor,
                )
            else:
                try:
                    source_descriptor = os.open(
                        session_name,
                        _READ_FLAGS,
                        dir_fd=sessions_descriptor,
                    )
                except OSError as error:
                    raise SessionResumeError(
                        "session file is missing or unsafe"
                    ) from error
                if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
                    raise SessionResumeError("session file is unsafe")
                session_descriptor = os.open(
                    session_name,
                    _CREATE_READ_WRITE_FLAGS,
                    0o600,
                    dir_fd=stage_descriptor,
                )
                _SecureSessionLease._copy_descriptor(
                    source_descriptor,
                    session_descriptor,
                )
                os.close(source_descriptor)
                source_descriptor = None
            if not stat.S_ISREG(os.fstat(session_descriptor).st_mode):
                raise SessionResumeError("session file is unsafe")
            session_anchor_name = ".session-anchor"
            os.link(
                session_name,
                session_anchor_name,
                src_dir_fd=stage_descriptor,
                dst_dir_fd=stage_descriptor,
                follow_symlinks=False,
            )

            for suffix in _STAGED_CONTEXT_SUFFIXES:
                name = f"{session_id}{suffix}"
                try:
                    source_descriptor = os.open(
                        name,
                        _READ_FLAGS,
                        dir_fd=sessions_descriptor,
                    )
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise SessionResumeError("session artifact is unsafe") from error
                if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
                    raise SessionResumeError("session artifact is unsafe")
                staged_descriptor = os.open(
                    name,
                    _CREATE_READ_WRITE_FLAGS,
                    0o600,
                    dir_fd=stage_descriptor,
                )
                _SecureSessionLease._copy_descriptor(
                    source_descriptor,
                    staged_descriptor,
                )
                os.close(staged_descriptor)
                staged_descriptor = None
                os.close(source_descriptor)
                source_descriptor = None

            lease = _SecureSessionLease(
                directory_descriptors,
                stage_descriptor,
                stage_path,
                session_descriptor,
                session_name,
                session_anchor_name,
                lock_descriptor,
                lock_name,
                process_claim,
                coordination_claim,
            )
            if create_session:
                lease._publish_initial_session()
            session_descriptor = None
            lock_descriptor = None
            stage_descriptor = None
            process_claim = None
            coordination_claim = None
            return lease
        except BaseException as primary:
            if staged_descriptor is not None:
                try:
                    os.close(staged_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if source_descriptor is not None:
                try:
                    os.close(source_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if session_descriptor is not None:
                try:
                    os.close(session_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if stage_descriptor is not None:
                staged_names: list[str] | None = None
                for _attempt in range(2):
                    try:
                        staged_names = os.listdir(stage_descriptor)
                    except BaseException as error:
                        cleanup_errors.append(error)
                        continue
                    break
                if staged_names is not None:
                    for name in staged_names:
                        try:
                            os.unlink(name, dir_fd=stage_descriptor)
                        except BaseException as error:
                            cleanup_errors.append(error)
                try:
                    os.close(stage_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if stage_created and stage_name is not None:
                try:
                    os.rmdir(stage_name, dir_fd=sessions_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if lock_descriptor is not None:
                try:
                    os.ftruncate(lock_descriptor, 0)
                except BaseException as error:
                    cleanup_errors.append(error)
                    try:
                        os.ftruncate(lock_descriptor, 0)
                    except BaseException:
                        pass
                try:
                    os.close(lock_descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if process_claim is not None:
                release_errors: list[BaseException] = []
                for _attempt in range(2):
                    try:
                        process_claim.release()
                    except BaseException as error:
                        release_errors.append(error)
                        continue
                    break
                else:
                    cleanup_errors.extend(release_errors)
            if coordination_claim is not None:
                release_errors = []
                for _attempt in range(2):
                    try:
                        coordination_claim.release()
                    except BaseException as error:
                        release_errors.append(error)
                        continue
                    break
                else:
                    cleanup_errors.extend(release_errors)
            for descriptor in reversed(directory_descriptors):
                try:
                    os.close(descriptor)
                except BaseException as error:
                    cleanup_errors.append(error)
            if cleanup_errors:
                primary.add_note(
                    "lease acquisition cleanup incomplete: "
                    + "; ".join(
                        f"{type(error).__name__}: {error}"
                        for error in cleanup_errors
                    )
                )
                primary.cleanup_errors = tuple(  # type: ignore[attr-defined]
                    cleanup_errors
                )
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
            messages = lease.session_log.load()
            lease.publish()
            lease.commit()
            lease.close()
            return messages
        except BaseException as primary:
            try:
                lease.abort()
            except BaseException as cleanup_error:
                primary.add_note(
                    "transcript cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
                existing = tuple(getattr(primary, "cleanup_errors", ()))
                primary.cleanup_errors = (  # type: ignore[attr-defined]
                    *existing,
                    cleanup_error,
                )
            raise

    @classmethod
    def _context_mode_for_discovery(
        cls,
        workspace: Path,
        session_id: str,
    ) -> str:
        descriptors = cls._open_session_directory_descriptors(workspace)
        sessions_descriptor = descriptors[-1]
        mode_descriptor: int | None = None
        ledger_descriptor: int | None = None
        try:
            try:
                mode_descriptor = os.open(
                    f"{session_id}.context-mode",
                    _READ_FLAGS,
                    dir_fd=sessions_descriptor,
                )
            except OSError:
                stored = ""
            else:
                if not stat.S_ISREG(os.fstat(mode_descriptor).st_mode):
                    stored = ""
                else:
                    try:
                        stored = os.read(mode_descriptor, 64).decode().strip()
                    except UnicodeError:
                        stored = ""
            try:
                ledger_descriptor = os.open(
                    f"{session_id}.folds.sqlite3",
                    _READ_FLAGS,
                    dir_fd=sessions_descriptor,
                )
            except OSError:
                has_ledger = False
            else:
                has_ledger = stat.S_ISREG(os.fstat(ledger_descriptor).st_mode)
            return "folding" if stored == "folding" or has_ledger else "compaction"
        finally:
            if mode_descriptor is not None:
                os.close(mode_descriptor)
            if ledger_descriptor is not None:
                os.close(ledger_descriptor)
            cls._close_descriptors(descriptors)

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
                            context_mode=self._context_mode_for_discovery(
                                workspace,
                                session_id,
                            ),
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
        session_lease: _SecureSessionLease,
        *,
        resuming: bool,
    ) -> HarnessRuntime:
        try:
            return HarnessRuntime(
                config,
                llm,
                session_lease.session_path,
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
            lease,
            resuming=False,
        )

    @staticmethod
    def _secure_runtime_lease(runtime: HarnessRuntime) -> _SecureSessionLease | None:
        lease = getattr(runtime, "_session_lease", None)
        return lease if isinstance(lease, _SecureSessionLease) else None

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
            lease = self._secure_runtime_lease(runtime)
            try:
                if lease is not None:
                    lease.publish()
                record = self.metadata.create_session(provisional)
            except BaseException as error:
                self._abort_after_failure(runtime, error)
                raise
            if lease is not None:
                lease.commit()
            self._runtimes[session_id] = runtime
            return record

    def _open_runtime_locked(self, session_id: object) -> HarnessRuntime:
        self._ensure_open()
        record = self._required_record(session_id)
        lifecycle = self._session_lifecycle.get(record.session_id)
        if lifecycle is not None:
            raise SessionResumeError(f"session cleanup is {lifecycle}")
        existing = self._runtimes.get(record.session_id)
        if existing is not None:
            if getattr(existing, "_ui_durability_failed", False):
                raise SessionResumeError(
                    "session transcript authority is unavailable"
                )
            self._safe_session_path(
                record.workspace,
                record.session_id,
                require_file=False,
                create_parents=False,
            )
            return existing
        runtime = self._construct_runtime(record)
        lease = self._secure_runtime_lease(runtime)
        try:
            if lease is not None:
                lease.publish()
            self.metadata.touch_session(record.session_id)
        except BaseException as error:
            self._abort_after_failure(runtime, error)
            raise
        if lease is not None:
            lease.commit()
        self._runtimes[record.session_id] = runtime
        return runtime

    async def open_runtime(self, session_id: object) -> HarnessRuntime:
        async with self._runtime_lock:
            return self._open_runtime_locked(session_id)

    async def connect(self, session_id: object) -> SessionConnection:
        """Claim the next WebSocket generation for an existing session."""
        loop = asyncio.get_running_loop()
        async with self._runtime_lock:
            runtime = self._open_runtime_locked(session_id)
            validated = validate_session_id(session_id)
            channel = self._channels.get(validated)
            if channel is None:
                channel = _SessionChannel(validated, runtime, self.metadata)
                self._channels[validated] = channel
            return channel.connect(loop)

    async def transcript(self, session_id: object) -> list[dict]:
        async with self._runtime_lock:
            self._ensure_open()
            record = self._required_record(session_id)
            lifecycle = self._session_lifecycle.get(record.session_id)
            if lifecycle is not None:
                raise SessionResumeError(f"session cleanup is {lifecycle}")
            self._safe_session_path(
                record.workspace,
                record.session_id,
                require_file=True,
                create_parents=False,
            )
            runtime = self._runtimes.get(record.session_id)
            if runtime is not None:
                try:
                    return copy.deepcopy(runtime.session_log.load())
                except SessionResumeError:
                    raise
                except (KeyError, OSError, ValueError) as error:
                    raise SessionResumeError(str(error)) from None
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
            self._session_lifecycle[record.session_id] = "draining"
            channel = self._channels.get(record.session_id)
            if channel is not None:
                channel.begin_shutdown()
                await channel.wait_for_worker()
            runtime = self._runtimes.get(record.session_id)
            if runtime is not None:
                try:
                    runtime.close()
                except BaseException:
                    self._session_lifecycle[record.session_id] = "cleanup_failed"
                    if channel is not None:
                        channel.mark_cleanup_failed()
                    raise
                self._runtimes.pop(record.session_id, None)
                self._channels.pop(record.session_id, None)
            try:
                self.metadata.archive_session(record.session_id)
            except BaseException:
                self._session_lifecycle[record.session_id] = "cleanup_failed"
                if channel is not None:
                    channel.mark_cleanup_failed()
                raise
            self._channels.pop(record.session_id, None)
            self._session_lifecycle.pop(record.session_id, None)

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
            self._closed = True
            for session_id, channel in tuple(self._channels.items()):
                self._session_lifecycle[session_id] = "draining"
                channel.begin_shutdown()
            worker_ready: set[str] = set()
            for session_id, channel in tuple(self._channels.items()):
                try:
                    await channel.wait_for_worker()
                except Exception as error:
                    errors.append(error)
                else:
                    worker_ready.add(session_id)
            for session_id, runtime in tuple(self._runtimes.items()):
                if session_id in self._channels and session_id not in worker_ready:
                    continue
                try:
                    runtime.close()
                except Exception as error:
                    errors.append(error)
                    self._session_lifecycle[session_id] = "cleanup_failed"
                    channel = self._channels.get(session_id)
                    if channel is not None:
                        channel.mark_cleanup_failed()
                else:
                    self._runtimes.pop(session_id, None)
                    self._channels.pop(session_id, None)
                    self._session_lifecycle.pop(session_id, None)
            for session_id in worker_ready:
                if session_id not in self._runtimes:
                    self._channels.pop(session_id, None)
                    self._session_lifecycle.pop(session_id, None)
            if not self._metadata_closed:
                try:
                    self.metadata.close()
                except Exception as error:
                    errors.append(error)
                else:
                    self._metadata_closed = True
        if errors:
            raise ExceptionGroup("session manager close failed", errors)
