import subprocess

from harness.sandbox import NoSandbox, Sandbox
from harness.tools.base import Tool
from harness.truncate import truncate


def run_sandboxed(
    command: str, sandbox: Sandbox, timeout: int = 30, output_limit: int = 8000
) -> str:
    try:
        proc = subprocess.run(
            sandbox.wrap(command),  # sandbox decides how the command runs
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"command timed out after {timeout}s"
    body = proc.stdout + proc.stderr
    return f"exit code: {proc.returncode}\n{truncate(body, output_limit)}"


def bash_tool(
    sandbox: Sandbox | None = None, timeout: int = 30, output_limit: int = 8000
) -> Tool:
    sandbox = sandbox or NoSandbox()  # default is uncaged; main.py opts in
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
        execute=lambda command: run_sandboxed(command, sandbox, timeout, output_limit),
    )
