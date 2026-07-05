import sys
from pathlib import Path
from typing import Protocol


class SandboxPolicy:
    """Platform-neutral confinement policy: what a command may touch.

    The policy is the same everywhere; only the enforcement backend
    (which OS mechanism applies it) is platform-specific.
    """

    def __init__(self, workspace: Path, allow_network: bool = False):
        self.workspace = Path(workspace).resolve()
        self.allow_network = allow_network


class Sandbox(Protocol):
    def wrap(self, command: str) -> list[str]:
        """Turn a shell command into an argv that runs it confined."""
        ...


def macos_profile(policy: SandboxPolicy) -> str:
    """Build a sandbox-exec profile string. Pure function — no OS calls, so
    it is unit-testable on any platform."""
    network = "(allow network*)" if policy.allow_network else "(deny network*)"
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow file-read*)",  # reading is broadly allowed; writing is the risk
            f'(allow file-write* (subpath "{policy.workspace}"))',
            '(allow file-write* (subpath "/private/tmp"))',
            # Note: $TMPDIR (/private/var/folders/...) is deliberately NOT
            # writable — allowing that whole tree is too broad and would let a
            # command escape any workspace living under it. Scratch space is
            # the workspace or /tmp. (An over-permissive rule here was caught
            # by the escape test — exactly what enforcement testing is for.)
            '(allow file-write-data (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))',
            network,
        ]
    )


class MacOSSandbox:
    def __init__(self, policy: SandboxPolicy):
        self.policy = policy

    def wrap(self, command: str) -> list[str]:
        return [
            "sandbox-exec",
            "-p",
            macos_profile(self.policy),
            "sh",
            "-c",
            command,
        ]


class NoSandbox:
    """Pass-through: runs the command with no confinement. Used on platforms
    without a backend, and as the default in unit tests."""

    def wrap(self, command: str) -> list[str]:
        return ["sh", "-c", command]


class LinuxSandbox:
    def __init__(self, policy: SandboxPolicy):
        self.policy = policy

    def wrap(self, command: str) -> list[str]:
        # TODO: implement with bwrap (bubblewrap), e.g.
        #   bwrap --ro-bind / / --bind <workspace> <workspace> --unshare-net ...
        # Left unbuilt because it can't be run or tested on the macOS dev box;
        # writing it blind would ship unverified code.
        raise NotImplementedError(
            "Linux sandboxing is not implemented yet — needs a bwrap backend"
        )


def default_sandbox(policy: SandboxPolicy) -> Sandbox:
    """Pick the enforcement backend for the current platform."""
    if sys.platform == "darwin":
        return MacOSSandbox(policy)
    if sys.platform.startswith("linux"):
        return LinuxSandbox(policy)
    return NoSandbox()
