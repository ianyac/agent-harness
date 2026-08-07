import subprocess
import sys
from pathlib import Path

from harness.folding import FoldingContext
from main import folding_paths


ROOT = Path(__file__).parents[1]


def test_folding_artifacts_are_stably_derived_from_the_session_path(tmp_path):
    # Regression caught: resume must reopen the original ledger instead of
    # silently creating a fresh visibility state under a process-specific name.
    session = tmp_path / ".agent" / "sessions" / "s.jsonl"
    assert folding_paths(session) == (
        session.with_suffix(".folds.sqlite3"),
        session.with_suffix(".fold-decisions.jsonl"),
    )


def test_cli_rejects_explicit_compaction_with_folding_before_startup(tmp_path):
    # Real CLI boundary: conflict rejection must happen before credentials,
    # hooks, session files, or the model client can produce unrelated failures.
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--workspace",
            str(tmp_path),
            "--fold-context",
            "--compact-threshold",
            "100",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--fold-context cannot be combined with --compact-threshold" in result.stderr
    assert not (tmp_path / ".agent").exists()


def test_cli_help_advertises_recoverable_context_folding():
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--fold-context" in result.stdout
    assert "recoverable context folding" in result.stdout


def test_resume_rejects_compaction_for_a_persisted_folding_session(tmp_path):
    sessions = tmp_path / ".agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(
        '{"type":"message","message":{"role":"assistant","content":"done"}}\n'
    )
    (sessions / "s.context-mode").write_text("folding\n")
    FoldingContext(sessions / "s.folds.sqlite3", "s").close()

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--workspace",
            str(tmp_path),
            "--resume",
            "s",
            "--compact-threshold",
            "100",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "session uses context folding" in result.stderr
    assert not (sessions / "s.lock").exists()


def test_resume_automatically_restores_the_persisted_folding_mode(tmp_path):
    sessions = tmp_path / ".agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(
        '{"type":"message","message":{"role":"assistant","content":"done"}}\n'
    )
    (sessions / "s.context-mode").write_text("folding\n")
    FoldingContext(sessions / "s.folds.sqlite3", "s").close()

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--workspace",
            str(tmp_path),
            "--resume",
            "s",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "recoverable context folding enabled; compaction disabled" in result.stdout
    assert (sessions / "s.folds.sqlite3").exists()


def test_resume_refuses_to_silently_replace_a_missing_folding_ledger(tmp_path):
    sessions = tmp_path / ".agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(
        '{"type":"message","message":{"role":"assistant","content":"done"}}\n'
    )
    (sessions / "s.context-mode").write_text("folding\n")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--workspace",
            str(tmp_path),
            "--resume",
            "s",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "folding ledger is missing" in result.stderr
