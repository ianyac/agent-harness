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
from harness.tools.agent import agent_tool
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


def environment(workspace: Path) -> Environment:
    # the one place real-world facts are read; rebuilt each turn so a
    # session that crosses midnight keeps the right date
    return Environment(
        cwd=str(Path.cwd().resolve()),
        workspace=str(workspace),
        os=platform.platform(),
        date=datetime.date.today().isoformat(),
    )


def current_system_prompt(workspace: Path) -> str:
    return build_system_prompt(environment(workspace))


def current_subagent_prompt(workspace: Path) -> str:
    # same core prompt, plus the role section — the extra_sections seam
    return build_system_prompt(
        environment(workspace),
        extra_sections=[
            "You are a subagent: another agent delegated one self-contained "
            "task to you. Work it to completion and make your final reply "
            "the complete answer — it is the only thing the delegating "
            "agent will see."
        ],
    )


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
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    sandbox = default_sandbox(SandboxPolicy(workspace))

    # every executed tool call lands here, fresh per session, so compaction
    # can point at it instead of trusting the summary to carry the trail
    action_log = workspace / ".agent" / "actions.jsonl"
    try:
        action_log.parent.mkdir(exist_ok=True)
    except FileExistsError:
        parser.error(f"{action_log.parent} exists and is not a directory")
    action_log.write_text("")

    def record_action(name: str, args: dict) -> None:
        # self-heal: the agent's own tools can delete .agent mid-session
        action_log.parent.mkdir(exist_ok=True)
        with action_log.open("a") as log:
            log.write(json.dumps({"name": name, "args": args}) + "\n")

    def observe_tool_call(name: str, args: dict) -> None:
        print(f"⚙ {name}({json.dumps(args)})")
        record_action(name, args)

    def observe_sub_tool_call(name: str, args: dict) -> None:
        print(f"  ⚙↳ {name}({json.dumps(args)})")
        record_action(name, args)

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
        entries = action_log.read_text().count("\n") if action_log.exists() else 0
        # refreshed per turn like the system prompt, so the sub's env facts
        # (date, cwd) never go stale; the closure shares this same registry
        tools["agent"] = agent_tool(
            llm,
            tools,
            policy=policy,
            asker=ask_user,
            system=current_subagent_prompt(workspace),
            on_tool_call=observe_sub_tool_call,
        )
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
                # absolute: read_file resolves against the workspace but
                # bash resolves against the process cwd — only an absolute
                # path means the same file to both recovery tools
                breadcrumbs=f"Action log: {action_log} ({entries} entries)",
            )
            print("agent:", reply["content"])
        except KeyboardInterrupt:
            # drop the half-built exchange: a dangling tool_call in history
            # would poison every later request. Compaction may have shifted
            # indices mid-turn, so roll back to the last completed exchange
            # rather than to a saved position.
            dropped = 0
            while messages and not (
                messages[-1]["role"] == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                messages.pop()
                dropped += 1
            if dropped:
                print(f"\n(turn cancelled — {dropped} unfinished messages dropped)")
            else:
                print("\n(turn already complete — nothing to roll back)")


if __name__ == "__main__":
    main()
