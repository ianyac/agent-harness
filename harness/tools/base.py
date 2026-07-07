from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments object
    execute: Callable[..., str]  # kwargs from parsed arguments -> string result
    read_only: bool = False  # True = observes only; permissions trust this flag
    # True = must never appear in a subagent's registry (the recursion
    # guard); a field so the exclusion survives renaming and hook-wrapping
    spawns_subagents: bool = False

    def __post_init__(self):
        # fail at construction, not as a provider 400 mid-conversation
        if self.parameters.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: parameters must be type 'object'")
        properties = self.parameters.get("properties")
        if not isinstance(properties, dict):
            raise ValueError(f"tool {self.name!r}: parameters needs a properties dict")
        missing = set(self.parameters.get("required", [])) - properties.keys()
        if missing:
            raise ValueError(
                f"tool {self.name!r}: required args not in properties: {sorted(missing)}"
            )

    def definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def definitions(tools: dict[str, Tool]) -> list[dict]:
    return [tool.definition() for tool in tools.values()]
