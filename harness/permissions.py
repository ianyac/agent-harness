from harness.tools.base import Tool

# modes a session may START in (via --mode). "plan" is deliberately NOT here:
# it is a per-turn mode entered mid-session with /plan, never selected at
# startup — starting in "plan" would make base_mode "plan" and trap the session.
STARTUP_MODES = ("default", "acceptAll", "readOnly")
# modes in which NO mutating action may run, whatever the allowlist says. Shared
# so anything deriving a capability from the mode (e.g. whether a subagent may
# run a skill's commands) cannot drift from what decide() actually enforces.
NO_MUTATION_MODES = ("readOnly", "plan")


class PermissionPolicy:
    """Decides whether a tool call may run. The loop enforces; the asker
    (injected into run_turn) handles the human on "ask"."""

    def __init__(self, mode: str = "default"):
        # a session may only START in a startup mode. "plan" is entered
        # per-turn by assigning self.mode later — never as base_mode, which
        # would trap the session with no escape. Guarding here (not only at
        # main.py's argparse) keeps every constructible policy escapable at
        # the library seam, for consumers that never pass through the CLI.
        if mode not in STARTUP_MODES:
            raise ValueError(
                f"cannot start in mode {mode!r}; choose from {STARTUP_MODES}"
            )
        self.mode = mode
        self.base_mode = mode  # the mode to restore to when leaving plan mode
        self.session_allowlist: set[str] = set()

    def decide(self, tool: Tool) -> str:
        """Return "allow", "deny", or "ask"."""
        if tool.read_only:
            return "allow"  # observing never needs a gate, in any mode
        match self.mode:
            case mode if mode in NO_MUTATION_MODES:
                # these modes deny ALL mutation outright — even a tool the user
                # "always"-allowed in an earlier turn. The allowlist must not
                # tunnel a mutating call through a read-only / plan turn.
                return "deny"
            case "acceptAll":
                return "allow"
            case _:  # default
                return "allow" if tool.name in self.session_allowlist else "ask"
