from pathlib import Path

from harness.tools.base import Tool

# Deliberately unconfined until lesson 7 (permissions) and lesson 9 (sandbox).


def _read_file(path: str) -> str:
    return Path(path).read_text()


def read_file_tool() -> Tool:
    return Tool(
        name="read_file",
        description=(
            "Read a text file and return its full contents. Use this "
            "before summarizing or modifying a file. Fails if the file "
            "does not exist — use list_dir first when unsure of the path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, absolute or relative to the working directory.",
                }
            },
            "required": ["path"],
        },
        execute=_read_file,
    )
