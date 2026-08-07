import json

import pytest

from harness.folding import FoldConfig, FoldError, FoldingContext
from harness.tools.folding import fold_tool, unfold_tool
from tests.test_folding import rich_note, tool_exchange


def context_with_result(tmp_path):
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0),
    )
    messages = tool_exchange("noop", {}, "evidence")
    context.sync(messages)
    return context


def test_fold_tool_exposes_the_normative_reason_boundary_and_marks_a_span(tmp_path):
    # Regression caught: accepting scanner-only `sensitive` would let an
    # injected agent purge retained content through the public tool.
    context = context_with_result(tmp_path)
    tool = fold_tool(context)

    assert tool.parameters["properties"]["reason"]["enum"] == [
        "duplicate",
        "superseded",
        "finished",
        "irrelevant",
        "handled_failure",
        "scaffolding",
        "poisoned",
    ]
    assert tool.parameters["required"] == ["span_id", "reason", "note"]
    result = tool.execute(span_id="m2.r0", reason="finished", note=rich_note())
    assert "marked m2.r0" in result
    assert context.state("m2.r0") == "folded"


def test_fold_tool_schema_rejects_sensitive_before_execution(tmp_path):
    context = context_with_result(tmp_path)
    tool = fold_tool(context)
    assert "sensitive" not in tool.parameters["properties"]["reason"]["enum"]
    with pytest.raises(FoldError, match="invalid fold reason"):
        tool.execute(span_id="m2.r0", reason="sensitive", note=rich_note())


def test_unfold_tool_restores_full_content_not_an_excerpt(tmp_path):
    context = context_with_result(tmp_path)
    context.fold("m2.r0", "finished", rich_note())
    context.checkpoint()
    tool = unfold_tool(context)

    result = tool.execute(span_id="m2.r0")

    assert "reinstated m2.r0 in full" in result
    assert context.project(context.shadow_messages())[-1]["content"].endswith("evidence")


def test_control_tools_are_permission_safe_but_not_inheritable(tmp_path):
    # Regression caught: a subagent must never mutate its parent's fold ledger,
    # while the parent should not need filesystem-mutation approval for hygiene.
    context = context_with_result(tmp_path)
    for tool in (fold_tool(context), unfold_tool(context)):
        assert tool.read_only is True
        assert tool.inheritable is False


def test_fold_tool_definition_serializes_as_provider_json(tmp_path):
    # Boundary check: descriptions and schema travel through the existing Tool
    # definition seam without non-JSON values.
    definition = fold_tool(context_with_result(tmp_path)).definition()
    assert json.loads(json.dumps(definition))["function"]["name"] == "fold"
