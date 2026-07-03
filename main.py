from typing import List, Dict


class FakeLLM:
    def __init__(self, script: List[str]):
        self.script = script
        self.current_line = 0
        self.calls = []

    def complete(self, messages: List[Dict[str, str]]) -> Dict[str, str]:
        self.calls.append(messages)
        response = self.script[self.current_line]
        self.current_line += 1
        return {"role": "assistant", "content": response}
