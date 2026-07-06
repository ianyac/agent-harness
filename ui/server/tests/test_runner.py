import queue
import threading

from harness.permissions import PermissionPolicy
from harness.tools.base import Tool

from ui.server.runner import TurnCancelled, TurnRunner
from ui.server.tests.fake_llm import FakeLLM, text_reply, tool_reply


def echo_tool(read_only=True):
    return Tool(
        name="echo",
        description="echo x back",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        execute=lambda x: f"echo:{x}",
        read_only=read_only,
    )


def make_runner(llm, messages=None, tools=None, policy=None, **kwargs):
    events_q = queue.Queue()
    messages = messages if messages is not None else []
    runner = TurnRunner(
        llm=llm,
        tools=tools or {},
        policy=policy,
        system_prompt=lambda: "test system",
        messages=messages,
        emit=events_q.put,
        **kwargs,
    )
    return runner, events_q, messages


def run_to_completion(runner, text):
    assert runner.try_begin()
    runner.run_turn_blocking(text)


def drain(events_q):
    out = []
    while True:
        try:
            out.append(events_q.get_nowait())
        except queue.Empty:
            return out


def wait_for(events_q, type_, timeout=5):
    """Pop events until one of the given type arrives (or time out)."""
    while True:
        event = events_q.get(timeout=timeout)
        if event["type"] == type_:
            return event


def test_text_only_turn():
    runner, events_q, messages = make_runner(FakeLLM([text_reply("hi there")]))
    run_to_completion(runner, "hello")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == ["turn_started", "turn_done"]
    assert evts[-1]["messages"] is not messages  # a copy, not the live list
    assert evts[-1]["messages"] == messages
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[-1]["content"] == "hi there"
    assert runner.running is False


def test_tool_turn_emits_call_and_result():
    llm = FakeLLM([tool_reply(("echo", {"x": 7})), text_reply("done")])
    runner, events_q, messages = make_runner(llm, tools={"echo": echo_tool()})
    run_to_completion(runner, "go")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == [
        "turn_started", "tool_call", "tool_result", "turn_done",
    ]
    assert evts[1] == {"type": "tool_call", "name": "echo", "args": {"x": 7}}
    assert evts[2] == {"type": "tool_result", "name": "echo", "result": "echo:7"}
    assert messages[-1]["content"] == "done"
    assert any(m.get("role") == "tool" and m["content"] == "echo:7" for m in messages)


def test_llm_failure_rolls_back_and_emits_turn_error():
    class ExplodingLLM:
        def complete(self, messages, tools=None, system=None):
            raise RuntimeError("boom")

    prior = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
    ]
    runner, events_q, messages = make_runner(ExplodingLLM(), messages=list(prior))
    run_to_completion(runner, "new question")
    evts = drain(events_q)
    assert [e["type"] for e in evts] == ["turn_started", "turn_error"]
    assert "RuntimeError: boom" in evts[-1]["message"]
    assert messages == prior  # the broken exchange is gone, history intact
    assert runner.running is False


def test_error_on_first_turn_rolls_back_to_empty():
    class ExplodingLLM:
        def complete(self, messages, tools=None, system=None):
            raise RuntimeError("boom")

    runner, events_q, messages = make_runner(ExplodingLLM())
    run_to_completion(runner, "hello")
    assert messages == []


def test_try_begin_rejects_second_claim():
    runner, _, _ = make_runner(FakeLLM([text_reply("hi")]))
    assert runner.try_begin() is True
    assert runner.try_begin() is False
    runner.run_turn_blocking("hello")
    assert runner.try_begin() is True  # slot free again after the turn


def run_on_thread(runner, text):
    assert runner.try_begin()
    thread = threading.Thread(target=runner.run_turn_blocking, args=(text,))
    thread.start()
    return thread


def test_permission_yes_runs_the_tool():
    llm = FakeLLM([tool_reply(("echo", {"x": 1})), text_reply("ok")])
    runner, events_q, _ = make_runner(
        llm,
        tools={"echo": echo_tool(read_only=False)},
        policy=PermissionPolicy("default"),
    )
    thread = run_on_thread(runner, "go")
    request = wait_for(events_q, "permission_request")
    assert request["name"] == "echo" and runner.pending_permission["id"] == request["id"]
    runner.answer_permission(request["id"], "yes")
    thread.join(timeout=5)
    assert not thread.is_alive()
    types = [e["type"] for e in drain(events_q)]
    assert "tool_call" in types and "tool_result" in types and "turn_done" in types
    assert runner.pending_permission is None


def test_permission_no_denies_without_running():
    executed = []
    tool = Tool(
        name="touchy",
        description="side effect",
        parameters={"type": "object", "properties": {}},
        execute=lambda: executed.append(True) or "did it",
        read_only=False,
    )
    llm = FakeLLM([tool_reply(("touchy", {})), text_reply("understood")])
    runner, events_q, messages = make_runner(
        llm, tools={"touchy": tool}, policy=PermissionPolicy("default")
    )
    thread = run_on_thread(runner, "go")
    request = wait_for(events_q, "permission_request")
    runner.answer_permission(request["id"], "no")
    thread.join(timeout=5)
    assert executed == []
    assert any(
        m.get("role") == "tool" and "Permission denied" in m["content"]
        for m in messages
    )
    assert [e["type"] for e in drain(events_q)][-1] == "turn_done"


def test_permission_always_skips_second_prompt():
    llm = FakeLLM([
        tool_reply(("echo", {"x": 1})),
        tool_reply(("echo", {"x": 2})),
        text_reply("done"),
    ])
    runner, events_q, _ = make_runner(
        llm,
        tools={"echo": echo_tool(read_only=False)},
        policy=PermissionPolicy("default"),
    )
    thread = run_on_thread(runner, "go")
    request = wait_for(events_q, "permission_request")
    runner.answer_permission(request["id"], "always")
    thread.join(timeout=5)
    remaining = [e["type"] for e in drain(events_q)]
    assert remaining.count("permission_request") == 0  # allowlisted after "always"
    assert remaining.count("tool_result") == 2


def test_stale_permission_answer_is_ignored():
    runner, _, _ = make_runner(FakeLLM([text_reply("hi")]))
    runner.answer_permission("perm-999", "yes")  # nothing pending: no crash
