import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from harness.tools.base import Tool

EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "stop")
DEFAULT_TIMEOUT = 10.0


class HookError(Exception):
    """A fail-closed lifecycle hook failed; startup must not proceed."""


@dataclass
class Hook:
    command: str
    matcher: str | None = None  # regex, fullmatched against the tool name


@dataclass
class HookSet:
    pre_tool_use: list[Hook] = field(default_factory=list)
    post_tool_use: list[Hook] = field(default_factory=list)
    session_start: list[Hook] = field(default_factory=list)
    stop: list[Hook] = field(default_factory=list)


def load_hooks(path: Path) -> HookSet:
    """hooks.json -> HookSet. A missing file means no hooks; anything
    malformed is a hard error — a policy file that cannot be read must
    never be silently ignored."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return HookSet()
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("hooks.json must be a JSON object")
    unknown = set(config) - set(EVENTS)
    if unknown:
        raise ValueError(
            f"hooks.json: unknown events {sorted(unknown)}; expected one of {EVENTS}"
        )
    hookset = HookSet()
    for event, entries in config.items():
        if not isinstance(entries, list):
            raise ValueError(f"hooks.json: {event} must be a list of hook entries")
        for entry in entries:
            if not isinstance(entry, dict) or "command" not in entry:
                raise ValueError(f"hooks.json: every {event} entry needs a 'command'")
            getattr(hookset, event).append(
                Hook(command=entry["command"], matcher=entry.get("matcher"))
            )
    return hookset


@dataclass
class _Outcome:
    ok: bool
    stdout: str
    reason: str


def _run(hook: Hook, payload: dict, timeout: float) -> _Outcome:
    # hooks run UNSANDBOXED (learner's ruling): their utility lives outside
    # the workspace — notifications, external logs, network. Accepted risk,
    # since hooks.json is model-reachable: see plan decision 4.
    try:
        proc = subprocess.run(
            ["sh", "-c", hook.command],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _Outcome(False, "", f"hook timed out after {timeout:g}s")
    except OSError as error:
        return _Outcome(False, "", f"hook failed to start: {error}")
    if proc.returncode == 0:
        return _Outcome(True, proc.stdout, "")
    return _Outcome(
        False, proc.stdout, proc.stderr.strip() or f"hook exited {proc.returncode}"
    )


def _matching(hooks: list[Hook], tool_name: str) -> list[Hook]:
    return [
        h for h in hooks if h.matcher is None or re.fullmatch(h.matcher, tool_name)
    ]


def with_hooks(
    tools: dict[str, Tool],
    hookset: HookSet,
    on_warning: Callable[[str], None] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Tool]:
    """Wrap every tool's execute with the pre/post hooks. The loop never
    knows: a hooked registry is just a registry. Runs after the permission
    gate — a hook can narrow what the human allowed, never widen it."""
    if not hookset.pre_tool_use and not hookset.post_tool_use:
        return tools
    return {
        name: _wrap(tool, hookset, on_warning, timeout)
        for name, tool in tools.items()
    }


def _wrap(
    tool: Tool,
    hookset: HookSet,
    on_warning: Callable[[str], None] | None,
    timeout: float,
) -> Tool:
    def execute(**args) -> str:
        for hook in _matching(hookset.pre_tool_use, tool.name):
            payload = {"event": "pre_tool_use", "tool": tool.name, "args": args}
            outcome = _run(hook, payload, timeout)
            if not outcome.ok:
                # fail closed: a policy hook that cannot run must block —
                # silently skipping enforcement is the one wrong answer
                return f"Blocked by hook: {outcome.reason}"
        result = tool.execute(**args)
        for hook in _matching(hookset.post_tool_use, tool.name):
            payload = {
                "event": "post_tool_use",
                "tool": tool.name,
                "args": args,
                "result": result,
            }
            outcome = _run(hook, payload, timeout)
            if not outcome.ok and on_warning is not None:
                # observers have nothing to halt: fail loud, never alter
                on_warning(f"post_tool_use hook failed: {outcome.reason}")
        return result

    return replace(tool, execute=execute)


def run_session_start(hookset: HookSet, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Run session_start hooks; each hook's stdout becomes an injected
    context section. Fails closed: a broken startup hook aborts startup —
    running without context the user demanded is not an option."""
    sections = []
    for hook in hookset.session_start:
        outcome = _run(hook, {"event": "session_start"}, timeout)
        if not outcome.ok:
            raise HookError(f"session_start hook failed: {outcome.reason}")
        if outcome.stdout.strip():
            sections.append(outcome.stdout.strip())
    return sections


def run_stop(hookset: HookSet, reply: dict, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Run stop hooks after a completed turn. Pure observers: failures
    come back as warning strings for the caller to surface."""
    warnings = []
    for hook in hookset.stop:
        outcome = _run(hook, {"event": "stop", "reply": reply}, timeout)
        if not outcome.ok:
            warnings.append(f"stop hook failed: {outcome.reason}")
    return warnings
