import argparse
import datetime
import json
import os
import platform
from pathlib import Path

from harness.llm import CodexAdapter
from harness.loop import run_turn
from harness.permissions import MODES, PermissionPolicy
from harness.prompts import Environment, build_system_prompt
from harness.sandbox import SandboxPolicy, default_sandbox
from harness.session import SessionLog
from harness.tools.agent import agent_tool
from harness.tools.bash import bash_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool

KEEP_RECENT = 8  # messages kept verbatim through a compaction
# fraction of the model's context window that triggers compaction; the rest
# is headroom for output tokens, mid-turn growth, and estimate bias
COMPACT_FRACTION = 0.8


def make_asker(actor: str):
    def ask(name: str, args: dict) -> str:
        print(f"  {actor} wants to run: {name}({json.dumps(args)})")
        while True:
            try:
                answer = (
                    input("  allow? [y]es / [n]o / [a]lways for this tool: ")
                    .strip()
                    .lower()
                )
            except EOFError:
                return "no"  # Ctrl-D at a prompt is a refusal, not a crash
            match answer:
                case "y" | "yes":
                    return "yes"
                case "n" | "no":
                    return "no"
                case "a" | "always":
                    return "always"
                case _:
                    print("  please answer y, n, or a")

    return ask


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
    parser.add_argument(
        "--resume",
        metavar="ID",
        default=None,
        help="resume the session with this id (filename stem under .agent/sessions)",
    )
    parser.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="resume the most recent session in this workspace",
    )
    cli_args = parser.parse_args()
    if cli_args.resume and cli_args.continue_:
        parser.error("--resume and --continue are mutually exclusive")

    workspace = cli_args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    if cli_args.compact_threshold is not None and cli_args.compact_threshold <= 0:
        parser.error("--compact-threshold must be a positive token count")
    sandbox = default_sandbox(SandboxPolicy(workspace))

    # every executed tool call lands here, one file per session (a second
    # REPL in the same workspace must not clobber this one's trail), so
    # compaction can point at it instead of trusting the summary
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    action_log = workspace / ".agent" / f"actions-{stamp}-{os.getpid()}.jsonl"
    sessions_dir = workspace / ".agent" / "sessions"
    try:
        action_log.parent.mkdir(exist_ok=True)
        action_log.write_text("")
        sessions_dir.mkdir(exist_ok=True)
    except OSError as error:
        parser.error(f"cannot prepare {action_log.parent}: {error}")

    if cli_args.continue_:
        # the stamp prefix makes lexicographic order chronological
        candidates = sorted(sessions_dir.glob("*.jsonl"))
        if not candidates:
            parser.error(f"no sessions to continue in {sessions_dir}")
        session_path = candidates[-1]
    elif cli_args.resume:
        name = cli_args.resume.removesuffix(".jsonl")
        session_path = sessions_dir / f"{name}.jsonl"
        if not session_path.exists():
            parser.error(f"no such session: {session_path}")
    else:
        session_path = sessions_dir / f"{stamp}-{os.getpid()}.jsonl"

    def record_action(name: str, args: dict) -> None:
        try:
            # self-heal: the agent's own tools can delete .agent mid-session
            action_log.parent.mkdir(exist_ok=True)
            with action_log.open("a") as log:
                log.write(json.dumps({"name": name, "args": args}) + "\n")
        except OSError as error:
            # the journal is observability; it must never kill the session
            print(f"(action log unavailable: {error})")

    def breadcrumb_note() -> str:
        # called at compaction fire time, so the count is current even
        # when this turn's own tool calls preceded the compaction
        try:
            entries = action_log.read_text().count("\n")
        except OSError:
            entries = 0
        # absolute path: read_file resolves against the workspace but bash
        # resolves against the process cwd — only absolute means both agree
        return f"Action log: {action_log} ({entries} entries)"

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
    ask_user = make_asker("agent")
    ask_subagent = make_asker("the subagent")
    # built once: the callable system prompt is re-evaluated per delegation,
    # so the sub's env facts (date, cwd) never go stale anyway
    tools["agent"] = agent_tool(
        llm,
        tools,
        policy=policy,
        asker=ask_subagent,
        system=lambda: current_subagent_prompt(workspace),
        on_tool_call=observe_sub_tool_call,
        compact_threshold=compact_threshold,
    )
    session = SessionLog(session_path)
    try:
        messages = session.load()
    except OSError as error:
        parser.error(f"cannot read session {session_path}: {error}")
    print(f"(session: {session_path.name})")
    if messages:
        print(f"(resumed {len(messages)} messages)")

    def record_turn() -> None:
        try:
            session.record_turn(messages)
        except OSError as error:
            # persistence is best-effort; it must never kill the session
            print(f"(session log unavailable: {error})")

    def on_compact(summarized: int) -> None:
        print(f"[compacted {summarized} messages into a summary]")
        try:
            # messages[0] is the freshly spliced-in summary; the cut count
            # equals what the loop reported
            session.record_compaction(cut=summarized, summary=messages[0])
        except OSError as error:
            print(f"(session log unavailable: {error})")

    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
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
                on_compact=on_compact,
                breadcrumbs=breadcrumb_note,
            )
            print("agent:", reply["content"])
            record_turn()
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
