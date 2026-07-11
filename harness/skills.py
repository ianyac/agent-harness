import re
import shlex
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


# the frontmatter keys discover() understands; anything else is a likely typo.
# A misspelled 'allowed-tools' would read as absent and silently WIDEN a fork
# skill to the full registry, so an unknown key warns rather than passing quietly.
_KNOWN_KEYS = {"name", "description", "context", "model", "allowed-tools"}


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
    try:
        entries = sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []
    except OSError as error:
        # an unreadable skills dir must not sink the session (the "never fatal"
        # invariant): degrade to zero skills with a warning, as glob() used to
        on_warning(f"skipping skills directory {skills_dir}: {error}")
        entries = []
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
        # warn on frontmatter that would otherwise fail silently
        for key in meta.keys() - _KNOWN_KEYS:
            on_warning(f"skill {entry.name}: ignoring unknown frontmatter key {key!r}")
        context = meta.get("context")
        if context is not None and context != "fork":
            on_warning(
                f"skill {entry.name}: context {context!r} is not 'fork' — "
                "loading as a plain (non-fork) skill"
            )
        allowed_tools = None
        if "allowed-tools" in meta:
            allowed_tools = meta["allowed-tools"].split()
            if not allowed_tools:  # a blank value → zero tools, almost never intended
                on_warning(
                    f"skill {entry.name}: empty allowed-tools — the subagent "
                    "would have no tools"
                )
        name = meta["name"]
        # shlex.quote so a ${SKILL_DIR} spliced into a command survives a path
        # with spaces (a no-op for ordinary paths)
        body = body.replace("${SKILL_DIR}", shlex.quote(str(base)))
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
                fork=context == "fork",
                model=model,
                allowed_tools=allowed_tools,
            )
        )
    return sorted(skills, key=lambda s: s.name)  # menu order = displayed names


def skills_section(skills: list[Skill], tool_name: str = "skill") -> str | None:
    """The always-present metadata block: name + description only. Full
    bodies are pulled in on demand by the skill tool, so an unused skill costs
    one line, not its whole content. `tool_name` must name the tool the caller
    actually registered — the main loop registers "skill", the ui lane
    "view_skill" — so the menu never tells the model to call a missing tool."""
    if not skills:
        return None
    lines = [f"Available skills (call the {tool_name} tool to load one in full):"]
    lines += [f"- {s.name}: {s.description}" for s in skills]
    return "\n".join(lines)


def parse_slash(text: str) -> tuple[str, str] | None:
    """Parse a slash command '/name args' into (name, args): the name is the
    first token after the '/', args the stripped remainder. A bare '/' (or any
    text not starting with '/') returns None — not a command."""
    if not text.startswith("/"):
        return None
    rest = text[1:].strip()
    if not rest:
        return None
    parts = rest.split(maxsplit=1)  # split on any whitespace run (a tab too, not just a space)
    return parts[0], (parts[1] if len(parts) > 1 else "")


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


def shell_substitute_args(command: str, args: str) -> str:
    """substitute_args for a command that will run via `sh -c`: each filled
    value is shlex.quote'd, so shell metacharacters in model-chosen args
    (`;`, `|`, backticks, `$( )`) are passed as literal data — never
    interpreted. $ARGUMENTS expands to its whitespace tokens, each quoted, so
    multi-token flag args still work (`--oneline -n 5`); a missing positional
    is "" (nothing), matching substitute_args.

    Skill-authoring convention: write BARE placeholders (`git log $1`,
    `grep $1 file`) — this function supplies the quoting. A template that also
    quotes the placeholder (`grep "$1" file`) would double-quote and break the
    moment an arg needs escaping, so do not quote placeholders yourself."""
    parts = args.split()

    def repl(match: "re.Match[str]") -> str:
        token = match.group(1)
        if token == "ARGUMENTS":
            return " ".join(shlex.quote(p) for p in parts)
        i = int(token)
        return shlex.quote(parts[i - 1]) if i <= len(parts) else ""

    return _ARG.sub(repl, command)


def expand_body(body: str, run: Callable[[str], str] | None, args: str = "") -> str:
    """Expand a skill body at invocation. `!`cmd`` spans are located on THIS body
    — the template the human approved at session start — then each command has
    $args filled and is run via `run`. Prose between commands gets $args but is
    NOT re-scanned for commands, so an arg containing !`...` lands in prose,
    inert: args can FILL an approved command but never INTRODUCE a new one. A
    command that RUNS gets shell-quoted args (metacharacters stay literal);
    `\\!`cmd`` is a literal and run=None leaves commands unrun — both show args
    raw, since nothing reaches a shell."""
    out: list[str] = []
    last = 0
    for match in _CMD.finditer(body):
        out.append(substitute_args(body[last : match.start()], args))  # prose: args, never a command
        escaped, command = match.group(1), match.group(2)
        if escaped or run is None:
            # literal (escaped) or non-executing: nothing reaches a shell, so
            # show the args raw for display fidelity
            out.append(f"!`{substitute_args(command, args)}`")
        else:
            try:
                out.append(run(shell_substitute_args(command, args)))  # runs → quote args
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


def view_skill_tool(skills: list[Skill]) -> Tool:
    """The lesson-15 read-only skill viewer, kept for the ui lane, which keys on
    this tool's name ("view_skill") and its behavior. It returns a skill's body
    verbatim — no !`cmd` execution, no fork, no args — and is NOT a subagent
    delegator, so subagents may use it. The executing "skill" tool (skill_tool)
    is the main-loop path. (A bare `view_skill_tool = skill_tool` alias silently
    changed the name to "skill" and set spawns_subagents=True, breaking both.)"""
    by_name = {s.name: s for s in skills}

    def execute(name: str) -> str:
        if name not in by_name:
            available = ", ".join(sorted(by_name)) or "none"
            return f"Error: no skill named {name!r}. Available skills: {available}"
        return by_name[name].body  # verbatim; commands are not run

    return Tool(
        name="view_skill",
        description=(
            "Load one of the available skills (listed in the system prompt) by "
            "name and read its full instructions. Read-only: it shows the "
            "skill, it does not run it."
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
