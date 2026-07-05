import subprocess
import sys
from pathlib import Path

import pytest

from harness.sandbox import (
    LinuxSandbox,
    MacOSSandbox,
    NoSandbox,
    SandboxPolicy,
    macos_profile,
)

macos_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="sandbox-exec is macOS-only"
)


# --- platform-neutral: policy -> profile string, and the pass-through backend ---


def test_macos_profile_denies_by_default_and_allows_the_workspace():
    policy = SandboxPolicy(workspace=Path("/work/space"))
    profile = macos_profile(policy)
    assert "(deny default)" in profile
    assert "/work/space" in profile
    assert "file-write*" in profile


def test_macos_profile_blocks_network_by_default_and_opens_it_when_flagged():
    denied = macos_profile(SandboxPolicy(workspace=Path("/w"), allow_network=False))
    assert "(deny network*)" in denied
    allowed = macos_profile(SandboxPolicy(workspace=Path("/w"), allow_network=True))
    assert "(allow network*)" in allowed


def test_no_sandbox_runs_the_command_verbatim():
    argv = NoSandbox().wrap("echo hi")
    assert argv == ["sh", "-c", "echo hi"]


def test_macos_sandbox_wraps_with_sandbox_exec():
    argv = MacOSSandbox(SandboxPolicy(workspace=Path("/w"))).wrap("echo hi")
    assert argv[0] == "sandbox-exec"
    assert argv[-2:] == ["-c", "echo hi"]


def test_linux_sandbox_is_an_honest_stub():
    with pytest.raises(NotImplementedError, match="bwrap"):
        LinuxSandbox(SandboxPolicy(workspace=Path("/w"))).wrap("echo hi")


# --- macOS-only: the kernel actually enforces the walls ---


@macos_only
def test_write_inside_workspace_succeeds(tmp_path):
    argv = MacOSSandbox(SandboxPolicy(workspace=tmp_path)).wrap(
        f"echo hi > {tmp_path}/ok.txt"
    )
    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode == 0
    assert (tmp_path / "ok.txt").read_text().strip() == "hi"


@macos_only
def test_write_outside_workspace_is_blocked(tmp_path):
    outside = tmp_path.parent / "escapee.txt"
    argv = MacOSSandbox(SandboxPolicy(workspace=tmp_path)).wrap(
        f"echo pwned > {outside}"
    )
    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode != 0
    assert not outside.exists()


@macos_only
def test_network_is_blocked_when_disabled(tmp_path):
    argv = MacOSSandbox(SandboxPolicy(workspace=tmp_path)).wrap(
        "curl -sS --max-time 5 https://example.com"
    )
    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode != 0
