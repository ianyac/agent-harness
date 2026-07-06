import json

import pytest

from harness.hooks import (
    Hook,
    HookError,
    HookSet,
    load_hooks,
    run_session_start,
    run_stop,
    with_hooks,
)
from harness.tools.base import Tool


def noop_tool() -> Tool:
    return Tool(
        name="noop",
        description="A tool that does nothing, for tests.",
        parameters={"type": "object", "properties": {}},
        execute=lambda **args: "ok",
    )


def hooked(hookset: HookSet, **kwargs) -> Tool:
    tools = with_hooks({"noop": noop_tool()}, hookset, **kwargs)
    return tools["noop"]


def test_exit_2_pre_hook_blocks_with_its_stderr():
    hookset = HookSet(pre_tool_use=[Hook(command="echo NOPE >&2; exit 2")])
    assert hooked(hookset).execute() == "Blocked by hook: NOPE"


def test_exit_0_pre_hook_proceeds():
    hookset = HookSet(pre_tool_use=[Hook(command="exit 0")])
    assert hooked(hookset).execute() == "ok"


def test_matcher_scopes_hooks_to_named_tools():
    blocker = Hook(command="exit 2", matcher="bash")
    assert hooked(HookSet(pre_tool_use=[blocker])).execute() == "ok"
    matching = Hook(command="exit 2", matcher="noop|bash")
    assert hooked(HookSet(pre_tool_use=[matching])).execute().startswith(
        "Blocked by hook"
    )


def test_pre_hook_receives_the_call_payload(tmp_path):
    capture = tmp_path / "payload.json"
    hookset = HookSet(pre_tool_use=[Hook(command=f"cat > {capture}")])
    hooked(hookset).execute(path="x")
    assert json.loads(capture.read_text()) == {
        "event": "pre_tool_use",
        "tool": "noop",
        "args": {"path": "x"},
    }


def test_post_hook_receives_the_result(tmp_path):
    capture = tmp_path / "payload.json"
    hookset = HookSet(post_tool_use=[Hook(command=f"cat > {capture}")])
    assert hooked(hookset).execute() == "ok"
    payload = json.loads(capture.read_text())
    assert payload["event"] == "post_tool_use"
    assert payload["result"] == "ok"


def test_a_crashed_pre_hook_fails_closed():
    # a policy hook that cannot run must block — silently skipping
    # enforcement is the one wrong answer
    hookset = HookSet(pre_tool_use=[Hook(command="exit 1")])
    assert hooked(hookset).execute().startswith("Blocked by hook")


def test_a_timed_out_pre_hook_fails_closed():
    hookset = HookSet(pre_tool_use=[Hook(command="sleep 5")])
    result = hooked(hookset, timeout=0.2).execute()
    assert result.startswith("Blocked by hook")
    assert "timed out" in result


def test_a_crashed_post_hook_warns_and_proceeds():
    # observers have nothing to halt: fail loud, never alter the result
    hookset = HookSet(post_tool_use=[Hook(command="echo broken >&2; exit 1")])
    warnings = []
    assert hooked(hookset, on_warning=warnings.append).execute() == "ok"
    assert warnings and "broken" in warnings[0]


def test_session_start_stdout_is_returned_for_injection():
    hookset = HookSet(session_start=[Hook(command="echo CONTEXT LINE")])
    assert run_session_start(hookset) == ["CONTEXT LINE"]


def test_a_broken_session_start_hook_fails_closed():
    hookset = HookSet(session_start=[Hook(command="exit 1")])
    with pytest.raises(HookError):
        run_session_start(hookset)


def test_stop_hook_failures_are_warnings_not_errors():
    hookset = HookSet(stop=[Hook(command="exit 1")])
    reply = {"role": "assistant", "content": "done"}
    warnings = run_stop(hookset, reply)
    assert warnings and "stop hook failed" in warnings[0]
    assert run_stop(HookSet(stop=[Hook(command="true")]), reply) == []


def test_missing_config_is_an_empty_hookset(tmp_path):
    assert load_hooks(tmp_path / "absent.json") == HookSet()


def test_unknown_event_is_a_hard_error(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"pre_toll_use": []}))  # typo must not pass
    with pytest.raises(ValueError):
        load_hooks(path)


def test_an_entry_without_a_command_is_a_hard_error(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"stop": [{"matcher": "bash"}]}))
    with pytest.raises(ValueError):
        load_hooks(path)


def test_load_parses_events_and_matchers(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {
                "pre_tool_use": [{"command": "exit 2", "matcher": "bash"}],
                "session_start": [{"command": "echo hi"}],
            }
        )
    )
    hookset = load_hooks(path)
    assert hookset.pre_tool_use == [Hook(command="exit 2", matcher="bash")]
    assert hookset.session_start == [Hook(command="echo hi")]
    assert hookset.post_tool_use == [] and hookset.stop == []
