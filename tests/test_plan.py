from harness.permissions import PermissionPolicy
from harness.tools.plan import exit_plan_mode_tool


def test_approved_plan_restores_base_mode():
    policy = PermissionPolicy("default")
    policy.mode = "plan"
    tool = exit_plan_mode_tool(policy, approve=lambda plan: (True, ""))
    result = tool.execute(plan="1. do the thing")
    assert policy.mode == "default"  # restored to base
    assert "approved" in result.lower()


def test_rejected_plan_stays_in_plan_and_returns_feedback():
    policy = PermissionPolicy("default")
    policy.mode = "plan"
    tool = exit_plan_mode_tool(policy, approve=lambda plan: (False, "also update tests"))
    result = tool.execute(plan="p")
    assert policy.mode == "plan"
    assert "also update tests" in result


def test_exit_plan_mode_is_a_noop_outside_plan_mode():
    policy = PermissionPolicy("default")  # not in plan
    calls = []
    tool = exit_plan_mode_tool(policy, approve=lambda plan: calls.append(plan) or (True, ""))
    result = tool.execute(plan="p")
    assert calls == []                 # approve never consulted
    assert policy.mode == "default"
    assert "not in plan mode" in result.lower()


def test_exit_plan_mode_flags():
    tool = exit_plan_mode_tool(PermissionPolicy("default"), approve=lambda p: (True, ""))
    assert tool.read_only is True and tool.spawns_subagents is True
