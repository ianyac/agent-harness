import datetime
import platform
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from harness.folding import FoldingContext
from harness.llm import LLMClient
from harness.permissions import PermissionPolicy
from harness.prompts import (
    Environment,
    PLAN_MODE,
    PLAN_MODE_SUBAGENT,
    WORKSPACE_HYGIENE,
    build_system_prompt,
)
from harness.sandbox import Sandbox
from harness.skills import (
    Skill,
    discover,
    has_cmd_blocks,
    skill_tool,
    skills_section,
)
from harness.tools.agent import agent_tool, run_subagent
from harness.tools.bash import bash_tool
from harness.tools.base import Tool
from harness.tools.folding import fold_tool, unfold_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.plan import exit_plan_mode_tool
from harness.tools.read_file import read_file_tool
from harness.tools.web import SearchProvider, web_fetch_tool, web_search_tool
from harness.tools.write_file import write_file_tool

if TYPE_CHECKING:
    from server.runtime import HarnessRuntime


PlanReviewer = Callable[[str], tuple[bool, str]]


def _system_prompt(
    workspace: Path,
    skills: list[Skill],
    policy: PermissionPolicy,
    tools: dict[str, Tool],
    *,
    subagent: bool = False,
) -> str:
    section = skills_section(skills)
    extra = [section] if section is not None else []
    if {"fold", "unfold"} <= tools.keys():
        extra.append(WORKSPACE_HYGIENE)
    if policy.mode == "plan":
        extra.append(PLAN_MODE_SUBAGENT if subagent else PLAN_MODE)
    if subagent:
        extra.insert(
            0,
            "You are a subagent. Complete the delegated task and return a complete answer.",
        )
    return build_system_prompt(
        Environment(
            cwd=str(Path.cwd().resolve()),
            workspace=str(workspace.resolve()),
            os=platform.platform(),
            date=datetime.date.today().isoformat(),
        ),
        extra_sections=extra,
    )


def build_system(runtime: "HarnessRuntime") -> str:
    return _system_prompt(
        runtime.workspace,
        runtime.skills,
        runtime.policy,
        runtime.tools,
    )


def _subagent_registry(
    offered: dict[str, Tool],
    skills: list[Skill],
) -> tuple[dict[str, Tool], dict[str, Tool], list[Skill]]:
    """Mirror the public agent derivation so prompts describe effective tools."""
    effective = {
        name: tool
        for name, tool in offered.items()
        if not tool.spawns_subagents and tool.inheritable
    }
    usable_skills = [
        skill for skill in skills if not skill.fork and not has_cmd_blocks(skill.body)
    ]
    substitutions: dict[str, Tool] = {}
    if "skill" in offered and usable_skills:
        replacement = skill_tool(usable_skills)
        substitutions["skill"] = replacement
        effective["skill"] = replacement
    prompt_skills = usable_skills if "skill" in effective else []
    return effective, substitutions, prompt_skills


def build_registry(
    runtime: "HarnessRuntime | None" = None,
    *,
    workspace: Path | None = None,
    llm: LLMClient | None = None,
    policy: PermissionPolicy | None = None,
    sandbox: Sandbox | None = None,
    context: FoldingContext | None = None,
    compact_threshold: int | None = None,
    plan_reviewer: PlanReviewer | None = None,
    search_provider: SearchProvider | None = None,
) -> tuple[dict[str, Tool], list[Skill]]:
    """Build the v1 registry exclusively from public harness factories."""
    if runtime is not None:
        return runtime.tools, runtime.skills
    if workspace is None or llm is None or policy is None or sandbox is None:
        raise TypeError("workspace, llm, policy, and sandbox are required")
    if plan_reviewer is None:
        raise TypeError("plan_reviewer is required")

    workspace = Path(workspace).resolve()
    skills = discover(workspace / "skills")
    native = [
        read_file_tool(workspace=workspace),
        write_file_tool(workspace=workspace),
        list_dir_tool(workspace=workspace),
        bash_tool(sandbox=sandbox),
        web_fetch_tool(),
    ]
    tools = {tool.name: tool for tool in native}
    if search_provider is not None:
        search = web_search_tool(search_provider)
        tools[search.name] = search
    if context is not None:
        tools["fold"] = fold_tool(context)
        tools["unfold"] = unfold_tool(context)

    if skills:

        def fork_run(
            task: str,
            _model: str | None,
            allowed_tools: list[str] | None,
        ) -> str:
            offered = (
                tools
                if allowed_tools is None
                else {
                    name: tool
                    for name, tool in tools.items()
                    if name in allowed_tools
                }
            )
            effective, substitutions, prompt_skills = _subagent_registry(
                offered, skills
            )

            def fork_system() -> str:
                return _system_prompt(
                    workspace,
                    prompt_skills,
                    policy,
                    effective,
                    subagent=True,
                )

            return run_subagent(
                task,
                llm,
                offered,
                policy=policy,
                system=fork_system,
                compact_threshold=compact_threshold,
                substitutions=substitutions,
            )

        tools["skill"] = skill_tool(skills, run=None, fork_run=fork_run)

    effective, substitutions, prompt_skills = _subagent_registry(tools, skills)

    def subagent_system() -> str:
        return _system_prompt(
            workspace,
            prompt_skills,
            policy,
            effective,
            subagent=True,
        )

    tools["agent"] = agent_tool(
        llm,
        tools,
        policy=policy,
        system=subagent_system,
        compact_threshold=compact_threshold,
        substitutions=substitutions,
    )
    tools["exit_plan_mode"] = exit_plan_mode_tool(policy, plan_reviewer)
    return tools, skills
