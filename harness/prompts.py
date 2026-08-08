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

WORKSPACE_HYGIENE = """## Workspace hygiene

Your context is your working memory. Keep it dense with what matters now.

When a line of work CLOSES — an investigation concludes, a hypothesis is
confirmed or ruled out, or a subtask completes — fold its evidence and record
your verdict:

  fold(span_id, reason, note)

Your note is what future-you will have INSTEAD of the content. State the
conclusion, the key facts supporting it, and anything you would otherwise
re-check: file paths, line numbers, and exact values.

Rules:
- Fold at natural pauses, after finishing and before starting the next thing.
  Never interrupt an active investigation to clean up.
- If you cannot write a specific note, do not fold. "No longer needed" means
  you have not finished thinking about the evidence.
- When unsure whether something is finished or merely irrelevant so far, keep
  it. Recovery is available through unfold, but fold only when confident.
- If content is WRONG, fold it as `poisoned` with a corrective note immediately.
- Hygiene serves the task; never pause a productive thread just to tidy.
- Evidence contradicting your current hypothesis is the last thing to fold,
  not the first.

Before moving on or saying something is confirmed, ask which evidence from the
phase just closed can be folded with a verdict."""


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
