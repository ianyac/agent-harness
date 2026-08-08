import json
from pathlib import Path

import pytest

from server.registry import build_registry, build_system
from server.runtime import HarnessRuntime, RuntimeConfig


class FakeLLM:
    context_window = 10_000

    def complete(self, messages, tools=None, system=None, on_text_delta=None, projection_hash=None):
        return {"role": "assistant", "content": "done"}


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
