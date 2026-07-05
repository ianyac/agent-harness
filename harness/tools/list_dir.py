from pathlib import Path

from harness.tools.base import Tool
from harness.tools.workspace import resolve_in_workspace


def _list_dir(path: str = ".", workspace: Path | None = None) -> str:
    target = resolve_in_workspace(path, workspace)
    entries = sorted(target.iterdir(), key=lambda e: e.name)
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)


def list_dir_tool(workspace: Path | None = None) -> Tool:
    return Tool(
        name="list_dir",
        description=(
            "List a directory's entries, one per line, in name order; "
            "directories are marked with a trailing '/'. Defaults to the "
            "current working directory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list; defaults to '.'.",
                }
            },
        },
        execute=lambda path=".": _list_dir(path, workspace),
        read_only=True,
    )
