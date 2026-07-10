from typing import Callable

from harness.permissions import PermissionPolicy
from harness.tools.base import Tool


def exit_plan_mode_tool(
    policy: PermissionPolicy,
    approve: Callable[[str], tuple[bool, str]],
) -> Tool:
    """Present a plan and, on the human's approval, leave plan mode. `approve`
    is given the plan text and returns (approved, feedback). Approval restores
    policy.base_mode so the model may act in the same turn; rejection returns
    the feedback and stays in plan mode. A no-op outside plan mode."""

    def execute(plan: str) -> str:
        if policy.mode != "plan":
            return "Not in plan mode — this tool only applies while planning."
        approved, feedback = approve(plan)
        if approved:
            policy.mode = policy.base_mode
            return "Plan approved. Proceeding — you may now take the actions above."
        return f"Plan not approved; stay read-only and revise. Feedback: {feedback or '(none)'}"

    return Tool(
        name="exit_plan_mode",
        description=(
            "Call this once you have finished investigating and have a complete "
            "plan. Pass the plan as `plan`. The user reviews it; if they approve "
            "you leave plan mode and may act. Until then you are read-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "Your complete plan for the user to review."}
            },
            "required": ["plan"],
        },
        execute=execute,
        read_only=True,          # presenting a plan is read-only; the flip is user-consented
        spawns_subagents=True,   # top-level only — a subagent must not exit plan mode
    )
