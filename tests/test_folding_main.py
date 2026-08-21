import os
import subprocess
import sys
from pathlib import Path

from harness.folding import FoldConfig, FoldingContext
from main import folding_paths


ROOT = Path(__file__).parents[1]


def run_main(*args, workspace=None, env=None):
    """Run main.py as a real child process: the CLI boundary, not the library."""
    argv = [sys.executable, str(ROOT / "main.py")]
    if workspace is not None:
        argv += ["--workspace", str(workspace)]
    return subprocess.run([*argv, *args], capture_output=True, text=True, env=env)


def persisted_folding_session(tmp_path, *, ledger=True):
    """A recorded session whose context-mode file says folding; ``ledger=False``
    leaves its folds database missing."""
    sessions = tmp_path / ".agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(
        '{"type":"message","message":{"role":"assistant","content":"done"}}\n'
    )
    (sessions / "s.context-mode").write_text("folding\n")
    if ledger:
        FoldingContext(sessions / "s.folds.sqlite3", "s").close()
    return sessions


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
    result = run_main("--fold-context", "--compact-threshold", "100", workspace=tmp_path)
    assert result.returncode == 2
    assert "--fold-context cannot be combined with --compact-threshold" in result.stderr
    assert not (tmp_path / ".agent").exists()


def test_cli_help_advertises_recoverable_context_folding():
    result = run_main("--help")
    assert result.returncode == 0
    assert "--fold-context" in result.stdout
    assert "recoverable context folding" in result.stdout


def test_resume_rejects_compaction_for_a_persisted_folding_session(tmp_path):
    sessions = persisted_folding_session(tmp_path)

    result = run_main("--resume", "s", "--compact-threshold", "100", workspace=tmp_path)

    assert result.returncode == 2
    assert "session uses context folding" in result.stderr
    assert not (sessions / "s.lock").exists()


def test_resume_automatically_restores_the_persisted_folding_mode(tmp_path):
    sessions = persisted_folding_session(tmp_path)
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text(
        '{"tokens":{"access_token":"test-token","account_id":"test-account"}}'
    )

    result = run_main(
        "--resume", "s", workspace=tmp_path, env={**os.environ, "HOME": str(tmp_path)}
    )

    assert result.returncode == 0
    assert "recoverable context folding enabled; compaction disabled" in result.stdout
    assert (sessions / "s.folds.sqlite3").exists()


def test_resume_rejects_an_incompatible_folding_ledger_at_the_cli_boundary(tmp_path):
    # Regression caught: a ledger written under another config or projection
    # template must fail closed with a CLI error and a released lock, not a
    # traceback from inside FoldingContext.
    sessions = persisted_folding_session(tmp_path, ledger=False)
    FoldingContext(
        sessions / "s.folds.sqlite3", "s", config=FoldConfig(min_span_tokens=1)
    ).close()

    result = run_main("--resume", "s", workspace=tmp_path)

    assert result.returncode == 2
    assert "cannot open folding ledger for s.jsonl" in result.stderr
    assert "resume config does not match" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (sessions / "s.lock").exists()


def test_resume_refuses_to_silently_replace_a_missing_folding_ledger(tmp_path):
    persisted_folding_session(tmp_path, ledger=False)

    result = run_main("--resume", "s", workspace=tmp_path)

    assert result.returncode == 2
    assert "folding ledger is missing" in result.stderr
