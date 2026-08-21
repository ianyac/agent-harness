import hashlib
import json
from typing import Callable

from harness.tools.base import Tool


def canonical(value) -> str:
    """The ledger's canonical JSON — the bytes projection hashes are taken over."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def exchange(question: str, answer: str) -> list[dict]:
    """A plain user/assistant pair."""
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


def history(n_exchanges: int) -> list[dict]:
    """Padded exchanges: enough tokens to cross a small compaction threshold."""
    messages = []
    for i in range(n_exchanges):
        messages.append({"role": "user", "content": f"question {i} " + "detail " * 30})
        messages.append({"role": "assistant", "content": f"answer {i} " + "detail " * 30})
    return messages


def tool_call(name: str, arguments: dict, call_id: str = "call_0") -> dict:
    """One entry of an assistant message's ``tool_calls``, in the wire shape."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def simple_tool(
    name: str,
    execute: Callable[..., str],
    *,
    description: str = "A test tool.",
    **fields,
) -> Tool:
    """A Tool with an empty argument schema: only name and behaviour vary.

    ``read_only`` keeps the Tool default (False) unless passed explicitly —
    folding records it as span metadata, so tests must opt in deliberately.
    """
    return Tool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        execute=execute,
        **fields,
    )


def add_tool() -> Tool:
    """A real two-argument tool — the loop and schema tests' canonical example."""
    return Tool(
        name="add",
        description="Add two integers and return the sum.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
        execute=lambda a, b: str(a + b),
    )


def write_skill(skills_dir, name, description, body, extra=""):
    """A flat skill file; ``extra`` is more frontmatter (e.g. "context: fork\n")."""
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"
    )


def write_dir_skill(skills_dir, dirname, name, description, body, extra=""):
    d = skills_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"
    )
    return d


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
