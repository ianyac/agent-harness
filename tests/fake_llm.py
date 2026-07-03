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
