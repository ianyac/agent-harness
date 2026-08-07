import json
from typing import Callable

from harness.compaction import compact, estimate_tokens
from harness.folding import FoldingContext
from harness.llm import LLMClient
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool, definitions

# prefix of the reply run_turn fabricates when max_iterations is exhausted;
# wrappers (e.g. the agent tool) detect aborts by it
ABORTED_PREFIX = "[turn aborted by harness"


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
    system: str | Callable[[], str] | None = None,
    compact_threshold: int | None = None,
    keep_recent: int = 8,
    on_compact: Callable[[int], None] | None = None,
    breadcrumbs: str | Callable[[], str] | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    context: FoldingContext | None = None,
) -> dict:
    if context is not None and compact_threshold is not None:
        raise ValueError("context folding and compaction are mutually exclusive")
    tools = tools or {}
    defs = definitions(tools) or None
    if context is not None:
        context.begin_turn(messages, tools)
    messages.append({"role": "user", "content": user_input})
    for _ in range(max_iterations):
        # re-evaluated every iteration: a callable system prompt can change
        # mid-turn (e.g. plan mode is left after exit_plan_mode is approved,
        # so the plan-mode section must drop for the remaining iterations)
        sys_prompt = system() if callable(system) else system
        if context is not None:
            context.sync(messages, tools)
            if context.should_checkpoint(estimate_tokens(messages, defs, sys_prompt)):
                context.checkpoint(reason="marked token threshold")
        # re-checked every iteration: tool results can balloon the context
        # mid-turn, long after the turn-start estimate looked safe
        if (
            compact_threshold is not None
            and estimate_tokens(messages, defs, sys_prompt) > compact_threshold
        ):
            compacted = compact(
                messages,
                llm,
                keep_recent,
                # a callable is evaluated now, at fire time — a note built at
                # turn start goes stale once this turn's own calls land
                breadcrumbs=breadcrumbs() if callable(breadcrumbs) else breadcrumbs,
            )
            if compacted is not messages:
                summarized = len(messages) - (len(compacted) - 1)
                messages[:] = compacted  # in place: the caller owns this list
                if on_compact is not None:
                    on_compact(summarized)
        # the kwarg travels only when streaming is on: pre-seam LLMClient
        # implementations keep working until their caller opts in
        extra = {"on_text_delta": on_text_delta} if on_text_delta is not None else {}
        outgoing = context.project(messages) if context is not None else messages
        reply = llm.complete(outgoing, tools=defs, system=sys_prompt, **extra)
        messages.append(reply)
        if context is not None:
            context.sync(messages, tools)
        calls = reply.get("tool_calls")
        if not calls:
            return reply
        for call in calls:
            result = _run_one_call(call, tools, policy, asker, on_tool_call)
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
            if context is not None:
                context.sync(messages, tools)
    # cap hit: close the transcript gracefully — the law is that every turn
    # ends with a plain assistant message, even an unsuccessful one
    reply = {
        "role": "assistant",
        "content": f"{ABORTED_PREFIX}: no final answer after "
        f"{max_iterations} iterations]",
    }
    messages.append(reply)
    if context is not None:
        context.sync(messages, tools)
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
