from harness.tools.base import Tool


def noop_tool(
    read_only: bool = True,
    name: str = "noop",
    spawns_subagents: bool = False,
) -> Tool:
    return Tool(
        name=name,
        description="A tool that does nothing, for tests.",
        parameters={"type": "object", "properties": {}},
        execute=lambda **args: "ok",
        read_only=read_only,
        spawns_subagents=spawns_subagents,
    )
