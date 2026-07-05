from dataclasses import dataclass


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
            "- File and shell access is confined to the workspace root above; "
            "paths outside it will be refused.\n"
            "- Tool results are the ground truth — trust them over your "
            "assumptions."
        ),
    ]
    sections.extend(extra_sections or [])
    return "\n\n".join(sections)
