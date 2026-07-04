import json
from typing import Callable

from harness.llm import LLMClient
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool, definitions


def _permitted(
    tool: Tool,
    args: dict,
    policy: PermissionPolicy | None,
    asker: Callable[[str, dict], str] | None,
) -> bool:
    if policy is None:
        return True  # no-gate escape hatch: pre-lesson-7 behavior
    decision = policy.decide(tool)
    if decision == "ask":
        answer = asker(tool.name, args) if asker is not None else "no"
        if answer == "always":
            policy.session_allowlist.add(tool.name)
        decision = "allow" if answer in ("yes", "always") else "deny"
    return decision == "allow"


def run_turn(
    messages: list[dict],
    user_input: str,
    llm: LLMClient,
    tools: dict[str, Tool] | None = None,
    max_iterations: int = 20,  # crude runaway guard; real policy is lesson 8
    on_tool_call: Callable[[str, dict], None] | None = None,
    policy: PermissionPolicy | None = None,
    asker: Callable[[str, dict], str] | None = None,
) -> dict:
    tools = tools or {}
    defs = definitions(tools) or None
    messages.append({"role": "user", "content": user_input})
    for _ in range(max_iterations):
        reply = llm.complete(messages, tools=defs)
        messages.append(reply)
        calls = reply.get("tool_calls")
        if not calls:
            return reply
        for call in calls:
            fn = call["function"]
            name = fn["name"]
            args = json.loads(fn["arguments"])
            if not _permitted(tools[name], args, policy, asker):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": f"Permission denied: {name} was not allowed. "
                        "Do not retry unless the user asks for it differently.",
                    }
                )
                continue
            if on_tool_call is not None:
                on_tool_call(name, args)
            result = tools[name].execute(**args)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    raise RuntimeError(f"no final answer after {max_iterations} iterations")
