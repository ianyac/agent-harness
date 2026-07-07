import json
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from harness.tools.base import Tool

EVENTS = ("pre_tool_use", "post_tool_use", "session_start", "stop")
DEFAULT_TIMEOUT = 10.0
_ENTRY_KEYS = {"command", "matcher"}


class HookError(Exception):
    """A fail-closed lifecycle hook failed; startup must not proceed."""


@dataclass
class Hook:
    command: str
    matcher: str | None = None  # regex on the tool name; falsy = match all


@dataclass
class HookSet:
    pre_tool_use: list[Hook] = field(default_factory=list)
    post_tool_use: list[Hook] = field(default_factory=list)
    session_start: list[Hook] = field(default_factory=list)
    stop: list[Hook] = field(default_factory=list)


def load_hooks(path: Path) -> HookSet:
    """hooks.json -> HookSet. A missing file means no hooks; anything
    malformed — unreadable file, bad JSON, unknown events or entry keys,
    non-string fields, an invalid matcher regex — is a hard error at load
    time. A policy file must fail at startup, never at tool-call time
    (a bad post_tool_use matcher discovered at run time would fire AFTER
    a side-effecting tool already executed)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return HookSet()
    except OSError as error:
        raise ValueError(f"cannot read {path.name}: {error}") from None
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
            if not isinstance(entry, dict):
                raise ValueError(f"hooks.json: every {event} entry must be an object")
            stray = set(entry) - _ENTRY_KEYS
            if stray:
                raise ValueError(
                    f"hooks.json: unknown keys {sorted(stray)} in a {event} entry"
                )
            command = entry.get("command")
            if not isinstance(command, str):
                raise ValueError(
                    f"hooks.json: every {event} entry needs a string 'command'"
                )
            matcher = entry.get("matcher")
            if matcher is not None:
                if not isinstance(matcher, str):
                    raise ValueError(f"hooks.json: {event} matcher must be a string")
                try:
                    re.compile(matcher)
                except re.error as error:
                    raise ValueError(
                        f"hooks.json: invalid {event} matcher {matcher!r}: {error}"
                    ) from None
            getattr(hookset, event).append(Hook(command=command, matcher=matcher))
    return hookset


@dataclass
class _Outcome:
    ok: bool
    stdout: str
    reason: str


def _run(hook: Hook, payload: dict, timeout: float, cwd: Path | None) -> _Outcome:
    # hooks run UNSANDBOXED (learner's ruling): their utility lives outside
    # the workspace — notifications, external logs, network. The startup
    # approval gate is the mitigation: see plan decision 4.
    payload = {**payload, "workspace": str(cwd or Path.cwd())}
    # temp files instead of pipes for output: a backgrounded grandchild
    # inherits the descriptors, and a pipe it holds open reads as a hang
    # even after the hook itself exited 0
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                ["sh", "-c", hook.command],
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                cwd=cwd,
                start_new_session=True,  # own process group: timeout kills the tree
            )
        except OSError as error:
            return _Outcome(False, "", f"hook failed to start: {error}")
        try:
            proc.communicate(json.dumps(payload).encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return _Outcome(False, "", f"hook timed out after {timeout:g}s")
        out.seek(0)
        stdout = out.read().decode(errors="replace")
        err.seek(0)
        stderr = err.read().decode(errors="replace")
    if proc.returncode == 0:
        return _Outcome(True, stdout, "")
    return _Outcome(False, stdout, stderr.strip() or f"hook exited {proc.returncode}")


def _matching(hooks: list[Hook], tool_name: str) -> list[Hook]:
    # falsy matcher (absent or "") matches every tool — mirroring the
    # convention hook authors come with; "" as match-nothing would be a
    # policy that silently never enforces
    return [
        h for h in hooks if not h.matcher or re.fullmatch(h.matcher, tool_name)
    ]


def with_hooks(
    tools: dict[str, Tool],
    hookset: HookSet,
    on_warning: Callable[[str], None] = print,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
) -> dict[str, Tool]:
    """Wrap every tool's execute with the pre/post hooks — IN PLACE, and
    returns the same dict: closures over the registry (the agent tool)
    must observe the hooked tools, or delegation would evade every hook.
    Runs after the permission gate — a hook can narrow what the human
    allowed, never widen it. Observer failures are loud by default."""
    if not hookset.pre_tool_use and not hookset.post_tool_use:
        return tools
    for name in list(tools):
        tools[name] = _wrap(tools[name], hookset, on_warning, timeout, cwd)
    return tools


def _wrap(
    tool: Tool,
    hookset: HookSet,
    on_warning: Callable[[str], None],
    timeout: float,
    cwd: Path | None,
) -> Tool:
    def execute(**args) -> str:
        for hook in _matching(hookset.pre_tool_use, tool.name):
            payload = {"event": "pre_tool_use", "tool": tool.name, "args": args}
            outcome = _run(hook, payload, timeout, cwd)
            if not outcome.ok:
                # fail closed: a policy hook that cannot run must block —
                # silently skipping enforcement is the one wrong answer
                return f"Blocked by hook: {outcome.reason}"
        try:
            result = tool.execute(**args)
        except Exception as error:  # noqa: BLE001 — mirror the loop's conversion
            # observers must see failing calls too; same string the loop
            # would have produced for the model
            result = f"Error: {type(error).__name__}: {error}"
        for hook in _matching(hookset.post_tool_use, tool.name):
            payload = {
                "event": "post_tool_use",
                "tool": tool.name,
                "args": args,
                "result": result,
            }
            outcome = _run(hook, payload, timeout, cwd)
            if not outcome.ok:
                # observers have nothing to halt: fail loud, never alter
                on_warning(f"post_tool_use hook failed: {outcome.reason}")
        return result

    return replace(tool, execute=execute)


def run_session_start(
    hookset: HookSet, timeout: float = DEFAULT_TIMEOUT, cwd: Path | None = None
) -> list[str]:
    """Run session_start hooks; each hook's stdout becomes an injected
    context section. Fails closed: a broken startup hook aborts startup —
    running without context the user demanded is not an option."""
    sections = []
    for hook in hookset.session_start:
        outcome = _run(hook, {"event": "session_start"}, timeout, cwd)
        if not outcome.ok:
            raise HookError(f"session_start hook failed: {outcome.reason}")
        if outcome.stdout.strip():
            sections.append(outcome.stdout.strip())
    return sections


def run_stop(
    hookset: HookSet,
    reply: dict,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
) -> list[str]:
    """Run stop hooks after a completed turn. Pure observers: failures
    come back as warning strings for the caller to surface."""
    warnings = []
    for hook in hookset.stop:
        outcome = _run(hook, {"event": "stop", "reply": reply}, timeout, cwd)
        if not outcome.ok:
            warnings.append(f"stop hook failed: {outcome.reason}")
    return warnings
