from typing import Callable

from harness.llm import LLMClient
from harness.loop import ABORTED_PREFIX, run_turn
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool

_DESCRIPTION = (
    "Delegate a self-contained task to a subagent: a fresh agent with no "
    "memory of this conversation and the same tools (except this one). It "
    "runs unattended — it may use read-only tools and anything the user "
    "already granted, and every other action is denied rather than asked; "
    "it can never prompt the user. It returns only its final answer — use "
    "it for exploration or multi-step subtasks whose intermediate output "
    "would crowd this conversation."
)


def run_subagent(
    task: str,
    llm: LLMClient,
    tools: dict[str, Tool],
    *,
    policy: PermissionPolicy | None,
    system: str | Callable[[], str] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    max_iterations: int = 20,
    compact_threshold: int | None = None,
    keep_recent: int = 8,
) -> str:
    """Run one subagent to completion and return its final answer. A subagent
    never finds a delegation tool (the spawns_subagents recursion guard) and
    never prompts (asker=None → ask-decisions become denials)."""
    inner = {name: t for name, t in tools.items() if not t.spawns_subagents}
    reply = run_turn(
        [],
        task,
        llm,
        tools=inner,
        max_iterations=max_iterations,
        on_tool_call=on_tool_call,
        policy=policy,
        asker=None,
        system=system() if callable(system) else system,
        compact_threshold=compact_threshold,
        keep_recent=keep_recent,
    )
    content = reply["content"] or ""
    if content.startswith(ABORTED_PREFIX):
        return f"Error: subagent gave no final answer within {max_iterations} iterations"
    return content


def agent_tool(
    llm: LLMClient,
    tools: dict[str, Tool],
    *,
    # required: the caller must state the sub's authority envelope —
    # None disables the permission gate and has to be an explicit choice
    policy: PermissionPolicy | None,
    system: str | Callable[[], str] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    max_iterations: int = 20,
    compact_threshold: int | None = None,
    keep_recent: int = 8,
) -> Tool:
    """A subagent as a plain registry tool: fresh context in, one answer out.

    Subagents run in the background and never prompt the human: there is no
    asker, so permission decisions that would ask resolve to deny, which
    the sub receives as an ordinary tool result. Consent prompts happen
    only at parent level, where the human can see what they are approving —
    grants flow down through the shared policy, and nothing flows up.
    A callable system prompt is evaluated per delegation, so env facts
    never go stale. Note: compact_threshold is forwarded but a sub's
    single-turn transcript has no completed exchange to cut at — its real
    overflow guards are tool-result truncation and max_iterations.
    """

    def execute(task: str) -> str:
        return run_subagent(
            task, llm, tools,
            policy=policy, system=system, on_tool_call=on_tool_call,
            max_iterations=max_iterations, compact_threshold=compact_threshold,
            keep_recent=keep_recent,
        )

    tool = Tool(
        name="agent",
        description=_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Complete, self-contained instructions for the "
                        "subagent — it cannot see this conversation."
                    ),
                }
            },
            "required": ["task"],
        },
        execute=execute,
        # delegation itself changes nothing; the sub's own actions are
        # gated by the policy (with denial instead of prompting)
        read_only=True,
        spawns_subagents=True,
    )
    return tool
