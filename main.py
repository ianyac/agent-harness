import json

from harness.llm import CodexAdapter
from harness.loop import run_turn
from harness.tools.bash import bash_tool
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool


def main():
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
    messages = []
    while True:
        try:
            user_input = input("You: ")
            reply = run_turn(
                messages,
                user_input,
                llm,
                tools=tools,
                on_tool_call=lambda name, args: print(f"⚙ {name}({json.dumps(args)})"),
            )
            print("agent:", reply["content"])
        except EOFError:
            break


if __name__ == "__main__":
    main()
