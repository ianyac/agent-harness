import json

from harness.llm import LLMClient
from harness.tools import Tool, definitions


def run_turn(
    messages: list[dict],
    user_input: str,
    llm: LLMClient,
    tools: dict[str, Tool] | None = None,
    max_iterations: int = 20,  # crude runaway guard; real policy is lesson 8
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
            result = tools[fn["name"]].execute(**json.loads(fn["arguments"]))
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    raise RuntimeError(f"no final answer after {max_iterations} iterations")
