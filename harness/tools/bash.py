import subprocess

from harness.tools.base import Tool
from harness.truncate import truncate

# Deliberately uncaged: runs arbitrary shell as the process user.
# Lesson 7 gates it behind permissions; lesson 9 confines it in a sandbox.


def _run(command: str, timeout: int, output_limit: int) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"command timed out after {timeout}s"
    body = proc.stdout + proc.stderr
    return f"exit code: {proc.returncode}\n{truncate(body, output_limit)}"


def bash_tool(timeout: int = 30, output_limit: int = 8000) -> Tool:
    return Tool(
        name="bash",
        description=(
            "Run a shell command and return its exit code and combined "
            "stdout/stderr. Use for anything the other tools don't cover: "
            "grep, find, git, running scripts. Commands time out after "
            f"{timeout}s and long output is truncated in the middle."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                }
            },
            "required": ["command"],
        },
        execute=lambda command: _run(command, timeout, output_limit),
    )
