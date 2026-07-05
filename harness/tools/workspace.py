from pathlib import Path


def resolve_in_workspace(path: str, workspace: Path | None) -> Path:
    """Resolve `path` and, if a workspace is set, refuse anything outside it.

    Advisory confinement — an in-process check, weaker than the OS sandbox
    (a bug here defeats it), but sufficient for tools whose code we wrote.
    resolve() collapses '..' and follows symlinks, so escapes via either are
    caught at their real destination.
    """
    if workspace is None:
        return Path(path)  # unconfined: pre-lesson-9 behavior
    root = workspace.resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PermissionError(f"path {path!r} resolves outside the workspace {root}")
    return candidate
