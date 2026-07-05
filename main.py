import argparse
import json

from harness.llm import CodexAdapter
from harness.loop import run_turn
from harness.permissions import MODES, PermissionPolicy
from harness.tools.bash import bash_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool


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


def main():
    parser = argparse.ArgumentParser(description="agent-harness REPL")
    parser.add_argument("--mode", choices=MODES, default="default")
    cli_args = parser.parse_args()

    llm = CodexAdapter()
    tools = {
        tool.name: tool
        for tool in [
            read_file_tool(),
            write_file_tool(),
            list_dir_tool(),
            bash_tool(),
        ]
    }
    policy = PermissionPolicy(cli_args.mode)
    messages = []
    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        turn_start = len(messages)
        try:
            reply = run_turn(
                messages,
                user_input,
                llm,
                tools=tools,
                on_tool_call=lambda name, args: print(f"⚙ {name}({json.dumps(args)})"),
                policy=policy,
                asker=ask_user,
            )
            print("agent:", reply["content"])
        except KeyboardInterrupt:
            # drop the half-built exchange: a dangling tool_call in history
            # would poison every later request
            del messages[turn_start:]
            print("\n(turn cancelled — conversation rolled back to last exchange)")


if __name__ == "__main__":
    main()
