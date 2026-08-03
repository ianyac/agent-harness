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
        # warn on frontmatter that would otherwise fail silently
        for key in meta.keys() - _KNOWN_KEYS:
            on_warning(f"skill {entry.name}: ignoring unknown frontmatter key {key!r}")
        context = meta.get("context")
        if context is not None and context != "fork":
            on_warning(
                f"skill {entry.name}: context {context!r} is not 'fork' — "
                "loading as a plain (non-fork) skill"
            )
        is_fork = context == "fork"
        model = meta.get("model")
        if model is not None and not is_fork:
            # model only picks a fork subagent's client; on a plain skill it does
            # nothing — ignore it rather than drop the whole skill over the field
            on_warning(f"skill {entry.name}: model {model!r} ignored (only fork skills use a model)")
            model = None
        elif model is not None and model not in CONTEXT_WINDOWS:
            on_warning(f"skipping skill {entry.name}: unknown model {model!r}")
            continue
        allowed_tools = None
        if "allowed-tools" in meta:
            allowed_tools = meta["allowed-tools"].split()
            if not allowed_tools:  # a blank value → zero tools, almost never intended
                on_warning(
                    f"skill {entry.name}: empty allowed-tools — the subagent "
                    "would have no tools"
                )
        name = meta["name"]
        if name == "plan":
            # the /plan built-in resolves before skill names, so a skill named
            # "plan" is unreachable through the slash front door
            on_warning(f"skill {entry.name}: name 'plan' is shadowed by the /plan built-in")
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
                body=body,  # ${SKILL_DIR} stays a token; expand_body resolves it per-context
                dir=base,
                fork=is_fork,
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


# One-pass fill of ${SKILL_DIR}, $ARGUMENTS, and $1..$9. The single pass IS the
# security boundary: a value substituted in (an arg, or the skill-dir path) is
# never re-scanned, so args fill approved holes but can never INTRODUCE a
# ${SKILL_DIR} or a $-token, and a skill-dir path that happens to contain a
# $-token is inert. Splitting ${SKILL_DIR} and args into two passes (either
# order) re-opens one of those two holes.
_SUBST = re.compile(r"\$\{SKILL_DIR\}|\$(ARGUMENTS|[1-9])")


def _substitute(
    text: str, args: str, skill_dir: "Path | None", quote: Callable[[str], str] | None
) -> str:
    """Fill the placeholders in one pass. `quote` is shlex.quote for a command
    bound for `sh -c` (each filled value quoted, so metacharacters stay literal
    and a spaced ${SKILL_DIR} is one token) or None for display/prose. A missing
    $1..$9 is ""; a ${SKILL_DIR} with no skill_dir is left as the literal token."""
    parts = args.split()

    def repl(match: "re.Match[str]") -> str:
        token = match.group(1)
        if token is None:  # matched ${SKILL_DIR}
            if skill_dir is None:
                return match.group(0)  # nothing to resolve — leave the token
            return quote(str(skill_dir)) if quote else str(skill_dir)
        if token == "ARGUMENTS":
            return " ".join(quote(p) for p in parts) if quote else args
        i = int(token)
        if i > len(parts):
            return ""  # missing positional → nothing, in both modes
        return quote(parts[i - 1]) if quote else parts[i - 1]

    return _SUBST.sub(repl, text)


def substitute_args(body: str, args: str) -> str:
    """Fill $ARGUMENTS (whole string) and $1..$9 (whitespace positionals;
    missing → "") for display — no shell quoting; ${SKILL_DIR} left literal."""
    return _substitute(body, args, None, None)


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
    return _substitute(command, args, None, shlex.quote)


def expand_body(
    body: str,
    run: Callable[[str], str] | None,
    args: str = "",
    skill_dir: "Path | None" = None,
) -> str:
    """Expand a skill body at invocation. `!`cmd`` spans are located on THIS body
    — the template the human approved at session start — then each command's
    placeholders (${SKILL_DIR}, $args) are filled in ONE pass and it is run via
    `run`. Prose between commands gets the same fill but is NOT re-scanned for
    commands, so an arg containing !`...` lands in prose, inert: args FILL an
    approved template but never INTRODUCE a command or a ${SKILL_DIR}. A running
    command gets shell-quoted args and a shell-quoted ${SKILL_DIR}; `\\!`cmd`` is
    a literal and run=None leaves commands unrun — both, and all prose, are raw,
    since nothing reaches a shell."""
    out: list[str] = []
    last = 0
    for match in _CMD.finditer(body):
        # prose (and the template's ${SKILL_DIR}) raw — never a command
        out.append(_substitute(body[last : match.start()], args, skill_dir, None))
        escaped, command = match.group(1), match.group(2)
        if escaped or run is None:
            # literal (escaped) or non-executing: nothing reaches a shell → raw
            out.append(f"!`{_substitute(command, args, skill_dir, None)}`")
        else:
            filled = _substitute(command, args, skill_dir, shlex.quote)  # runs → quoted
            try:
                out.append(run(filled))
            except Exception as error:  # a bad block degrades, never raises
                out.append(f"[skill command failed: {error}]")
        last = match.end()
    out.append(_substitute(body[last:], args, skill_dir, None))
    return "".join(out)


def _lookup(by_name: dict, name: str) -> tuple[Skill | None, str]:
    """Resolve a skill by name, or (None, error) with an identical not-found
    message — shared by the skill and view_skill tools so the two seams never
    drift. Callers branch on `skill is None` (which narrows the type)."""
    skill = by_name.get(name)
    if skill is None:
        available = ", ".join(sorted(by_name)) or "none"
        return None, f"Error: no skill named {name!r}. Available skills: {available}"
    return skill, ""


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
        skill, error = _lookup(by_name, name)
        if skill is None:
            return error
        processed = expand_body(skill.body, run, args, skill_dir=skill.dir)  # commands from the template only
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
        read_only=True,  # the call injects text or delegates; sub actions are policy-gated
        # the guard tracks the CAPABILITY, not the tool's identity: only a
        # fork-capable build can delegate. Built without fork_run, a fork skill
        # is refused above, so this build cannot recurse and is safe inside a
        # subagent — which is how subagents get skills at all.
        spawns_subagents=fork_run is not None,
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
        skill, error = _lookup(by_name, name)
        if skill is None:
            return error
        # verbatim (commands not run, placeholders left as-is) except ${SKILL_DIR},
        # resolved raw so a bundled-file path the model reads out is a real path
        return skill.body.replace("${SKILL_DIR}", str(skill.dir))

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
