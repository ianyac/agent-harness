from harness.tools.base import Tool

# modes a session may START in (via --mode). "plan" is deliberately NOT here:
# it is a per-turn mode entered mid-session with /plan, never selected at
# startup — starting in "plan" would make base_mode "plan" and trap the session.
STARTUP_MODES = ("default", "acceptAll", "readOnly")
MODES = (*STARTUP_MODES, "plan")  # all valid modes (PermissionPolicy + decide)


class PermissionPolicy:
    """Decides whether a tool call may run. The loop enforces; the asker
    (injected into run_turn) handles the human on "ask"."""

    def __init__(self, mode: str = "default"):
        if mode not in MODES:
            raise ValueError(f"unknown permission mode {mode!r}; choose from {MODES}")
        self.mode = mode
        self.base_mode = mode  # the mode to restore to when leaving plan mode
        self.session_allowlist: set[str] = set()

    def decide(self, tool: Tool) -> str:
        """Return "allow", "deny", or "ask"."""
        if tool.read_only or tool.name in self.session_allowlist:
            return "allow"
        match self.mode:
            case "acceptAll":
                return "allow"
            case "readOnly" | "plan":
                return "deny"
            case _:
                return "ask"
