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
from harness.llm import make_llm
from harness.loop import run_turn
from harness.mcp import MCPError, MCPServer, load_config, mcp_tools
from harness.permissions import NO_MUTATION_MODES, STARTUP_MODES, PermissionPolicy
from harness.prompts import (
    Environment,
    PLAN_MODE,
    PLAN_MODE_SUBAGENT,
    build_system_prompt,
)
from harness.sandbox import LinuxSandbox, NoSandbox, SandboxPolicy, default_sandbox
from harness.session import SessionLog, lock, unlock
from harness.skills import (
    Skill,
    cmd_blocks,
    discover,
    has_cmd_blocks,
    parse_slash,
    skill_tool,
    skills_section,
)
from harness.tools.agent import agent_tool, run_subagent
from harness.tools.bash import bash_tool, run_sandboxed
from harness.tools.list_dir import list_dir_tool
from harness.tools.plan import exit_plan_mode_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool

KEEP_RECENT = 8  # messages kept verbatim through a compaction
# fraction of the model's context window that triggers compaction; the rest
# is headroom for output tokens, mid-turn growth, and estimate bias
COMPACT_FRACTION = 0.8
# registry names that mean "this agent may already run shell". Used to decide
# whether a subagent may also run a skill's !`cmd`; a shell reached under any
# other name is not recognised, so the decision fails closed.
SHELL_TOOLS = ("bash",)


def may_run_skill_commands(offered: dict, mode: str) -> bool:
    """Whether a subagent holding `offered` may run a skill's !`cmd`.

    The rule that keeps allowed-tools meaningful: a skill's commands are shell,
    so a subagent only gets an executing skill build if it was already allowed
    shell. Otherwise excluding `bash` from a fork skill's allowed-tools would
    be no restriction at all — the sub could run anything through a skill body.
    A mode that denies mutation denies these too (NO_MUTATION_MODES, shared with
    decide() so the two cannot drift). Fails closed: a shell reached under an
    unrecognised name yields False."""
    if not any(name in offered for name in SHELL_TOOLS):
        return False
    return mode not in NO_MUTATION_MODES


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
    """Workspace config (hooks.json, mcp.json, skills/) is clone-shippable and
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


def approve_skill_execution(skills: list[Skill], sandboxed: bool) -> bool:
    commands = [
        f"{skill.name}: {command}"
        for skill in skills
        for command in cmd_blocks(skill.body)
    ]
    return approve_commands("skills/", "skill commands", commands, sandboxed=sandboxed)


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
    parser.add_argument("--mode", choices=STARTUP_MODES, default="default")
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
    # a skill's !`cmd` is config-authored shell (the human installs the file),
    # so it is gated once here like hooks.json/mcp.json — not per call. Decline
    # drops the executable skills (a capability, not policy); prose skills stay.
    # LinuxSandbox is a stub that raises on wrap(), so it is NOT a working
    # sandbox — treat it like NoSandbox when deciding whether commands may run
    sandbox_enforces = not isinstance(sandbox, (NoSandbox, LinuxSandbox))
    executable = [s for s in skills if has_cmd_blocks(s.body)]
    if executable:
        if not sandbox_enforces:
            # no working sandbox on this platform: skill commands are refused
            # at run time (they render as inert skip-notices), so there is
            # nothing dangerous to approve. Keep the skills — their prose and
            # args still work — and say the commands won't run.
            print(
                f"(no working sandbox here — {len(executable)} skill(s) with "
                "commands will not run those commands)"
            )
        elif not approve_skill_execution(executable, sandboxed=True):
            print(f"(skill execution declined — dropping {len(executable)} executable skill(s))")
            skills = [s for s in skills if not has_cmd_blocks(s.body)]
    section = skills_section(skills)
    context_sections = hook_sections + ([section] if section else [])
    # NOTE: context_sections is the MAIN LOOP's. A subagent's sections are built
    # separately by subagent_prompt_for, from what that sub actually holds — a
    # new section added here does NOT reach subagents unless added there too.

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

    llm = make_llm()  # the main-loop / agent-tool client (gpt-5.5)
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

    def register_builtin(name, tool):
        # built-ins (agent/skill/exit_plan_mode) join AFTER the MCP tools, so the
        # keep-first-warn guard above doesn't cover them. The built-in wins (it
        # is core harness capability), but warn so a same-named MCP tool isn't
        # shadowed silently — the very thing that guard exists to announce.
        if name in tools:
            print(f"(builtin {name!r} shadows an MCP tool of the same name — using the builtin)")
        tools[name] = tool

    policy = PermissionPolicy(cli_args.mode)

    def subagent_prompt_for(offered: dict):
        """The sub's system prompt, built from what THIS sub will actually hold.
        A factory: the returned closure is re-evaluated on every model call in
        the delegation (run_turn re-reads a callable system prompt each
        iteration), so it reflects late registrations and the mode at that
        moment. The menu is derived, not assumed — see sub_usable_skills. An
        honest menu is the whole reason the old strip-it workaround existed."""

        def build() -> str:
            usable = sub_usable_skills(offered) if "skill" in offered else []
            section = skills_section(usable)
            sections = hook_sections + ([section] if section else [])
            # the read-only plan note when this delegation happens inside a
            # plan-mode turn (the sub shares the plan policy but cannot exit
            # plan mode, so PLAN_MODE_SUBAGENT — not PLAN_MODE)
            extra = sections + ([PLAN_MODE_SUBAGENT] if policy.mode == "plan" else [])
            return current_subagent_prompt(workspace, extra)

        return build

    # Subagent-specific builds of parent tools, applied after the recursion
    # filter. Two sets, chosen per delegation by what the sub actually holds: a
    # sub without shell must not gain it through a skill's !`cmd`, or
    # allowed-tools could no longer bound its capabilities. Both are filled by
    # the skills block below and hook-wrapped with the rest; the closures read
    # them at delegation time.
    sub_variants_exec: dict = {}   # sub has shell → skills may run their commands
    sub_variants_plain: dict = {}  # sub has none → commands render as skip notices

    def sub_may_run(offered: dict) -> bool:
        # may_run_skill_commands is the rule (module level, tested); a platform
        # with no working sandbox refuses every command anyway, so it decides
        # the same way — one predicate behind both the build and the menu
        return sandbox_enforces and may_run_skill_commands(offered, policy.mode)

    def sub_usable_skills(offered: dict) -> list:
        # a fork skill is refused by the sub's build; a command-bearing skill
        # the sub cannot run yields only NOT_RUN, so listing it would invite
        # the sub to "gather context" it cannot gather
        can_run = sub_may_run(offered)
        return [
            s for s in skills
            if not s.fork and (can_run or not has_cmd_blocks(s.body))
        ]

    def sub_substitutions(offered: dict) -> dict:
        # a sub that may not run commands still gets a build — one that renders
        # each command as a visible NOT_RUN rather than silently unrun text —
        # but only if that build has something to serve. Handing over a tool
        # whose description points at a menu that was never emitted is worse
        # than not handing it over at all.
        if not sub_usable_skills(offered):
            return {}
        return sub_variants_exec if sub_may_run(offered) else sub_variants_plain

    register_builtin("agent", agent_tool(
        llm,
        tools,
        policy=policy,
        system=subagent_prompt_for(tools),
        on_tool_call=observe_sub_tool_call,
        compact_threshold=compact_threshold,
        substitutions=sub_substitutions,
    ))
    if skills:
        # a skill's !`cmd` runs as a sandboxed preprocessor (config-authored,
        # session-approved — the lesson-18 model)
        def run(command: str) -> str:
            # require a working sandbox: model-influenced args reach sh -c, so a
            # run without enforcement is refused outright. (expand_body's
            # shell-quoting is the other half of this defense.) LinuxSandbox
            # counts as non-enforcing — its wrap() raises NotImplementedError.
            if not sandbox_enforces:
                return "[skill command not run: no working sandbox on this platform]"
            return run_sandboxed(command, sandbox)

        # a context:fork skill runs as a subagent: its body is the task, `model`
        # picks the client, `allowed-tools` filters the tool set. run_subagent
        # applies the recursion guard and the ask->deny policy.
        def fork_run(task: str, model: str | None, allowed_tools: list[str] | None) -> str:
            sub_tools = (
                tools
                if allowed_tools is None
                else {n: t for n, t in tools.items() if n in allowed_tools}
            )
            return run_subagent(
                task,
                make_llm(model),
                sub_tools,
                policy=policy,
                # the menu is built from THIS sub's registry, so a restricted
                # fork is never told to call a tool it wasn't given
                system=subagent_prompt_for(sub_tools),
                on_tool_call=observe_sub_tool_call,
                compact_threshold=compact_threshold,
                substitutions=sub_substitutions,
            )

        # the subagent's builds: same skills, NO fork_run — a fork skill is
        # refused, so neither build can recurse. The executing one goes only to
        # subs that already hold shell; a sub without it gets the run-less
        # build, so a skill's !`cmd` can't become a shell side-channel around
        # allowed-tools. That build renders each command as skills.NOT_RUN, so
        # a skipped command is never mistaken for one that printed nothing.
        sub_variants_exec["skill"] = skill_tool(skills, run)
        sub_variants_plain["skill"] = skill_tool(skills)
        register_builtin("skill", skill_tool(skills, run, fork_run))

        # what a subagent can actually end up holding: the inheritable tools,
        # plus `skill` — which never survives the comprehension (the parent's
        # build is fork-capable, so spawns_subagents excludes it) and instead
        # arrives by substitution. Listing it in allowed-tools is legitimate.
        sub_capable = {n for n, t in tools.items() if not t.spawns_subagents}
        sub_capable.add("skill")
        for s in skills:
            unknown = [t for t in (s.allowed_tools or []) if t not in sub_capable]
            if unknown:
                print(
                    f"(skill {s.name!r}: allowed-tools not available to a "
                    f"subagent: {', '.join(unknown)})"
                )

    def approve_plan(plan: str) -> tuple[bool, str]:
        print("Proposed plan:\n" + plan)
        if not sys.stdin.isatty():
            return False, ""  # no human to consent
        try:
            answer = input("approve this plan? [y]es / [n]o: ").strip().lower()
        except EOFError:
            return False, ""
        if answer in ("y", "yes"):
            return True, ""
        try:
            feedback = input("feedback for the revision (optional): ").strip()
        except EOFError:
            feedback = ""  # Ctrl-D at the feedback prompt is still a rejection
        return False, feedback

    register_builtin("exit_plan_mode", exit_plan_mode_tool(policy, approve_plan))
    # the slash front door invokes a skill as an explicit USER action, so it
    # calls the UNWRAPPED skill tool: the tool hooks gate the model's
    # autonomous calls, not a command the human typed, and — crucially — the
    # unwrapped tool RAISES on failure (a fork skill's LLM error) so the slash
    # loop's try/except can catch it, whereas with_hooks converts failures to
    # result strings that would otherwise be fed to the model as a prompt. The
    # fork subagent's own tool calls still see the hooked registry.
    raw_skill_tool = tools.get("skill")
    # wrapped IN PLACE after the agent tool joins: every tool including the
    # delegation is hooked, the sub's closure sees the hooked registry, and
    # the spawns_subagents field keeps the recursion guard intact through
    # the wrapping
    with_hooks(tools, hookset, on_warning=lambda w: print(f"({w})"), cwd=workspace)
    # a derived tool is model-driven too, so it gets the same hook wrapping —
    # unlike the raw slash-command tool captured above, which is a human action
    for variants in (sub_variants_exec, sub_variants_plain):
        with_hooks(variants, hookset, on_warning=lambda w: print(f"({w})"), cwd=workspace)
    session = SessionLog(session_path)
    try:
        messages = session.load()
    except OSError as error:
        parser.error(f"cannot read session {session_path}: {error}")
    print(f"(session: {session_path.name})")
    if messages:
        print(f"(resumed {len(messages)} messages)")
    if skills:
        print(f"({len(skills)} skills — /name to run one, / to list)")

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

    plan_armed = False
    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        # only a real turn consumes the arming; a non-turn interaction (listing,
        # arming) must NOT silently disarm plan mode before the next message
        run_in_plan = plan_armed
        if user_input.startswith("/"):
            parsed = parse_slash(user_input)
            names = sorted(s.name for s in skills)
            if parsed is None:  # a bare "/" — list; arming (if any) is preserved
                print(f"(commands: /plan; skills: {', '.join(names) or 'none'})")
                continue
            name, args = parsed
            if name == "plan":  # built-in — resolves before skill names, works with 0 skills
                plan_armed = False  # an explicit /plan supersedes any prior arming
                if args:
                    run_in_plan = True         # run this turn in plan mode
                    user_input = args
                else:
                    plan_armed = True          # arm the next turn
                    print("(plan mode armed — your next message runs in plan mode)")
                    continue
            elif raw_skill_tool is not None and name in names:
                # set the mode before executing (a fork skill spawns a subagent
                # that reads policy.mode) — but do NOT consume plan_armed yet:
                # if the skill fails/cancels below we `continue` without a turn,
                # and a real turn is what consumes the arming (line below)
                policy.mode = "plan" if run_in_plan else policy.base_mode
                print(f"(running /{name})")
                # journal it like a model-invoked skill call, and guard the
                # execute: a fork skill's LLM/tool failure, or Ctrl-C mid-fork,
                # must not crash the whole REPL (the unwrapped tool raises, so
                # this catches it — with_hooks would have hidden it in a string)
                record_action("agent", "skill", {"name": name, "args": args})
                try:
                    result = raw_skill_tool.execute(name=name, args=args)
                except KeyboardInterrupt:
                    print("\n(skill cancelled)")
                    continue
                except Exception as error:  # noqa: BLE001 — surface, don't die
                    print(f"(skill {name!r} failed: {error})")
                    continue
                if next(s for s in skills if s.name == name).fork:
                    # a fork skill already ran its subagent to a final answer;
                    # show it directly instead of feeding it into a second,
                    # top-level turn that would only paraphrase it at double cost
                    plan_armed = False  # a completed invocation consumes the arming
                    print(result)
                    continue
                user_input = result  # a plain skill's body drives the turn below
            # else: an unknown /name is not a command — fall through and send
            # the original message to the model rather than swallowing it
            # (e.g. "/etc/hosts has a stale entry, can you check it?")
        # commit to a turn: fix the mode (idempotent if the skill branch set it)
        # and consume any arming
        policy.mode = "plan" if run_in_plan else policy.base_mode
        was_plan_turn = policy.mode == "plan"
        plan_armed = False
        try:
            reply = run_turn(
                messages,
                user_input,
                llm,
                tools=tools,
                on_tool_call=observe_tool_call,
                policy=policy,
                asker=asker,
                # a callable, re-read each iteration: when exit_plan_mode is
                # approved mid-turn, policy.mode flips and the PLAN_MODE section
                # drops for the rest of the turn
                system=lambda: current_system_prompt(
                    workspace,
                    context_sections + ([PLAN_MODE] if policy.mode == "plan" else []),
                ),
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
            if was_plan_turn and policy.mode == "plan":
                # the plan was never approved (approval would have restored
                # base_mode). Plan mode is per-turn, so the next message runs in
                # the base mode — say so, mirroring the "(plan armed)" notice, so
                # the user isn't surprised that a rejected plan can now be acted on
                print(f"(plan mode ended — your next message runs in {policy.base_mode} mode)")
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
