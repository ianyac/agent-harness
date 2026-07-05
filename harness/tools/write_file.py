from pathlib import Path

from harness.tools.base import Tool
from harness.tools.workspace import resolve_in_workspace


def _write_file(path: str, content: str, workspace: Path | None) -> str:
    target = resolve_in_workspace(path, workspace)
    count = target.write_text(content)
    return f"wrote {count} characters to {path}"


def write_file_tool(workspace: Path | None = None) -> Tool:
    return Tool(
        name="write_file",
        description=(
            "Create or overwrite a text file with the given content. "
            "Overwrites without warning — read_file first if existing "
            "content matters. Returns a confirmation with the character "
            "count."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to create or overwrite.",
                },
                "content": {
                    "type": "string",
                    "description": "Full text content to write.",
                },
            },
            "required": ["path", "content"],
        },
        execute=lambda path, content: _write_file(path, content, workspace),
    )
