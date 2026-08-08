import json
from pathlib import Path

import pytest

from server.context_mode import PreparedContext, prepare_context_mode


def test_folding_session_cannot_resume_without_ledger(tmp_path: Path):
    session = tmp_path / "s.jsonl"
    session.touch()
    session.with_suffix(".context-mode").write_text("folding\n")

    with pytest.raises(ValueError, match="ledger is missing"):
        prepare_context_mode(session, requested=None, resuming=True)


def test_context_mode_rejects_invalid_persisted_and_requested_values(tmp_path: Path):
    session = tmp_path / "s.jsonl"
    session.with_suffix(".context-mode").write_text("automatic\n")

    with pytest.raises(ValueError, match="invalid persisted context mode"):
        prepare_context_mode(session, requested=None, resuming=False)

    session.with_suffix(".context-mode").unlink()
    with pytest.raises(ValueError, match="invalid requested context mode"):
        prepare_context_mode(session, requested="automatic", resuming=False)


def test_legacy_folding_ledger_is_authoritative_and_context_close_is_idempotent(
    tmp_path: Path,
):
    session = tmp_path / "legacy.jsonl"
    ledger = session.with_suffix(".folds.sqlite3")
    ledger.touch()

    prepared = prepare_context_mode(session, requested=None, resuming=True)

    assert prepared.mode == "folding"
    assert prepared.compact_threshold is None
    assert prepared.folding is not None
    assert session.with_suffix(".context-mode").read_text() == "folding\n"
    prepared.close()
    prepared.close()

def test_compacted_legacy_session_cannot_switch_to_folding(tmp_path: Path):
    session = tmp_path / "compacted.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "compact",
                "cut": 2,
                "summary": {"role": "assistant", "content": "summary"},
            }
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="contains compaction events"):
        prepare_context_mode(session, requested="folding", resuming=True)


def test_folding_rejects_compact_threshold_and_compaction_preserves_it(tmp_path: Path):
    folding_session = tmp_path / "folding.jsonl"
    with pytest.raises(ValueError, match="compact threshold"):
        prepare_context_mode(
            folding_session,
            requested="folding",
            resuming=False,
            compact_threshold=100,
        )

    compaction_session = tmp_path / "compaction.jsonl"
    prepared = prepare_context_mode(
        compaction_session,
        requested="compaction",
        resuming=False,
        compact_threshold=100,
    )
    assert prepared.mode == "compaction"
    assert prepared.compact_threshold == 100
    assert prepared.folding is None
    assert compaction_session.with_suffix(".context-mode").read_text() == (
        "compaction\n"
    )
    prepared.close()


def test_prepared_context_retries_only_a_still_owned_folding_resource():
    class CloseOnce:
        def __init__(self):
            self.calls = 0

        def close(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("close failed")

    folding = CloseOnce()
    prepared = PreparedContext("folding", None, folding)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="close failed"):
        prepared.close()
    prepared.close()
    prepared.close()

    assert folding.calls == 2


def test_failed_context_mode_write_removes_a_new_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session = tmp_path / "s.jsonl"
    mode_path = session.with_suffix(".context-mode")
    from server import context_mode as context_mode_module

    original_write = context_mode_module.os.write
    failed = False

    def fail_after_write(descriptor: int, payload: bytes):
        nonlocal failed
        if not failed:
            failed = True
            original_write(descriptor, payload[:1])
            raise OSError("disk full")
        return original_write(descriptor, payload)

    monkeypatch.setattr(context_mode_module.os, "write", fail_after_write)

    with pytest.raises(ValueError, match="cannot persist context mode"):
        prepare_context_mode(session, requested="compaction", resuming=False)

    assert not mode_path.exists()


def test_failed_context_mode_descriptor_close_removes_a_new_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session = tmp_path / "close-fails.jsonl"
    mode_path = session.with_suffix(".context-mode")
    from server import context_mode as context_mode_module

    original_open = context_mode_module.os.open
    original_close = context_mode_module.os.close
    mode_descriptor: int | None = None
    failed = False

    def record_open(path, flags, *args, **kwargs):
        nonlocal mode_descriptor
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == mode_path:
            mode_descriptor = descriptor
        return descriptor

    def fail_mode_close_once(descriptor: int):
        nonlocal failed
        if descriptor == mode_descriptor and not failed:
            failed = True
            raise OSError("mode descriptor close interrupted")
        return original_close(descriptor)

    monkeypatch.setattr(context_mode_module.os, "open", record_open)
    monkeypatch.setattr(context_mode_module.os, "close", fail_mode_close_once)

    with pytest.raises(ValueError, match="cannot persist context mode"):
        prepare_context_mode(session, requested="compaction", resuming=False)

    assert not mode_path.exists()


def test_context_rollback_preserves_a_replacement_at_an_owned_path(tmp_path: Path):
    session = tmp_path / "s.jsonl"
    prepared = prepare_context_mode(
        session,
        requested="compaction",
        resuming=False,
    )
    mode_path = session.with_suffix(".context-mode")
    mode_path.unlink()
    mode_path.write_text("replacement\n")

    prepared.rollback()

    assert mode_path.read_text() == "replacement\n"


def test_context_rollback_preserves_replacement_installed_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session = tmp_path / "s.jsonl"
    prepared = prepare_context_mode(
        session,
        requested="compaction",
        resuming=False,
    )
    mode_path = session.with_suffix(".context-mode")
    original_stat = Path.stat
    original_unlink = Path.unlink
    original_rename = Path.rename
    swapped = False

    def install_replacement() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        original_unlink(mode_path)
        mode_path.write_text("replacement installed during cleanup\n")

    def stat_then_swap(path: Path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == mode_path and kwargs.get("follow_symlinks") is False:
            install_replacement()
        return result

    def rename_with_swap(path: Path, target: Path):
        if path == mode_path:
            install_replacement()
        return original_rename(path, target)

    monkeypatch.setattr(Path, "stat", stat_then_swap)
    monkeypatch.setattr(Path, "rename", rename_with_swap)

    prepared.rollback()

    assert mode_path.read_text() == "replacement installed during cleanup\n"
