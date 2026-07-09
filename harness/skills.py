import re
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
    leading block delimited by lines that are exactly `---`, holding
    `key: value` pairs — parsed by hand rather than pulling in a YAML
    dependency for two string fields. Liberal in what it accepts (blank
    lines, `#` comments, and `---` inside values are fine) so a valid
    skill is never dropped over cosmetics."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing '---' frontmatter block")
    try:
        end = lines.index("---", 1)  # first delimiter LINE, not substring
    except ValueError:
        raise ValueError("frontmatter block is not closed with '---'") from None
    meta = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue  # blank lines and comments are not errors
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"frontmatter line is not 'key: value': {line!r}")
        meta[key.strip()] = value.strip()
    if "name" not in meta or "description" not in meta:
        raise ValueError("frontmatter needs 'name' and 'description'")
    return meta["name"], meta["description"], "\n".join(lines[end + 1 :]).strip()


def discover(
    skills_dir: Path, on_warning: Callable[[str], None] = print
) -> list[Skill]:
    """Load every skills/*.md file. A malformed skill is skipped with a
    warning, never fatal — one bad file must not sink the others."""
    skills = []
    seen: set[str] = set()
    for path in sorted(Path(skills_dir).glob("*.md")):
        try:
            # utf-8-sig: read UTF-8 with or without a BOM (some editors add
            # one), and never fall back to a locale codec that would drop a
            # skill over an em dash
            name, description, body = _parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            on_warning(f"skipping skill {path.name}: {error}")
            continue
        if name in seen:
            # a duplicate name would shadow the first in the skill tool's lookup;
            # keep the first, never silently serve the wrong body
            on_warning(f"skipping skill {path.name}: duplicate name {name!r}")
            continue
        seen.add(name)
        skills.append(Skill(name=name, description=description, body=body))
    return sorted(skills, key=lambda s: s.name)  # menu order = displayed names


def skills_section(skills: list[Skill]) -> str | None:
    """The always-present metadata block: name + description only. Full
    bodies are pulled in on demand by the skill tool, so an unused skill costs
    one line, not its whole content."""
    if not skills:
        return None
    lines = ["Available skills (call the skill tool to load one in full):"]
    lines += [f"- {s.name}: {s.description}" for s in skills]
    return "\n".join(lines)


# !`cmd` — at a token boundary (start of body or after whitespace): a bang, an
# optional \ escape, then a backtick-quoted non-empty command. The (?<![^\s])
# anchor keeps a bang buried in prose or an inline-code span — the `!` key,
# `foo!` `bar`, `!!` — from being read as a command (a bang glued to a word or
# backtick is not a command). `\!`cmd`` is a literal the skill can document.
_CMD = re.compile(r"(?<![^\s])(\\?)!`([^`]+)`")


def expand_body(body: str, run: Callable[[str], str]) -> str:
    """Replace each !`cmd` with run(cmd) at invocation time. `\\!`cmd`` is a
    literal — the backslash is stripped and the command is NOT run, so a skill
    can document the syntax. A raising run degrades to an inline marker rather
    than sinking the load."""

    def replace(match: "re.Match[str]") -> str:
        escaped, command = match.group(1), match.group(2)
        if escaped:
            return f"!`{command}`"  # literal: drop the backslash, do not run
        try:
            return run(command)
        except Exception as error:  # a bad block degrades, never raises
            return f"[skill command failed: {error}]"

    return _CMD.sub(replace, body)


def skill_tool(skills: list[Skill], run: Callable[[str], str] | None = None) -> Tool:
    """The skill tool. With `run`, a skill body's !`cmd` blocks execute and the
    body is not verbatim (read_only False). Without `run` (the default), bodies
    are returned verbatim and the tool is read-only — the lesson-15 behavior."""
    bodies = {s.name: s.body for s in skills}

    def execute(name: str) -> str:
        if name not in bodies:
            available = ", ".join(sorted(bodies)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        body = bodies[name]
        return body if run is None else expand_body(body, run)

    return Tool(
        name="skill",
        description=(
            "Load and run one of the available skills (listed in the system "
            "prompt) by name. Do this before a task the skill governs. Some "
            "skills run shell commands to gather live context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name."}
            },
            "required": ["name"],
        },
        execute=execute,
        read_only=run is None,
    )


# Deprecated compat alias: the ui lane still imports view_skill_tool. Calling it
# with no `run` yields the non-executing, read-only lesson-15 tool. The ui lane
# should migrate to skill_tool; tracked as a cross-lane coordination item.
view_skill_tool = skill_tool
