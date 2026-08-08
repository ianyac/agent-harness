import json
from dataclasses import dataclass, field
from pathlib import Path

from harness.folding import FoldingContext


CONTEXT_MODES = ("compaction", "folding")


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
    _created_paths: list[Path] = field(default_factory=list, repr=False)
    _created_identities: dict[Path, tuple[int, int]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _folding_owned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._folding_owned = self.folding is not None
        for path in self._created_paths:
            try:
                created = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            self._created_identities[path] = (created.st_dev, created.st_ino)

    def close(self) -> None:
        if self._folding_owned and self.folding is not None:
            self.folding.close()
            self._folding_owned = False

    def rollback(self) -> None:
        """Release resources and remove only artifacts this attempt created."""
        self.close()
        for path in reversed(self._created_paths.copy()):
            identity = self._created_identities.get(path)
            if identity is not None:
                try:
                    current = path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    current_identity = None
                else:
                    current_identity = (current.st_dev, current.st_ino)
                if current_identity == identity:
                    path.unlink()
            self._created_identities.pop(path, None)
            self._created_paths.remove(path)


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
    candidates = [mode_path]
    if selected == "folding":
        candidates.extend([ledger, decision_log])
    created = [path for path in candidates if not path.exists()]

    try:
        mode_path.parent.mkdir(parents=True, exist_ok=True)
        mode_path.write_text(selected + "\n")
    except OSError as error:
        failure = ValueError(
            f"cannot persist context mode for {session_path.name}: {error}"
        )
        cleanup_errors = []
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
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
        return PreparedContext(selected, compact_threshold, _created_paths=created)

    try:
        folding = FoldingContext(
            ledger,
            session_id=session_path.stem,
            decision_log_path=decision_log,
            session_log_path=session_path,
        )
    except BaseException as error:
        cleanup_errors = []
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            error.add_note(
                "artifact cleanup incomplete: "
                + "; ".join(str(item) for item in cleanup_errors)
            )
            error.cleanup_errors = tuple(cleanup_errors)  # type: ignore[attr-defined]
        raise
    return PreparedContext(selected, None, folding, _created_paths=created)
