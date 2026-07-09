import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.llm import CONTEXT_WINDOWS
from harness.tools.base import Tool


@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: Path
    fork: bool = False
    model: str | None = None
    allowed_tools: list[str] | None = None


def _parse(text: str) -> tuple[dict, str]:
    """Split a skill file into (frontmatter dict, body). Frontmatter is a
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
    return meta, "\n".join(lines[end + 1 :]).strip()


def discover(
    skills_dir: Path, on_warning: Callable[[str], None] = print
) -> list[Skill]:
    """Load every skill under skills/. A skill is either a flat `<name>.md`
    file or a `<name>/SKILL.md` directory (which may bundle files referenced
    from the body via ${SKILL_DIR}). A malformed skill is skipped with a
    warning, never fatal — one bad skill must not sink the others."""
    skills = []
    seen: set[str] = set()
    skills_dir = Path(skills_dir)
    entries = sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []
    for entry in entries:
        if entry.is_file() and entry.suffix == ".md":
            source, base = entry, skills_dir  # flat skill (legacy)
        elif entry.is_dir() and (entry / "SKILL.md").is_file():
            source, base = entry / "SKILL.md", entry  # directory skill
        elif entry.suffix == ".md":
            # a .md-named entry that is not a readable file (a directory or a
            # dangling symlink named foo.md) — pre-lesson-19 this warned via
            # read_text; keep the signal rather than silently dropping a typo
            on_warning(f"skipping skill {entry.name}: expected a readable .md file")
            continue
        else:
            continue  # unrelated file, or a dir without SKILL.md — not a skill
        try:
            # utf-8-sig: read UTF-8 with or without a BOM (some editors add one)
            meta, body = _parse(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            on_warning(f"skipping skill {entry.name}: {error}")
            continue
        model = meta.get("model")
        if model is not None and model not in CONTEXT_WINDOWS:
            on_warning(f"skipping skill {entry.name}: unknown model {model!r}")
            continue
        name = meta["name"]
        body = body.replace("${SKILL_DIR}", str(base))  # a fixed path, resolved once
        if name in seen:
            # a duplicate name would shadow the first in the skill tool's lookup;
            # keep the first, never silently serve the wrong body
            on_warning(f"skipping skill {entry.name}: duplicate name {name!r}")
            continue
        seen.add(name)
        skills.append(
            Skill(
                name=name,
                description=meta["description"],
                body=body,
                dir=base,
                fork=meta.get("context") == "fork",
                model=model,
                allowed_tools=(
                    meta["allowed-tools"].split() if "allowed-tools" in meta else None
                ),
            )
        )
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


def cmd_blocks(body: str) -> list[str]:
    """The commands a body will actually run, in order (escaped `\\!`x`` excluded)."""
    return [m.group(2) for m in _CMD.finditer(body) if not m.group(1)]


def has_cmd_blocks(body: str) -> bool:
    """True iff the body contains at least one real (unescaped) command."""
    return any(not m.group(1) for m in _CMD.finditer(body))


_ARG = re.compile(r"\$(ARGUMENTS|[1-9])")


def substitute_args(body: str, args: str) -> str:
    """Replace $ARGUMENTS (the whole string) and $1..$9 (whitespace-split
    positionals; missing → "") in one pass, so an arg that itself contains a
    $-token is never re-expanded."""
    parts = args.split()

    def repl(match: "re.Match[str]") -> str:
        token = match.group(1)
        if token == "ARGUMENTS":
            return args
        i = int(token)
        return parts[i - 1] if i <= len(parts) else ""

    return _ARG.sub(repl, body)


def expand_body(body: str, run: Callable[[str], str] | None, args: str = "") -> str:
    """Expand a skill body at invocation. `!`cmd`` spans are located on THIS body
    — the template the human approved at session start — then each command has
    $args filled and is run via `run`. Prose between commands gets $args but is
    NOT re-scanned for commands, so an arg containing !`...` lands in prose,
    inert: args can FILL an approved command but never INTRODUCE a new one.
    `\\!`cmd`` is a literal; run=None leaves commands unrun (verbatim)."""
    out: list[str] = []
    last = 0
    for match in _CMD.finditer(body):
        out.append(substitute_args(body[last : match.start()], args))  # prose: args, never a command
        escaped, command = match.group(1), match.group(2)
        filled = substitute_args(command, args)
        if escaped or run is None:
            out.append(f"!`{filled}`")  # literal (escaped) or non-executing
        else:
            try:
                out.append(run(filled))
            except Exception as error:  # a bad block degrades, never raises
                out.append(f"[skill command failed: {error}]")
        last = match.end()
    out.append(substitute_args(body[last:], args))
    return "".join(out)


def skill_tool(
    skills: list[Skill],
    run: Callable[[str], str] | None = None,
    fork_run: Callable[[str, str | None, list[str] | None], str] | None = None,
) -> Tool:
    """The skill tool. `execute(name, args)` substitutes $ARGUMENTS/$1..$9, then
    runs the body's !`cmd` (if `run` is wired). A `context: fork` skill runs as a
    subagent via `fork_run` (returning its answer); other skills inject the text."""
    by_name = {s.name: s for s in skills}

    def execute(name: str, args: str = "") -> str:
        if name not in by_name:
            available = ", ".join(sorted(by_name)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        skill = by_name[name]
        processed = expand_body(skill.body, run, args)  # commands come from the template only
        if skill.fork:
            if fork_run is None:
                return "Error: this skill runs as a subagent, which is unavailable here."
            return fork_run(processed, skill.model, skill.allowed_tools)
        return processed

    return Tool(
        name="skill",
        description=(
            "Load and run one of the available skills (listed in the system "
            "prompt) by name, optionally passing `args`. Do this before a task "
            "the skill governs. Some skills run shell commands to gather live "
            "context; some run as a subagent and return its result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill's name."},
                "args": {
                    "type": "string",
                    "description": "Optional arguments, substituted as $ARGUMENTS / $1..$9.",
                },
            },
            "required": ["name"],
        },
        execute=execute,
        read_only=True,          # the call injects text or delegates; sub actions are policy-gated
        spawns_subagents=True,   # a fork skill delegates — keep it out of subagents (no nested fork)
    )


# Deprecated compat alias: the ui lane still imports view_skill_tool. Calling it
# with no `run` yields the non-executing, read-only lesson-15 tool. The ui lane
# should migrate to skill_tool; tracked as a cross-lane coordination item.
view_skill_tool = skill_tool
