from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.tools.base import Tool


@dataclass
class Skill:
    name: str
    description: str
    body: str


def _parse(text: str) -> tuple[str, str, str]:
    """Split a skill file into (name, description, body). Frontmatter is a
    leading `---` block of `key: value` lines — parsed by hand rather than
    pulling in a YAML dependency for two string fields."""
    if not text.startswith("---"):
        raise ValueError("missing '---' frontmatter block")
    _, frontmatter, body = text.split("---", 2)
    meta = {}
    for line in frontmatter.strip().splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"frontmatter line is not 'key: value': {line!r}")
        meta[key.strip()] = value.strip()
    if "name" not in meta or "description" not in meta:
        raise ValueError("frontmatter needs 'name' and 'description'")
    return meta["name"], meta["description"], body.strip()


def discover(
    skills_dir: Path, on_warning: Callable[[str], None] = print
) -> list[Skill]:
    """Load every skills/*.md file. A malformed skill is skipped with a
    warning, never fatal — one bad file must not sink the others."""
    skills = []
    for path in sorted(Path(skills_dir).glob("*.md")):
        try:
            name, description, body = _parse(path.read_text())
        except (OSError, ValueError) as error:
            on_warning(f"skipping skill {path.name}: {error}")
            continue
        skills.append(Skill(name=name, description=description, body=body))
    return skills


def skills_section(skills: list[Skill]) -> str | None:
    """The always-present metadata block: name + description only. Full
    bodies are pulled in on demand via view_skill, so an unused skill costs
    one line, not its whole content."""
    if not skills:
        return None
    lines = ["Available skills (call view_skill to load one in full):"]
    lines += [f"- {s.name}: {s.description}" for s in skills]
    return "\n".join(lines)


def view_skill_tool(skills: list[Skill]) -> Tool:
    bodies = {s.name: s.body for s in skills}

    def execute(name: str) -> str:
        if name not in bodies:
            available = ", ".join(sorted(bodies)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        return bodies[name]

    return Tool(
        name="view_skill",
        description=(
            "Load the full instructions for one of the available skills "
            "(listed in the system prompt) by name. Do this before a task "
            "the skill governs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name."}
            },
            "required": ["name"],
        },
        execute=execute,
        read_only=True,
    )
