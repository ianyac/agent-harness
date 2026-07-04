import json

from harness.llm import CodexAdapter
from harness.loop import run_turn
from harness.tools import Tool


def add_tool() -> Tool:  # toy tool; real ones arrive in lesson 5
    return Tool(
        name="add",
        description="Add two integers and return the sum.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        execute=lambda a, b: str(a + b),
    )


def main():
    llm = CodexAdapter()
    tools = {"add": add_tool()}
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
