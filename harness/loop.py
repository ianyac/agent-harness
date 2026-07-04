import json
from typing import Callable

from harness.llm import LLMClient
from harness.tools.base import Tool, definitions


def run_turn(
    messages: list[dict],
    user_input: str,
    llm: LLMClient,
    tools: dict[str, Tool] | None = None,
    max_iterations: int = 20,  # crude runaway guard; real policy is lesson 8
    on_tool_call: Callable[[str, dict], None] | None = None,
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
            args = json.loads(fn["arguments"])
            if on_tool_call is not None:
                on_tool_call(fn["name"], args)
            result = tools[fn["name"]].execute(**args)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    raise RuntimeError(f"no final answer after {max_iterations} iterations")
