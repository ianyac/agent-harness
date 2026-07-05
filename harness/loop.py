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
    system: str | None = None,
) -> dict:
    tools = tools or {}
    defs = definitions(tools) or None
    messages.append({"role": "user", "content": user_input})
    for _ in range(max_iterations):
        reply = llm.complete(messages, tools=defs, system=system)
        messages.append(reply)
        calls = reply.get("tool_calls")
        if not calls:
            return reply
        for call in calls:
            result = _run_one_call(call, tools, policy, asker, on_tool_call)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    # cap hit: close the transcript gracefully — the law is that every turn
    # ends with a plain assistant message, even an unsuccessful one
    reply = {
        "role": "assistant",
        "content": f"[turn aborted by harness: no final answer after "
        f"{max_iterations} iterations]",
    }
    messages.append(reply)
    return reply


def _run_one_call(
    call: dict,
    tools: dict[str, Tool],
    policy: PermissionPolicy | None,
    asker: Callable[[str, dict], str] | None,
    on_tool_call: Callable[[str, dict], None] | None,
) -> str:
    """Execute one tool call, converting every failure into result text —
    each call must produce a result, or the transcript corrupts."""
    name = call["function"]["name"]
    if name not in tools:
        available = ", ".join(tools) or "none"
        return f"Error: unknown tool {name!r}. Available tools: {available}"
    try:
        args = json.loads(call["function"]["arguments"])
    except json.JSONDecodeError as error:
        return f"Error: arguments are not valid JSON ({error}). Retry the call."
    if not _permitted(tools[name], args, policy, asker):
        return (
            f"Permission denied: {name} was not allowed. "
            "Do not retry unless the user asks for it differently."
        )
    if on_tool_call is not None:
        on_tool_call(name, args)
    try:
        return tools[name].execute(**args)
    except Exception as error:  # noqa: BLE001 — the model handles it from here
        return f"Error: {type(error).__name__}: {error}"
