from harness.tools.base import Tool


def noop_tool(read_only: bool = True) -> Tool:
    return Tool(
        name="noop",
        description="A tool that does nothing, for tests.",
        parameters={"type": "object", "properties": {}},
        execute=lambda **args: "ok",
        read_only=read_only,
    )
