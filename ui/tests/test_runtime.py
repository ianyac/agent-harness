import json
from pathlib import Path

import pytest

from harness.sandbox import NoSandbox
from server.registry import build_registry, build_system
from server.runtime import HarnessRuntime, RuntimeConfig


class FakeLLM:
    context_window = 10_000

    def complete(self, messages, tools=None, system=None, on_text_delta=None, projection_hash=None):
        return {"role": "assistant", "content": "done"}


class InvalidWindowLLM(FakeLLM):
    context_window = 0


def config(workspace: Path, **changes) -> RuntimeConfig:
    values = {
        "session_id": "s1",
        "workspace": workspace,
        "mode": "default",
        "context_mode": "compaction",
        "compact_threshold": None,
    }
    values.update(changes)
    return RuntimeConfig(**values)


@pytest.mark.parametrize("session_id", ["", "../s1", "space id"])
def test_runtime_config_rejects_unsafe_session_id(session_id: str, tmp_path: Path):
    with pytest.raises(ValueError, match="session_id"):
        config(tmp_path, session_id=session_id)


def test_runtime_locks_before_loading_and_owns_authoritative_session_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    message = {"role": "assistant", "content": "authoritative"}
    session.write_text(json.dumps({"type": "message", "message": message}) + "\n")

    from server import runtime as runtime_module

    original_load = runtime_module.SessionLog.load

    def load_while_locked(log):
        assert session.with_suffix(".lock").exists()
        return original_load(log)

    monkeypatch.setattr(runtime_module.SessionLog, "load", load_while_locked)
    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)
    try:
        assert runtime.messages == [message]
        assert runtime.session_log.path == session
        assert session.read_text().count("authoritative") == 1
    finally:
        runtime.close()


def test_runtime_close_releases_lock_idempotently(tmp_path: Path):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)

    assert session.with_suffix(".lock").exists()
    runtime.close()
    runtime.close()

    assert not session.with_suffix(".lock").exists()


def test_runtime_failure_releases_lock(tmp_path: Path):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"

    with pytest.raises(ValueError, match="invalid requested context mode"):
        HarnessRuntime(
            config(tmp_path, context_mode="automatic"), FakeLLM(), session
        )

    assert not session.with_suffix(".lock").exists()


@pytest.mark.parametrize("session_id", ["", "../s1", "space id", "other"])
def test_runtime_rejects_invalid_or_mismatched_session_ids_before_artifacts(
    tmp_path: Path, session_id: str
):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"

    with pytest.raises(ValueError, match="session_id"):
        HarnessRuntime(
            config(tmp_path, session_id=session_id), FakeLLM(), session
        )

    assert not session.with_suffix(".lock").exists()
    assert not session.with_suffix(".context-mode").exists()


def test_non_resource_validation_cannot_persist_context_artifacts(tmp_path: Path):
    folding_session = tmp_path / "invalid-mode" / "s1.jsonl"
    with pytest.raises(ValueError):
        HarnessRuntime(
            config(tmp_path, mode="plan", context_mode="folding"),
            FakeLLM(),
            folding_session,
        )
    assert not folding_session.with_suffix(".context-mode").exists()
    assert not folding_session.with_suffix(".folds.sqlite3").exists()
    assert not folding_session.with_suffix(".fold-decisions.jsonl").exists()
    assert not folding_session.with_suffix(".lock").exists()

    compaction_session = tmp_path / "invalid-window" / "s1.jsonl"
    with pytest.raises(ValueError, match="context_window"):
        HarnessRuntime(config(tmp_path), InvalidWindowLLM(), compaction_session)
    assert not compaction_session.with_suffix(".context-mode").exists()
    assert not compaction_session.with_suffix(".lock").exists()


def test_late_constructor_failure_rolls_back_only_new_context_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from server import runtime as runtime_module

    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    session.touch()
    ledger = session.with_suffix(".folds.sqlite3")
    ledger.touch()

    def fail_registry(**_kwargs):
        raise RuntimeError("registry failed")

    monkeypatch.setattr(runtime_module, "build_registry", fail_registry)

    with pytest.raises(RuntimeError, match="registry failed"):
        HarnessRuntime(config(tmp_path), FakeLLM(), session)

    assert ledger.exists()
    assert not session.with_suffix(".context-mode").exists()
    assert not session.with_suffix(".fold-decisions.jsonl").exists()
    assert not session.with_suffix(".lock").exists()

    new_session = tmp_path / ".agent" / "sessions" / "s2.jsonl"
    with pytest.raises(RuntimeError, match="registry failed"):
        HarnessRuntime(
            config(tmp_path, session_id="s2", context_mode="folding"),
            FakeLLM(),
            new_session,
        )
    assert not new_session.with_suffix(".context-mode").exists()
    assert not new_session.with_suffix(".folds.sqlite3").exists()
    assert not new_session.with_suffix(".fold-decisions.jsonl").exists()
    assert not new_session.with_suffix(".lock").exists()

    existing_session = tmp_path / ".agent" / "sessions" / "s3.jsonl"
    existing_mode = existing_session.with_suffix(".context-mode")
    existing_mode.write_text("compaction\n")
    with pytest.raises(RuntimeError, match="registry failed"):
        HarnessRuntime(
            config(tmp_path, session_id="s3"), FakeLLM(), existing_session
        )
    assert existing_mode.read_text() == "compaction\n"


def test_runtime_close_retries_unlock_without_double_closing_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from server import runtime as runtime_module

    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)
    original_unlock = runtime_module.unlock
    calls = 0

    def unlock_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unlock failed")
        original_unlock(path)

    monkeypatch.setattr(runtime_module, "unlock", unlock_once)

    with pytest.raises(RuntimeError, match="unlock failed"):
        runtime.close()
    assert session.with_suffix(".lock").exists()
    runtime.close()
    runtime.close()

    assert calls == 2
    assert not session.with_suffix(".lock").exists()


def test_constructor_preserves_original_error_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from server import runtime as runtime_module

    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    original_unlock = runtime_module.unlock
    unlock_calls = 0

    def fail_registry(**_kwargs):
        raise ValueError("original construction error")

    def fail_first_unlock(path):
        nonlocal unlock_calls
        unlock_calls += 1
        if unlock_calls == 1:
            raise RuntimeError("cleanup unlock error")
        original_unlock(path)

    monkeypatch.setattr(runtime_module, "build_registry", fail_registry)
    monkeypatch.setattr(runtime_module, "unlock", fail_first_unlock)

    with pytest.raises(ValueError, match="original construction error") as raised:
        HarnessRuntime(config(tmp_path), FakeLLM(), session)

    cleanup_errors = getattr(raised.value, "cleanup_errors")
    assert len(cleanup_errors) == 1
    assert str(cleanup_errors[0]) == "cleanup unlock error"
    assert getattr(raised.value, "cleanup_state") == {
        "context_owned": False,
        "lock_held": True,
    }
    assert "cleanup incomplete" in "\n".join(raised.value.__notes__)
    assert session.with_suffix(".lock").exists()
    runtime_module.unlock(session)


def test_runtime_registry_system_and_late_plan_reviewer_binding(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "guide.md").write_text(
        "---\nname: guide\ndescription: useful guide\n---\nUse the guide."
    )
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)
    try:
        assert build_registry(runtime)[0] is runtime.tools
        system = build_system(runtime)
        assert f"Workspace root: {tmp_path.resolve()}" in system
        assert "guide: useful guide" in system

        reviewed: list[str] = []

        def reviewer(plan: str) -> tuple[bool, str]:
            reviewed.append(plan)
            return True, ""

        runtime.bind_plan_reviewer(reviewer)
        runtime.policy.mode = "plan"
        result = runtime.tools["exit_plan_mode"].execute(plan="ship it")
        assert reviewed == ["ship it"]
        assert result.startswith("Plan approved")
        assert runtime.policy.mode == "default"
    finally:
        runtime.close()


def test_safety_snapshot_reports_exact_decisions_boundaries_and_egress(
    tmp_path: Path,
):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)
    try:
        snapshot = runtime.safety_snapshot()
        assert snapshot.mode == "default"
        assert snapshot.sandbox_backend == type(runtime.sandbox).__name__
        assert snapshot.workspace_write_boundary == str(tmp_path.resolve())
        assert snapshot.read_breadth == "host"
        assert snapshot.network_policy == "deny"
        assert snapshot.tools["read_file"].decision == "allow"
        assert snapshot.tools["write_file"].decision == "ask"
        assert snapshot.tools["bash"].decision == "ask"
        assert snapshot.tools["web_fetch"].decision == "allow"
        assert snapshot.tools["web_fetch"].network_egress is True
        assert snapshot.tools["read_file"].network_egress is False

        runtime.policy.mode = "readOnly"
        readonly = runtime.safety_snapshot()
        assert readonly.tools["write_file"].decision == "deny"
        assert readonly.tools["bash"].decision == "deny"
    finally:
        runtime.close()


def test_no_sandbox_snapshot_reports_effective_unrestricted_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from server import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "default_sandbox", lambda policy: NoSandbox())
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)
    try:
        snapshot = runtime.safety_snapshot()
        assert snapshot.sandbox_backend == "NoSandbox"
        assert snapshot.workspace_write_boundary == "unenforced"
        assert snapshot.read_breadth == "unrestricted"
        assert snapshot.network_policy == "allow"
        assert snapshot.tools["web_fetch"].network_egress is True
    finally:
        runtime.close()


def test_folding_runtime_uses_documented_artifacts_and_no_compaction(
    tmp_path: Path,
):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    runtime = HarnessRuntime(
        config(tmp_path, context_mode="folding"), FakeLLM(), session
    )
    try:
        assert runtime.context.mode == "folding"
        assert runtime.compact_threshold is None
        assert runtime.folding is not None
        assert session.with_suffix(".folds.sqlite3").exists()
        assert {"fold", "unfold"} <= runtime.tools.keys()
        assert "Workspace hygiene" in build_system(runtime)
    finally:
        runtime.close()


def test_legacy_folding_ledger_wins_before_default_compaction_threshold(
    tmp_path: Path,
):
    session = tmp_path / ".agent" / "sessions" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    session.touch()
    session.with_suffix(".folds.sqlite3").touch()

    runtime = HarnessRuntime(config(tmp_path), FakeLLM(), session)
    try:
        assert runtime.context.mode == "folding"
        assert runtime.compact_threshold is None
        assert runtime.folding is not None
    finally:
        runtime.close()
