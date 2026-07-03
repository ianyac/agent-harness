from copy import deepcopy


class FakeLLM:
    def __init__(self, script: list[str]):
        self.script = script
        self.current_line = 0
        self.calls = []

    def complete(self, messages: list[dict[str, str]]) -> dict[str, str]:
        self.calls.append(deepcopy(messages))
        output = {"role": "assistant", "content": self.script[self.current_line]}
        self.current_line += 1
        return output


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
