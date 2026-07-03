# TEMP: REPL runs on the fake until the codex adapter lands (task 6)
from tests.fake_llm import FakeLLM


def run_turn(messages: list[dict[str, str]], user_input: str, llm) -> dict[str, str]:
    messages.append({"role": "user", "content": user_input})
    response = llm.complete(messages)
    messages.append(response)
    return response


def main():
    llm = FakeLLM(["Hello! How can I help you?", "Goodbye!"])
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
