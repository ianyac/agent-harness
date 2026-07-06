from typing import Callable

from harness.llm import LLMClient
from harness.loop import run_turn
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool

_DESCRIPTION = (
    "Delegate a self-contained task to a subagent: a fresh agent with no "
    "memory of this conversation, the same tools (except this one), and the "
    "same permissions. It works the task to completion and returns only its "
    "final answer — use it for exploration or multi-step subtasks whose "
    "intermediate output would crowd this conversation."
)


def agent_tool(
    llm: LLMClient,
    tools: dict[str, Tool],
    policy: PermissionPolicy | None = None,
    asker: Callable[[str, dict], str] | None = None,
    system: str | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    max_iterations: int = 20,
) -> Tool:
    """A subagent as a plain registry tool: fresh context in, one answer out.

    Context is isolated (the inner run_turn starts on an empty list and only
    the final reply's text comes back); authority is not elevated (same
    sandbox-wrapped tools, same policy object, same asker).
    """

    def execute(task: str) -> str:
        # filtered at call time: the registry dict is shared and gains
        # "agent" only after this tool is constructed — a subagent must
        # never find it there (no recursion, structurally)
        inner = {name: tool for name, tool in tools.items() if name != "agent"}
        reply = run_turn(
            [],  # fresh context: isolation is the whole point
            task,
            llm,
            tools=inner,
            max_iterations=max_iterations,
            on_tool_call=on_tool_call,
            policy=policy,
            asker=asker,
            system=system,
        )
        return reply["content"] or ""

    return Tool(
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
        # delegation itself changes nothing; every side effect inside the
        # sub passes the same permission gate individually
        read_only=True,
    )
