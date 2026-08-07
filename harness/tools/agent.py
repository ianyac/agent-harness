from typing import Callable

from harness.llm import LLMClient
from harness.loop import ABORTED_PREFIX, run_turn
from harness.permissions import PermissionPolicy
from harness.tools.base import Tool

# A subagent-specific tool set: either a fixed {name: Tool} mapping, or a
# callable given the sub's filtered registry that returns one — so a caller can
# pick a build from what the sub will actually hold.
Substitutions = (
    dict[str, Tool] | Callable[[dict[str, Tool]], dict[str, Tool]] | None
)

_DESCRIPTION = (
    "Delegate a self-contained task to a subagent: a fresh agent with no "
    "memory of this conversation. It gets your tools except the ones that "
    "delegate, and some may be narrower versions — it cannot itself "
    "delegate, so do not send it work that depends on doing so. It "
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
    substitutions: "Substitutions" = None,
) -> str:
    """Run one subagent to completion and return its final answer. Its registry
    is DERIVED from the parent's: filter out delegating tools (the
    spawns_subagents recursion guard), then apply `substitutions` — a
    subagent-specific build of a tool, e.g. a skill tool made without fork_run,
    which cannot recurse and so is safe to hand down. Filtering alone can only
    REMOVE, so a tool unsafe in one configuration used to cost subagents the
    tool entirely.

    `substitutions` is a {name: Tool} mapping, or a callable given the filtered
    registry and returning one — the callable form lets a caller choose a build
    from what the sub will ACTUALLY hold (e.g. only hand over an executing
    skill tool when the sub also has `bash`, so a restriction stays a
    restriction). A substitution applies only to a name the caller actually
    offered, so a restricted registry (a fork skill's allowed-tools) stays
    authoritative. A subagent never prompts (asker=None → ask-decisions become
    denials)."""
    inner = {
        name: t
        for name, t in tools.items()
        if not t.spawns_subagents and t.inheritable
    }
    # a callable sees a COPY of the mapping: it is meant to choose a build from
    # what the sub will hold, not to add entries behind the checks below. (The
    # Tool objects inside are shared, as they are with the parent registry —
    # this guards the membership, not the tools themselves.)
    chosen = substitutions(dict(inner)) if callable(substitutions) else substitutions
    # validate the whole map BEFORE applying any of it, so a bad substitution is
    # caught the same way on every delegation — checking inside the per-name
    # offer test would make it fire only when that name happens to be offered
    for name, variant in (chosen or {}).items():
        if variant.spawns_subagents:
            raise ValueError(
                f"substitution {name!r} is a delegating tool; a subagent must "
                "never receive one"
            )
        if variant.name != name:
            raise ValueError(
                f"substitution key {name!r} does not match tool name "
                f"{variant.name!r}; the sub would be offered a tool the loop "
                "cannot dispatch"
            )
    for name, variant in (chosen or {}).items():
        # never smuggle a tool past the caller's restriction: substitution may
        # only REPLACE a name the caller offered, never add capability.
        # (Inherited tools keep the caller's own keys — that registry is the
        # caller's to construct; only substitutions are validated here.)
        if name in tools:
            inner[name] = variant
    reply = run_turn(
        [],
        task,
        llm,
        tools=inner,
        max_iterations=max_iterations,
        on_tool_call=on_tool_call,
        policy=policy,
        asker=None,
        # run_turn re-evaluates a callable system each iteration, so pass it
        # through rather than freezing it here — keeps the sub's env facts (date,
        # cwd) fresh over a long run and unifies both call sites on one idiom
        system=system,
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
    substitutions: "Substitutions" = None,
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
            keep_recent=keep_recent, substitutions=substitutions,
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
        foldable_inputs=("task",),
    )
    return tool
