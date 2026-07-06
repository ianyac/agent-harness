import argparse
import datetime
import json
import platform
from pathlib import Path

from harness.llm import CodexAdapter
from harness.loop import run_turn
from harness.permissions import MODES, PermissionPolicy
from harness.prompts import Environment, build_system_prompt
from harness.sandbox import SandboxPolicy, default_sandbox
from harness.tools.bash import bash_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool

KEEP_RECENT = 8  # messages kept verbatim through a compaction
# fraction of the model's context window that triggers compaction; the rest
# is headroom for output tokens, mid-turn growth, and estimate bias
COMPACT_FRACTION = 0.8


def ask_user(name: str, args: dict) -> str:
    print(f"  agent wants to run: {name}({json.dumps(args)})")
    while True:
        answer = input("  allow? [y]es / [n]o / [a]lways for this tool: ").strip().lower()
        match answer:
            case "y" | "yes":
                return "yes"
            case "n" | "no":
                return "no"
            case "a" | "always":
                return "always"
            case _:
                print("  please answer y, n, or a")


def current_system_prompt(workspace: Path) -> str:
    # the one place real-world facts are read; rebuilt each turn so a
    # session that crosses midnight keeps the right date
    env = Environment(
        cwd=str(Path.cwd().resolve()),
        workspace=str(workspace),
        os=platform.platform(),
        date=datetime.date.today().isoformat(),
    )
    return build_system_prompt(env)


def main():
    parser = argparse.ArgumentParser(description="agent-harness REPL")
    parser.add_argument("--mode", choices=MODES, default="default")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="root the agent may read/write/run within (default: cwd)",
    )
    parser.add_argument(
        "--compact-threshold",
        type=int,
        default=None,
        help="token estimate above which old turns are summarized "
        "(default: 80%% of the model's context window)",
    )
    cli_args = parser.parse_args()

    workspace = cli_args.workspace.resolve()
    sandbox = default_sandbox(SandboxPolicy(workspace))

    # every executed tool call lands here, fresh per session, so compaction
    # can point at it instead of trusting the summary to carry the trail
    action_log = workspace / ".agent" / "actions.jsonl"
    action_log.parent.mkdir(exist_ok=True)
    action_log.write_text("")

    def observe_tool_call(name: str, args: dict) -> None:
        print(f"⚙ {name}({json.dumps(args)})")
        with action_log.open("a") as log:
            log.write(json.dumps({"name": name, "args": args}) + "\n")

    llm = CodexAdapter()
    compact_threshold = (
        cli_args.compact_threshold
        if cli_args.compact_threshold is not None
        else int(COMPACT_FRACTION * llm.context_window)
    )
    tools = {
        tool.name: tool
        for tool in [
            read_file_tool(workspace=workspace),
            write_file_tool(workspace=workspace),
            list_dir_tool(workspace=workspace),
            bash_tool(sandbox=sandbox),
        ]
    }
    policy = PermissionPolicy(cli_args.mode)
    messages = []
    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        entries = action_log.read_text().count("\n")
        try:
            reply = run_turn(
                messages,
                user_input,
                llm,
                tools=tools,
                on_tool_call=observe_tool_call,
                policy=policy,
                asker=ask_user,
                system=current_system_prompt(workspace),
                compact_threshold=compact_threshold,
                keep_recent=KEEP_RECENT,
                on_compact=lambda n: print(f"[compacted {n} messages into a summary]"),
                breadcrumbs=f"Action log: .agent/actions.jsonl ({entries} entries)",
            )
            print("agent:", reply["content"])
        except KeyboardInterrupt:
            # drop the half-built exchange: a dangling tool_call in history
            # would poison every later request. Compaction may have shifted
            # indices mid-turn, so roll back to the last completed exchange
            # rather than to a saved position.
            while messages and not (
                messages[-1]["role"] == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                messages.pop()
            print("\n(turn cancelled — conversation rolled back to last exchange)")


if __name__ == "__main__":
    main()
