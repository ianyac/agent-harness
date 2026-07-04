from pathlib import Path

from harness.tools.base import Tool

# Deliberately unconfined until lesson 7 (permissions) and lesson 9 (sandbox).


def _write_file(path: str, content: str) -> str:
    count = Path(path).write_text(content)
    return f"wrote {count} characters to {path}"


def write_file_tool() -> Tool:
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
        execute=_write_file,
    )
