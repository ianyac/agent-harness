"""Lesson 23: what a subagent's skill build may do.

The rule these pin: a subagent only runs a skill's !`cmd` if it was already
allowed shell — otherwise a fork skill's `allowed-tools` would stop bounding
its capabilities. A sub that may not run them still gets the skill, with each
command rendered as a visible skip notice (never as bare template text, which
the model would read as data).
"""

from main import SHELL_TOOLS, may_run_skill_commands
from harness.permissions import NO_MUTATION_MODES
from harness.skills import NOT_RUN, discover, skill_tool
from tests.helpers import write_skill


def test_a_sub_with_shell_may_run_skill_commands():
    assert may_run_skill_commands({"bash": object(), "read_file": object()}, "default")


def test_a_sub_without_shell_may_not():
    # the gate that keeps allowed-tools a real bound: no bash → no skill shell
    assert not may_run_skill_commands({"read_file": object(), "skill": object()}, "default")


def test_a_mutation_denying_mode_denies_skill_commands_even_with_shell():
    # plan and readOnly are the SAME to decide() — both deny every mutating
    # call — so both must deny skill commands too, or a read-only session's
    # subagents run shell through a skill body
    for mode in NO_MUTATION_MODES:
        assert not may_run_skill_commands({"bash": object()}, mode), mode
    assert "readOnly" in NO_MUTATION_MODES and "plan" in NO_MUTATION_MODES


def test_modes_that_allow_mutation_allow_skill_commands():
    for mode in ("default", "acceptAll"):
        assert may_run_skill_commands({"bash": object()}, mode), mode


def test_an_unrecognised_shell_fails_closed():
    # a foreign (MCP) shell isn't in SHELL_TOOLS, so the decision fails closed
    assert "run_command" not in SHELL_TOOLS
    assert not may_run_skill_commands({"run_command": object()}, "default")


def test_the_no_shell_build_marks_commands_as_skipped(tmp_path):
    # the regression this replaced: the no-shell build re-emitted !`cmd` as
    # bare template text, indistinguishable from a command that ran and printed
    # nothing — the sub would answer from it as if it were live data
    write_skill(tmp_path, "gitstat", "d", "Status:\n!`git status --short`")
    out = skill_tool(discover(tmp_path)).execute(name="gitstat")   # no run
    assert "!`git status" not in out          # never bare template text
    assert out == f"Status:\n{NOT_RUN}"       # the skip is explicit


def test_a_capable_fork_build_still_expands_before_delegating(tmp_path):
    # the other half of the fork/expand reorder: refusing early must not have
    # cost a legitimate fork skill its expansion — fork_run receives the body
    # with commands already run
    write_skill(tmp_path, "research", "d", "ctx: !`echo LIVE`\ntask: $1", extra="context: fork\n")
    got = {}
    tool = skill_tool(
        discover(tmp_path),
        run=lambda cmd: "LIVE",
        fork_run=lambda task, model, allowed: got.update(task=task) or "ANSWER",
    )
    assert tool.execute(name="research", args="topic") == "ANSWER"
    assert got["task"] == "ctx: LIVE\ntask: topic"   # expanded, then delegated


def test_a_build_only_advertises_what_it_can_do(tmp_path):
    # the description is composed per build: a sub's build must not promise
    # fork or shell it does not have
    write_skill(tmp_path, "s", "d", "b")
    skills = discover(tmp_path)
    full = skill_tool(skills, run=lambda c: "x", fork_run=lambda *a: "y").description
    plain = skill_tool(skills).description
    assert "shell" in full and "subagent" in full
    assert "shell" not in plain and "subagent" not in plain


def test_a_build_only_advertises_skills_it_can_serve(tmp_path):
    # the not-found error is the only skill listing some subs ever see, so it
    # must match what that build can actually do — a fork skill it would refuse
    # and a command-bearing skill it cannot run are both absent
    write_skill(tmp_path, "notes", "d", "prose only")
    write_skill(tmp_path, "gitstat", "d", "!`git status`")
    write_skill(tmp_path, "deploy", "d", "b", extra="context: fork\n")
    skills = discover(tmp_path)

    no_shell = skill_tool(skills).execute(name="nope")
    assert "notes" in no_shell
    assert "gitstat" not in no_shell and "deploy" not in no_shell

    full = skill_tool(skills, run=lambda c: "x", fork_run=lambda *a: "y").execute(name="nope")
    assert "notes" in full and "gitstat" in full and "deploy" in full
