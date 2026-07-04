from harness.llm import CodexAdapter
from harness.loop import run_turn


def main():
    llm = CodexAdapter()
    messages = []
    while True:
        try:
            user_input = input("You: ")
            reply = run_turn(messages, user_input, llm)
            print("agent:", reply["content"])
        except EOFError:
            break


if __name__ == "__main__":
    main()
