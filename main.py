import argparse
import atexit
import datetime
import json
import os
import platform
import sys
from pathlib import Path

from harness.hooks import (
    EVENTS,
    HookError,
    HookSet,
    load_hooks,
    run_session_start,
    run_stop,
    with_hooks,
)
from harness.llm import CodexAdapter
from harness.loop import run_turn
from harness.mcp import MCPError, MCPServer, load_config, mcp_tools
from harness.permissions import MODES, PermissionPolicy
from harness.prompts import Environment, build_system_prompt
from harness.sandbox import SandboxPolicy, default_sandbox
from harness.session import SessionLog, lock, unlock
from harness.skills import (
    Skill,
    cmd_blocks,
    discover,
    has_cmd_blocks,
    skill_tool,
    skills_section,
)
from harness.tools.agent import agent_tool
from harness.tools.bash import bash_tool, run_sandboxed
from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool

KEEP_RECENT = 8  # messages kept verbatim through a compaction
# fraction of the model's context window that triggers compaction; the rest
# is headroom for output tokens, mid-turn growth, and estimate bias
COMPACT_FRACTION = 0.8


def ask_user(name: str, args: dict) -> str:
    # only the parent agent ever asks: subagents run in the background and
    # get denials instead of prompts (plan decision 3, revised)
    print(f"  agent wants to run: {name}({json.dumps(args)})")
    while True:
        try:
            answer = (
                input("  allow? [y]es / [n]o / [a]lways for this tool: ")
                .strip()
                .lower()
            )
        except EOFError:
            return "no"  # Ctrl-D at a prompt is a refusal, not a crash
        match answer:
            case "y" | "yes":
                return "yes"
            case "n" | "no":
                return "no"
            case "a" | "always":
                return "always"
            case _:
                print("  please answer y, n, or a")


def approve_commands(
    source: str, noun: str, commands: list[str], *, sandboxed: bool = False
) -> bool:
    """Workspace config (hooks.json, mcp.json) is clone-shippable and
    model-writable, so its commands run only after the human reads them
    on a real terminal. Runs BEFORE any listed command executes."""
    if not commands:
        return True
    if not sys.stdin.isatty():
        # no human at the terminal means no one read the listing: piped
        # input must never be able to approve unsandboxed commands
        print(f"({source} present but stdin is not interactive — {noun} disabled)")
        return False
    kind = "sandboxed" if sandboxed else "unsandboxed"
    print(f"{source} wants to run these commands ({kind}):")
    for line in commands:
        print(f"  {line}")
    while True:
        try:
            answer = input(f"enable these {noun}? [y]es / [n]o: ").strip().lower()
        except EOFError:
            return False  # no interactive consent = nothing runs
        match answer:
            case "y" | "yes":
                return True
            case "n" | "no":
                return False
            case _:
                print("  please answer y or n")


def approve_hooks(hookset: HookSet) -> bool:
    commands = [
        f"{event}: {hook.command}"
        for event in EVENTS
        for hook in getattr(hookset, event)
    ]
    return approve_commands("hooks.json", "hooks", commands)


def approve_mcp(servers: dict[str, str]) -> bool:
    commands = [f"{name}: {command}" for name, command in servers.items()]
    return approve_commands("mcp.json", "servers", commands)


def approve_skill_execution(skills: list[Skill]) -> bool:
    commands = [
        f"{skill.name}: {command}"
        for skill in skills
        for command in cmd_blocks(skill.body)
    ]
    return approve_commands("skills/", "skill commands", commands, sandboxed=True)


class StreamLine:
    """Terminal state for streamed replies. Two jobs: nothing else may
    print onto a half-painted line, and chunk accumulation tracks only the
    CURRENT model call — narration streamed before a tool call is not part
    of the final reply, so tool boundaries reset the buffer."""

    def __init__(self):
        self.chunks: list[str] = []
        self.open = False

    def write(self, delta: str) -> None:
        if not delta:
            return
        if not self.open:
            print("agent: ", end="", flush=True)
            self.open = True
        self.chunks.append(delta)
        print(delta, end="", flush=True)

    def break_line(self) -> None:
        # a tool call or prompt is about to print: close the painted line
        # and start fresh accumulation for the next model call
        if self.open:
            print()
            self.open = False
        self.chunks.clear()

    def close(self) -> str:
        """End of turn: close the line and return the final call's text."""
        text = "".join(self.chunks)
        if self.open:
            print()
        self.discard()
        return text

    def discard(self) -> None:
        # cancelled turn: the rollback message brings its own newline
        self.chunks.clear()
        self.open = False


def environment(workspace: Path) -> Environment:
    # the one place real-world facts are read; rebuilt each turn so a
    # session that crosses midnight keeps the right date
    return Environment(
        cwd=str(Path.cwd().resolve()),
        workspace=str(workspace),
        os=platform.platform(),
        date=datetime.date.today().isoformat(),
    )


def current_system_prompt(
    workspace: Path, extra_sections: list[str] | None = None
) -> str:
    return build_system_prompt(environment(workspace), extra_sections=extra_sections)


def current_subagent_prompt(
    workspace: Path, extra_sections: list[str] | None = None
) -> str:
    # same core prompt, plus the role section — the extra_sections seam
    role = (
        "You are a subagent: another agent delegated one self-contained "
        "task to you. Work it to completion and make your final reply "
        "the complete answer — it is the only thing the delegating "
        "agent will see."
    )
    return build_system_prompt(
        environment(workspace), extra_sections=[role] + (extra_sections or [])
    )


def main():
    parser = argparse.ArgumentParser(description="agent-harness REPL")
    parser.add_argument("--mode", choices=MODES, default="default")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="root the agent may read/write/run within (default: cwd)",
    )
    parser.add_argument(
        "--compact-threshold",
        type=int,
        default=None,
        help="token estimate above which old turns are summarized "
        "(default: 80%% of the model's context window)",
    )
    parser.add_argument(
        "--resume",
        metavar="ID",
        default=None,
        help="resume the session with this id (filename stem under .agent/sessions)",
    )
    parser.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="resume the most recent session in this workspace",
    )
    cli_args = parser.parse_args()
    if cli_args.resume is not None and cli_args.continue_:
        parser.error("--resume and --continue are mutually exclusive")

    workspace = cli_args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    if cli_args.compact_threshold is not None and cli_args.compact_threshold <= 0:
        parser.error("--compact-threshold must be a positive token count")
    sandbox = default_sandbox(SandboxPolicy(workspace))

    sessions_dir = workspace / ".agent" / "sessions"
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        parser.error(f"cannot prepare {sessions_dir}: {error}")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if cli_args.continue_:
        candidates = list(sessions_dir.glob("*.jsonl"))
        if not candidates:
            parser.error(f"no sessions to continue in {sessions_dir}")
        # most recently used, not most recently created: a resumed old
        # session must win over a newer-stamped abandoned one
        session_path = max(candidates, key=lambda p: p.stat().st_mtime)
    elif cli_args.resume is not None:
        name = cli_args.resume.removesuffix(".jsonl")
        if not name or Path(name).name != name:
            parser.error(f"invalid session id: {cli_args.resume!r}")
        session_path = sessions_dir / f"{name}.jsonl"
        if not session_path.exists():
            parser.error(f"no such session: {session_path}")
    else:
        session_path = sessions_dir / f"{stamp}-{os.getpid()}.jsonl"
    try:
        lock(session_path)
    except RuntimeError as error:
        parser.error(str(error))

    # the action journal is keyed to the session and appended across
    # resumes, so compaction breadcrumbs written in an earlier process
    # still point at a log containing those actions
    action_log = workspace / ".agent" / f"actions-{session_path.stem}.jsonl"
    try:
        action_log.touch()
    except OSError as error:
        parser.error(f"cannot create action log {action_log}: {error}")

    def record_action(actor: str, name: str, args: dict) -> None:
        try:
            # self-heal: the agent's own tools can delete .agent mid-session
            action_log.parent.mkdir(exist_ok=True)
            with action_log.open("a") as log:
                entry = {"actor": actor, "name": name, "args": args}
                log.write(json.dumps(entry) + "\n")
        except OSError as error:
            # the journal is observability; it must never kill the session
            print(f"(action log unavailable: {error})")

    def breadcrumb_note() -> str:
        # called at compaction fire time, so the count is current even
        # when this turn's own tool calls preceded the compaction
        try:
            entries = action_log.read_text().count("\n")
        except OSError:
            entries = 0
        # absolute path: read_file resolves against the workspace but bash
        # resolves against the process cwd — only absolute means both agree
        return f"Action log: {action_log} ({entries} entries)"

    stream = StreamLine()

    def asker(name: str, args: dict) -> str:
        stream.break_line()  # never prompt onto a half-painted line
        return ask_user(name, args)

    def observe_tool_call(name: str, args: dict) -> None:
        stream.break_line()
        print(f"⚙ {name}({json.dumps(args)})")
        record_action("agent", name, args)

    def observe_sub_tool_call(name: str, args: dict) -> None:
        stream.break_line()
        print(f"  ⚙↳ {name}({json.dumps(args)})")
        record_action("subagent", name, args)

    try:
        hookset = load_hooks(workspace / "hooks.json")
    except (ValueError, json.JSONDecodeError) as error:
        parser.error(f"hooks.json: {error}")
    if not approve_hooks(hookset):
        print("(hooks disabled for this session)")
        hookset = HookSet()
    try:
        hook_sections = run_session_start(hookset, cwd=workspace)
    except HookError as error:
        parser.error(str(error))

    # extra prompt sections, in order: hook-injected context, then the
    # skills menu (metadata only — bodies load on demand via the skill tool)
    skills = discover(workspace / "skills")
    executable = [s for s in skills if has_cmd_blocks(s.body)]
    if executable and not approve_skill_execution(executable):
        # a skill is a capability, not policy: decline drops the executable
        # ones (fail-open, the MCP line) and the session continues on prose
        print(f"(skill execution declined — dropping {len(executable)} executable skill(s))")
        skills = [s for s in skills if not has_cmd_blocks(s.body)]
    section = skills_section(skills)
    context_sections = hook_sections + ([section] if section else [])

    try:
        server_commands = load_config(workspace / "mcp.json")
    except ValueError as error:
        parser.error(f"mcp.json: {error}")
    if not approve_mcp(server_commands):
        print("(MCP servers disabled for this session)")
        server_commands = {}
    foreign_tools = []
    for name, command in server_commands.items():
        # commands resolve in the workspace — the config the human read —
        # not wherever the harness happened to launch (the hooks rule)
        server = MCPServer(name, command, cwd=str(workspace))
        # registered at spawn: parser.error and a crash escaping run_turn
        # must not orphan an approved unsandboxed process (close is
        # idempotent, so the normal path costs nothing)
        atexit.register(server.close)
        try:
            server.start()
            discovered = mcp_tools(server)
        except MCPError as error:
            # a server is a capability, not policy: one that won't serve
            # costs its own tools, loudly, and the session continues.
            # (hooks fail closed because skipping them changes what is
            # ALLOWED; skipping a server only shrinks what is POSSIBLE)
            server.close()
            print(f"(mcp: {name} unavailable — {error})")
            continue
        foreign_tools.extend(discovered)
        print(f"(mcp: {name} serves {len(discovered)} tools)")

    llm = CodexAdapter()
    compact_threshold = (
        cli_args.compact_threshold
        if cli_args.compact_threshold is not None
        else int(COMPACT_FRACTION * llm.context_window)
    )
    registry = [
        read_file_tool(workspace=workspace),
        write_file_tool(workspace=workspace),
        list_dir_tool(workspace=workspace),
        bash_tool(sandbox=sandbox),
    ]
    if skills:
        # only offer the skill tool when there is a menu to view — otherwise the
        # model can waste a turn calling a tool that can only ever error
        def run(command: str) -> str:
            return run_sandboxed(command, sandbox)

        registry.append(skill_tool(skills, run))
    tools = {tool.name: tool for tool in registry}
    # foreign tools join before the agent tool: subagents inherit them, and
    # the in-place hook wrapping below covers them like any native tool
    for tool in foreign_tools:
        if tool.name in tools:
            # keep-first-warn (the skills rule): a duplicate name must not
            # silently reroute calls approved under the first identity
            print(f"(mcp: duplicate tool name {tool.name!r} — keeping the first)")
            continue
        tools[tool.name] = tool
    policy = PermissionPolicy(cli_args.mode)
    if skills:
        # the session-start gate is the human's pre-consent to skill execution,
        # so loading a skill must not also prompt per call — an approved hook or
        # MCP server does not re-prompt either. This honors the "session-approved,
        # no per-command prompt" decision. (session_allowlist wins over --mode,
        # matching how explicit human pre-approval already behaves.)
        policy.session_allowlist.add("skill")
    # built once: the callable system prompt is re-evaluated per delegation,
    # so the sub's env facts (date, cwd) never go stale anyway
    tools["agent"] = agent_tool(
        llm,
        tools,
        policy=policy,
        system=lambda: current_subagent_prompt(workspace, context_sections),
        on_tool_call=observe_sub_tool_call,
        compact_threshold=compact_threshold,
    )
    # wrapped IN PLACE after the agent tool joins: every tool including the
    # delegation is hooked, the sub's closure sees the hooked registry, and
    # the spawns_subagents field keeps the recursion guard intact through
    # the wrapping
    with_hooks(tools, hookset, on_warning=lambda w: print(f"({w})"), cwd=workspace)
    session = SessionLog(session_path)
    try:
        messages = session.load()
    except OSError as error:
        parser.error(f"cannot read session {session_path}: {error}")
    print(f"(session: {session_path.name})")
    if messages:
        print(f"(resumed {len(messages)} messages)")

    def record_turn() -> None:
        try:
            session.record_turn(messages)
        except OSError as error:
            # persistence is best-effort; it must never kill the session
            print(f"(session log unavailable: {error})")

    def on_compact(summarized: int) -> None:
        print(f"[compacted {summarized} messages into a summary]")
        try:
            # messages[0] is the freshly spliced-in summary; the cut count
            # equals what the loop reported
            session.record_compaction(cut=summarized, summary=messages[0])
        except OSError as error:
            print(f"(session log unavailable: {error})")

    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        try:
            reply = run_turn(
                messages,
                user_input,
                llm,
                tools=tools,
                on_tool_call=observe_tool_call,
                policy=policy,
                asker=asker,
                system=current_system_prompt(workspace, context_sections),
                compact_threshold=compact_threshold,
                keep_recent=KEEP_RECENT,
                on_compact=on_compact,
                breadcrumbs=breadcrumb_note,
                on_text_delta=stream.write,
            )
            streamed_text = stream.close()
            if streamed_text != reply["content"]:
                if streamed_text:
                    # a retried stream repainted stale text; correct the
                    # record out loud — the assembled message is the truth
                    print("(stream was superseded; full reply:)")
                print("agent:", reply["content"])
            record_turn()
            for warning in run_stop(hookset, reply, cwd=workspace):
                print(f"({warning})")
        except KeyboardInterrupt:
            stream.discard()
            # drop the half-built exchange: a dangling tool_call in history
            # would poison every later request. Compaction may have shifted
            # indices mid-turn, so roll back to the last completed exchange
            # rather than to a saved position.
            dropped = 0
            while messages and not (
                messages[-1]["role"] == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                messages.pop()
                dropped += 1
            if dropped:
                print(f"\n(turn cancelled — {dropped} unfinished messages dropped)")
            else:
                print("\n(turn already complete — nothing to roll back)")
            # an interrupt can land between the reply print and record_turn;
            # persist whatever completed exchanges the log is still missing
            record_turn()
    unlock(session_path)


if __name__ == "__main__":
    main()
