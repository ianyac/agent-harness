"""Real wiring: the web equivalent of main.py's REPL setup."""

import argparse
import datetime
import platform
from pathlib import Path

import uvicorn

from harness.llm import CodexAdapter
from harness.permissions import MODES, PermissionPolicy
from harness.prompts import Environment, build_system_prompt
from harness.sandbox import SandboxPolicy, default_sandbox
from harness.tools.bash import bash_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool

from ui.server.app import HarnessDeps, create_app

KEEP_RECENT = 8  # mirror main.py
COMPACT_FRACTION = 0.8

SUBAGENT_SECTION = (
    "You are a subagent: another agent delegated one self-contained "
    "task to you. Work it to completion and make your final reply "
    "the complete answer — it is the only thing the delegating "
    "agent will see."
)


def environment(workspace: Path) -> Environment:
    return Environment(
        cwd=str(Path.cwd().resolve()),
        workspace=str(workspace),
        os=platform.platform(),
        date=datetime.date.today().isoformat(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-harness web UI server")
    parser.add_argument("--mode", choices=MODES, default="default")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="root the agent may read/write/run within (default: cwd)",
    )
    parser.add_argument("--host", default="127.0.0.1")  # localhost only: no auth
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    sandbox = default_sandbox(SandboxPolicy(workspace))
    llm = CodexAdapter()
    tools = {
        tool.name: tool
        for tool in [
            read_file_tool(workspace=workspace),
            write_file_tool(workspace=workspace),
            list_dir_tool(workspace=workspace),
            bash_tool(sandbox=sandbox),
        ]
    }
    deps = HarnessDeps(
        llm=llm,
        tools=tools,
        policy_factory=lambda: PermissionPolicy(args.mode),
        system_prompt=lambda: build_system_prompt(environment(workspace)),
        subagent_system_prompt=lambda: build_system_prompt(
            environment(workspace), extra_sections=[SUBAGENT_SECTION]
        ),
        mode=args.mode,
        workspace=str(workspace),
        compact_threshold=int(COMPACT_FRACTION * llm.context_window),
        keep_recent=KEEP_RECENT,
    )
    uvicorn.run(create_app(deps), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
