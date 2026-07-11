from dataclasses import dataclass

PLAN_MODE = (
    "Plan mode: you are investigating and proposing, not acting. Use only "
    "read-only tools (read files, list directories) — do NOT modify files or "
    "run commands (bash and every mutating tool are denied here, including for "
    "search). When you have a complete plan, call exit_plan_mode with it and "
    "wait for the user to approve before doing anything."
)

# Subagents share the plan-mode policy but cannot exit it (exit_plan_mode is
# top-level only), so they get this note instead of PLAN_MODE: read-only,
# report — no mention of a gate tool they don't have.
PLAN_MODE_SUBAGENT = (
    "This is a read-only planning turn: use only read-only tools (read files, "
    "list directories). Any attempt to modify files or run commands (bash "
    "included, so no shell search) is denied. Investigate and report what you "
    "find — do not act."
)


@dataclass
class Environment:
    """Facts about the world the model cannot sense on its own."""

    cwd: str
    workspace: str
    os: str
    date: str


def build_system_prompt(env: Environment, extra_sections: list[str] | None = None) -> str:
    """Assemble the system prompt from ordered sections. Sections are data so
    later lessons (e.g. skills) can inject their own."""
    sections = [
        "You are a coding agent operating inside a command-line harness. "
        "You complete the user's tasks by reading, writing, and running code "
        "with the tools you are given.",
        (
            "Environment:\n"
            f"- Working directory: {env.cwd}\n"
            f"- Workspace root: {env.workspace}\n"
            f"- Operating system: {env.os}\n"
            f"- Today's date: {env.date}"
        ),
        (
            "Using tools:\n"
            "- Prefer tools over guessing; read a file before summarizing or "
            "editing it, and list a directory when unsure of a path.\n"
            "- Relative paths in tool calls resolve against the workspace "
            "root above, not the working directory.\n"
            "- File tools refuse paths outside the workspace root. Shell "
            "commands are blocked from writing outside it where the platform "
            "supports sandboxing, but may still be able to read elsewhere.\n"
            "- Tool results are the ground truth — trust them over your "
            "assumptions."
        ),
    ]
    sections.extend(extra_sections or [])
    return "\n\n".join(sections)
