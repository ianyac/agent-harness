from pathlib import Path

from harness.tools.base import Tool
from harness.tools.workspace import resolve_in_workspace
from harness.truncate import truncate


def _read_file(
    path: str,
    workspace: Path | None,
    offset: int | None = None,
    limit: int | None = None,
    char_limit: int = 10000,
) -> str:
    text = resolve_in_workspace(path, workspace).read_text()
    # partial read: slice lines and announce the slice so the model knows
    # there is more to read
    if offset is not None or limit is not None:
        lines = text.splitlines()
        start = offset or 0
        end = len(lines) if limit is None else start + limit
        shown = lines[start:end]
        header = f"[showing lines {start + 1}–{min(end, len(lines))} of {len(lines)}]"
        return "\n".join([header, *shown])
    # whole read: clip only if it would flood the context window
    return truncate(text, char_limit)


def read_file_tool(workspace: Path | None = None) -> Tool:
    return Tool(
        name="read_file",
        description=(
            "Read a text file and return its contents. Use this before "
            "summarizing or modifying a file. Fails if the file does not "
            "exist — use list_dir first when unsure of the path. Large files "
            "are truncated in the middle; pass offset (0-based line) and "
            "limit (line count) to page through a big file precisely."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, absolute or relative to the working directory.",
                },
                "offset": {
                    "type": "integer",
                    "description": "0-based line to start from (optional).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to return from offset (optional).",
                },
            },
            "required": ["path"],
        },
        execute=lambda path, offset=None, limit=None: _read_file(
            path, workspace, offset, limit
        ),
        read_only=True,
    )
