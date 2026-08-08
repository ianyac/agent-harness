import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from harness.folding import FoldingContext


CONTEXT_MODES = ("compaction", "folding")
_READ_WRITE_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS = _READ_WRITE_FLAGS | os.O_CREAT | os.O_EXCL


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        written += os.write(descriptor, payload[written:])


def _attach_cleanup_errors(
    primary: BaseException,
    errors: list[BaseException],
) -> None:
    if not errors:
        return
    primary.add_note(
        "artifact cleanup incomplete: "
        + "; ".join(f"{type(error).__name__}: {error}" for error in errors)
    )
    primary.cleanup_errors = tuple(errors)  # type: ignore[attr-defined]


@dataclass
class _OwnedArtifact:
    """One atomically created artifact with retryable, replacement-safe cleanup."""

    path: Path
    identity: tuple[int, int]
    _quarantine: Path | None = field(default=None, init=False, repr=False)

    def rollback(self) -> None:
        if self._quarantine is None:
            quarantine = self.path.with_name(
                f".{self.path.name}.rollback-{secrets.token_hex(8)}"
            )
            try:
                self.path.rename(quarantine)
            except FileNotFoundError:
                try:
                    quarantine.stat(follow_symlinks=False)
                except FileNotFoundError:
                    return
                self._quarantine = quarantine
            except BaseException:
                try:
                    quarantine.stat(follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    self._quarantine = quarantine
                raise
            else:
                self._quarantine = quarantine

        quarantine = self._quarantine
        try:
            current = quarantine.stat(follow_symlinks=False)
        except FileNotFoundError:
            self._quarantine = None
            return
        current_identity = (current.st_dev, current.st_ino)
        if current_identity == self.identity:
            quarantine.unlink()
            self._quarantine = None
            return

        try:
            os.link(
                quarantine,
                self.path,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            try:
                private = quarantine.stat(follow_symlinks=False)
                public = self.path.stat(follow_symlinks=False)
            except FileNotFoundError:
                raise OSError(
                    f"cannot restore replacement at {self.path}"
                ) from error
            if (private.st_dev, private.st_ino) != (
                public.st_dev,
                public.st_ino,
            ):
                raise OSError(
                    f"cannot restore replacement at {self.path}"
                ) from error
        quarantine.unlink()
        self._quarantine = None


class _PublishingFoldingContext(FoldingContext):
    """FoldingContext that republishes files replaced by secure purge writes."""

    def __init__(
        self,
        *args,
        artifact_publisher: Callable[[], None],
        **kwargs,
    ) -> None:
        self._artifact_publisher = artifact_publisher
        super().__init__(*args, **kwargs)

    def _purge_session_log(self, *args, **kwargs) -> None:
        super()._purge_session_log(*args, **kwargs)
        self._artifact_publisher()


def _open_context_artifact(path: Path, *, create: bool) -> tuple[int, bool]:
    if create:
        try:
            descriptor = os.open(path, _CREATE_FLAGS, 0o600)
        except FileExistsError:
            descriptor = os.open(path, _READ_WRITE_FLAGS)
            created = False
        else:
            created = True
    else:
        descriptor = os.open(path, _READ_WRITE_FLAGS)
        created = False
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError(f"context artifact is not a regular file: {path}")
    return descriptor, created


def _ensure_context_artifact(path: Path) -> _OwnedArtifact | None:
    descriptor, created = _open_context_artifact(path, create=True)
    owned: _OwnedArtifact | None = None
    try:
        if created:
            opened = os.fstat(descriptor)
            owned = _OwnedArtifact(path, (opened.st_dev, opened.st_ino))
        os.close(descriptor)
        return owned
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        try:
            os.close(descriptor)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if created:
            if owned is None:
                try:
                    opened = os.stat(path, follow_symlinks=False)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                else:
                    owned = _OwnedArtifact(
                        path,
                        (opened.st_dev, opened.st_ino),
                    )
            if owned is not None:
                try:
                    owned.rollback()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
        _attach_cleanup_errors(error, cleanup_errors)
        raise


def _persist_context_mode(path: Path, mode: str) -> _OwnedArtifact | None:
    descriptor, created = _open_context_artifact(path, create=True)
    owned: _OwnedArtifact | None = None
    if created:
        opened = os.fstat(descriptor)
        owned = _OwnedArtifact(path, (opened.st_dev, opened.st_ino))
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, (mode + "\n").encode())
        os.close(descriptor)
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        try:
            os.close(descriptor)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if owned is not None:
            try:
                owned.rollback()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        _attach_cleanup_errors(error, cleanup_errors)
        raise
    return owned


def _contains_compaction(session_path: Path) -> bool:
    try:
        lines = session_path.read_text().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "compact":
            return True
    return False


@dataclass
class PreparedContext:
    mode: str
    compact_threshold: int | None
    folding: FoldingContext | None = None
    _created_artifacts: list[_OwnedArtifact] = field(default_factory=list, repr=False)
    _folding_owned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._folding_owned = self.folding is not None

    def close(self) -> None:
        if self._folding_owned and self.folding is not None:
            self.folding.close()
            self._folding_owned = False

    def rollback(self) -> None:
        """Release resources and remove only artifacts this attempt created."""
        self.close()
        for artifact in reversed(self._created_artifacts.copy()):
            artifact.rollback()
            self._created_artifacts.remove(artifact)


def resolve_context_mode(
    session_path: Path,
    *,
    requested: str | None,
    resuming: bool,
    compact_threshold: int | None = None,
) -> str:
    """Resolve context ownership from durable artifacts without mutating them."""
    session_path = Path(session_path)
    if requested not in (None, *CONTEXT_MODES):
        raise ValueError(f"invalid requested context mode: {requested!r}")
    if compact_threshold is not None and compact_threshold <= 0:
        raise ValueError("compact threshold must be a positive token count")

    mode_path = session_path.with_suffix(".context-mode")
    try:
        stored = mode_path.read_text().strip() if mode_path.exists() else None
    except OSError as error:
        raise ValueError(
            f"cannot read context mode for {session_path.name}: {error}"
        ) from error
    if stored not in (None, *CONTEXT_MODES):
        raise ValueError(
            f"invalid persisted context mode for {session_path.name}: {stored!r}"
        )

    ledger = session_path.with_suffix(".folds.sqlite3")
    if stored == "folding" and resuming and not ledger.exists():
        raise ValueError(
            f"session {session_path.name} uses context folding but its ledger is missing"
        )
    if stored is None and ledger.exists():
        stored = "folding"

    if stored == "folding":
        selected = "folding"
    elif stored == "compaction":
        if requested == "folding":
            raise ValueError(
                f"session {session_path.name} uses compaction and cannot switch to folding"
            )
        selected = "compaction"
    else:
        selected = requested or "compaction"
        if (
            selected == "folding"
            and resuming
            and _contains_compaction(session_path)
        ):
            raise ValueError(
                f"session {session_path.name} contains compaction events and cannot switch to folding"
            )

    if selected == "folding" and compact_threshold is not None:
        raise ValueError("context folding cannot be combined with a compact threshold")
    return selected


def prepare_context_mode(
    session_path: Path,
    *,
    requested: str | None,
    resuming: bool,
    compact_threshold: int | None = None,
    artifact_publisher: Callable[[], None] | None = None,
) -> PreparedContext:
    """Select and open the documented context artifacts for one session."""
    session_path = Path(session_path)
    selected = resolve_context_mode(
        session_path,
        requested=requested,
        resuming=resuming,
        compact_threshold=compact_threshold,
    )
    mode_path = session_path.with_suffix(".context-mode")
    ledger = session_path.with_suffix(".folds.sqlite3")
    decision_log = session_path.with_suffix(".fold-decisions.jsonl")
    created: list[_OwnedArtifact] = []
    try:
        mode_path.parent.mkdir(parents=True, exist_ok=True)
        mode_artifact = _persist_context_mode(mode_path, selected)
        if mode_artifact is not None:
            created.append(mode_artifact)
    except OSError as error:
        failure = ValueError(
            f"cannot persist context mode for {session_path.name}: {error}"
        )
        cleanup_errors = []
        for artifact in reversed(created):
            try:
                artifact.rollback()
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            failure.add_note(
                "artifact cleanup incomplete: "
                + "; ".join(str(item) for item in cleanup_errors)
            )
            failure.cleanup_errors = tuple(cleanup_errors)  # type: ignore[attr-defined]
        raise failure from error

    if selected == "compaction":
        return PreparedContext(
            selected,
            compact_threshold,
            _created_artifacts=created,
        )

    try:
        for path in (ledger, decision_log):
            artifact = _ensure_context_artifact(path)
            if artifact is not None:
                created.append(artifact)
        folding_type = (
            _PublishingFoldingContext
            if artifact_publisher is not None
            else FoldingContext
        )
        folding_kwargs = (
            {"artifact_publisher": artifact_publisher}
            if artifact_publisher is not None
            else {}
        )
        folding = folding_type(
            ledger,
            session_id=session_path.stem,
            decision_log_path=decision_log,
            session_log_path=session_path,
            **folding_kwargs,
        )
    except BaseException as error:
        cleanup_errors = []
        for artifact in reversed(created):
            try:
                artifact.rollback()
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            error.add_note(
                "artifact cleanup incomplete: "
                + "; ".join(str(item) for item in cleanup_errors)
            )
            error.cleanup_errors = tuple(cleanup_errors)  # type: ignore[attr-defined]
        raise
    return PreparedContext(selected, None, folding, _created_artifacts=created)
