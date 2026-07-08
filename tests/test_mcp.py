import sys
import time
from pathlib import Path

import pytest

from harness.loop import run_turn
from harness.mcp import MCPError, MCPServer, mcp_tools
from tests.fake_llm import FakeLLM

FAKE = Path(__file__).parent / "fake_mcp_server.py"
COMMAND = f'"{sys.executable}" "{FAKE}"'


@pytest.fixture
def server():
    s = MCPServer("fake", COMMAND, timeout=5)
    s.start()
    yield s
    s.close()


def test_handshake_and_discovery(server):
    tools = server.list_tools()
    names = [t["name"] for t in tools]
    assert "echo" in names and "crash" in names
    echo = next(t for t in tools if t["name"] == "echo")
    assert echo["description"] == "Echo the text back."
    assert echo["inputSchema"]["properties"]["text"] == {"type": "string"}


def test_call_round_trips_text(server):
    assert server.call("echo", {"text": "hi"}) == "hi"


def test_bridged_tools_are_namespaced_registry_entries(server):
    tools = {t.name: t for t in mcp_tools(server)}
    assert "fake__echo" in tools and "fake__shout" in tools
    # the server's inputSchema IS the Tool's parameters — same JSON schema
    assert tools["fake__echo"].parameters["required"] == ["text"]
    assert tools["fake__shout"].execute(text="hi") == "HI"


def test_read_only_honors_the_hint_and_defaults_closed(server):
    tools = {t.name: t for t in mcp_tools(server)}
    assert tools["fake__echo"].read_only is True  # declared readOnlyHint
    # no annotations: unknown side effects must face the permission gate
    assert tools["fake__shout"].read_only is False


def test_a_schema_without_properties_still_bridges(server):
    tools = {t.name: t for t in mcp_tools(server)}
    assert tools["fake__ping"].parameters["properties"] == {}
    assert tools["fake__ping"].execute() == "pong"


def test_unknown_tool_is_an_error_string(server):
    result = server.call("nope", {})
    assert result.startswith("Error:") and "unknown tool" in result


def test_is_error_results_read_as_errors(server):
    assert server.call("fail", {}) == "Error: deliberate failure"


def test_server_death_is_an_error_string_not_an_exception(server):
    result = server.call("crash", {})
    assert result.startswith("Error:") and "exited" in result
    # the corpse stays a string-returner: the model can carry on
    assert server.call("echo", {"text": "hi"}).startswith("Error:")


def test_timeout_is_an_error_string_and_late_replies_are_skipped():
    s = MCPServer("fake", COMMAND, timeout=0.2)
    s.start()
    try:
        s.timeout = 0.2
        result = s.call("slow", {"seconds": 0.6})
        assert result.startswith("Error:") and "timed out" in result
        # the abandoned reply lands in the pipe; the id-matching reader
        # must skip it instead of serving it as the next call's answer
        time.sleep(0.7)
        s.timeout = 5
        assert s.call("echo", {"text": "still sane"}) == "still sane"
    finally:
        s.close()


def test_results_are_truncated_like_native_tools(server):
    tools = {t.name: t for t in mcp_tools(server, output_limit=1000)}
    result = tools["fake__huge"].execute()
    assert "truncated" in result and len(result) < 2000


def test_close_terminates_the_child(server):
    server.close()
    assert server._proc.poll() is not None
    assert server.call("echo", {"text": "hi"}).startswith("Error:")


def test_the_loop_drives_a_foreign_process_it_has_never_heard_of(server):
    # invariant 2, cashed: run_turn dispatches by registry name only, so a
    # tool backed by a live child process needs zero loop changes
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [{"name": "fake__echo", "arguments": {"text": "over the wire"}}],
            },
            {"type": "text", "content": "done"},
        ]
    )
    registry = {t.name: t for t in mcp_tools(server)}
    messages = []
    run_turn(messages, "go", llm, tools=registry)
    tool_result = next(m for m in messages if m["role"] == "tool")
    assert tool_result["content"] == "over the wire"


def test_a_server_that_speaks_garbage_fails_at_startup():
    s = MCPServer("bad", "echo not-json; sleep 5")
    with pytest.raises(MCPError):
        s.start()
    s.close()


def test_a_server_that_dies_immediately_fails_at_startup():
    with pytest.raises(MCPError):
        MCPServer("gone", "true").start()
