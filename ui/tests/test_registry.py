from pathlib import Path

import pytest

from harness.folding import FoldingContext
from harness.permissions import PermissionPolicy
from harness.sandbox import NoSandbox
from server.registry import build_registry


class FakeLLM:
    context_window = 10_000

    def complete(self, messages, tools=None, system=None, on_text_delta=None, projection_hash=None):
        return {"role": "assistant", "content": "done"}


class FakeSearch:
    name = "fake"

    def search(self, query: str, count: int = 5) -> list[dict]:
        return []


@pytest.fixture
def runtime_parts():
    return {
        "llm": FakeLLM(),
        "policy": PermissionPolicy("default"),
        "sandbox": NoSandbox(),
        "context": None,
        "compact_threshold": 8_000,
        "plan_reviewer": lambda plan: (False, "revise"),
    }


def write_skills(workspace: Path) -> None:
    (workspace / "skills").mkdir()
    (workspace / "skills" / "plain.md").write_text(
        "---\nname: plain\ndescription: plain\n---\nRead this guidance."
    )
    (workspace / "skills" / "command.md").write_text(
        "---\nname: command\ndescription: command\n---\n!`git status`"
    )


def test_v1_registry_excludes_hooks_mcp_and_skill_shell(
    tmp_path: Path, runtime_parts: dict
):
    write_skills(tmp_path)

    tools, skills = build_registry(workspace=tmp_path, **runtime_parts)

    assert set(tools) == {
        "read_file",
        "write_file",
        "list_dir",
        "bash",
        "web_fetch",
        "agent",
        "skill",
        "exit_plan_mode",
    }
    assert tools["skill"].execute(name="command") == (
        "[skill command not run: this agent cannot run shell commands]"
    )
    assert tools["skill"].execute(name="plain") == "Read this guidance."
    assert {skill.name for skill in skills} == {"command", "plain"}


def test_registry_adds_only_public_optional_search_and_folding_tools(
    tmp_path: Path, runtime_parts: dict
):
    context = FoldingContext(
        tmp_path / "s.folds.sqlite3",
        session_id="s",
        decision_log_path=tmp_path / "s.fold-decisions.jsonl",
        session_log_path=tmp_path / "s.jsonl",
    )
    try:
        tools, _ = build_registry(
            workspace=tmp_path,
            **{**runtime_parts, "context": context},
            search_provider=FakeSearch(),
        )
        assert {"fold", "unfold", "web_search"} <= tools.keys()
        assert tools["fold"].inheritable is False
        assert tools["unfold"].inheritable is False
    finally:
        context.close()


def test_exit_plan_mode_uses_the_runtime_plan_reviewer_signature(
    tmp_path: Path, runtime_parts: dict
):
    reviewed: list[str] = []

    def review(plan: str) -> tuple[bool, str]:
        reviewed.append(plan)
        return False, "add tests"

    policy = runtime_parts["policy"]
    policy.mode = "plan"
    tools, _ = build_registry(
        workspace=tmp_path,
        **{**runtime_parts, "plan_reviewer": review},
    )

    result = tools["exit_plan_mode"].execute(plan="one plan")

    assert reviewed == ["one plan"]
    assert result.endswith("Feedback: add tests")
    assert policy.mode == "plan"
