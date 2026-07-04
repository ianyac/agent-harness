from pathlib import Path

from harness.tools.base import Tool


def _list_dir(path: str = ".") -> str:
    entries = sorted(Path(path).iterdir(), key=lambda e: e.name)
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)


def list_dir_tool() -> Tool:
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
        execute=_list_dir,
        read_only=True,
    )
