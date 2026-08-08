import asyncio
import threading
import time
from pathlib import Path

import pytest
import httpx

from harness.llm import RetryableHTTPError
from harness.tools.base import Tool
from server.bridge import CancellationToken, EventSink
from server.runner import TurnRunner
from server.runtime import HarnessRuntime, RuntimeConfig
from tests.fake_llm import FakeLLM


def _tool(name: str, execute, *, read_only: bool = False) -> Tool:
    return Tool(
        name=name,
        description=f"Test tool {name}",
        parameters={"type": "object", "properties": {}},
        execute=execute,
        read_only=read_only,
    )


@pytest.fixture
def make_runtime(tmp_path: Path):
    runtimes: list[HarnessRuntime] = []

    def make(
        llm: FakeLLM,
        *,
        mode: str = "default",
        compact_threshold: int = 100_000,
    ) -> HarnessRuntime:
        workspace = tmp_path / f"workspace-{len(runtimes)}"
        workspace.mkdir()
        session_path = workspace / ".agent" / f"session-{len(runtimes)}.jsonl"
        runtime = HarnessRuntime(
            RuntimeConfig(
                session_id=session_path.stem,
                workspace=workspace,
                mode=mode,
                context_mode="compaction",
                compact_threshold=compact_threshold,
            ),
            llm,
            session_path,
            search_provider=None,
        )
        runtimes.append(runtime)
        return runtime

    yield make

    for runtime in runtimes:
        runtime.close()


def _sink() -> EventSink:
    return EventSink("session", 1, asyncio.get_running_loop())


async def _run(runner: TurnRunner, sink: EventSink, *, mode: str = "base"):
    await asyncio.to_thread(
        runner.run,
        "hello",
        mode,
        "turn-1",
        sink,
        CancellationToken(),
    )


async def _events_until(sink: EventSink, terminal: str) -> list:
    events = []
    while True:
        event = await asyncio.wait_for(sink.next(), timeout=2)
        events.append(event)
        if event.type == terminal:
            return events


@pytest.mark.asyncio
async def test_runner_streams_text_and_completes_with_authoritative_messages(
    make_runtime,
):
    runtime = make_runtime(FakeLLM([{"type": "text", "content": "hello back"}]))
    runner = TurnRunner(runtime)
    sink = _sink()

    await _run(runner, sink)
    events = await _events_until(sink, "turn_completed")

    assert [event.type for event in events] == [
        "turn_started",
        "assistant_delta",
        "assistant_delta",
        "turn_completed",
    ]
    assert "".join(event.text for event in events[1:-1]) == "hello back"
    assert events[-1].messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hello back"},
    ]
    assert events[-1].messages == runtime.session_log.load()
    assert events[-1].final_text == "hello back"


@pytest.mark.asyncio
async def test_runner_emits_stream_reset_before_fresh_retry_text(make_runtime):
    runtime = make_runtime(
        FakeLLM(
            [
                {"type": "stream_reset"},
                {"type": "text", "content": "fresh"},
            ]
        )
    )
    runner = TurnRunner(runtime)
    sink = _sink()

    await _run(runner, sink)
    events = await _events_until(sink, "turn_completed")

    assert [event.type for event in events] == [
        "turn_started",
        "stream_reset",
        "assistant_delta",
        "turn_completed",
    ]


@pytest.mark.asyncio
async def test_runner_preserves_nested_activity_parentage_and_harness_error_text(
    make_runtime,
):
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [{"name": "agent", "arguments": {"task": "inspect"}}],
            },
            {
                "type": "tool_calls",
                "calls": [{"name": "explode", "arguments": {}}],
            },
            {"type": "text", "content": "subagent recovered"},
            {"type": "text", "content": "parent finished"},
        ]
    )
    runtime = make_runtime(llm, mode="acceptAll")

    def explode():
        raise ValueError("boom")

    runtime.tools["explode"] = _tool("explode", explode)
    runner = TurnRunner(runtime)
    sink = _sink()

    await _run(runner, sink)
    events = await _events_until(sink, "turn_completed")
    started = [event for event in events if event.type == "activity_started"]
    completed = [event for event in events if event.type == "activity_completed"]

    agent = next(event for event in started if event.name == "agent")
    child = next(event for event in started if event.name == "explode")
    child_done = next(event for event in completed if event.name == "explode")
    assert agent.actor == "subagent"
    assert agent.parent_activity_id is None
    assert child.parent_activity_id == agent.activity_id
    assert child_done.parent_activity_id == agent.activity_id
    assert child_done.result == "Error: ValueError: boom"
    assert child_done.is_error is True
    tool_result = next(
        message
        for message in llm.turns[2]["messages"]
        if message.get("tool_call_id") == "call_1"
    )
    assert child_done.result == tool_result["content"]


@pytest.mark.asyncio
async def test_permission_waits_for_matching_id_and_always_skips_the_next_gate(
    make_runtime,
):
    calls: list[str] = []
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "change", "arguments": {}}]},
            {"type": "tool_calls", "calls": [{"name": "change", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    runtime = make_runtime(llm)
    runtime.tools["change"] = _tool("change", lambda: calls.append("ran") or "ok")
    runner = TurnRunner(runtime)
    sink = _sink()
    task = asyncio.create_task(_run(runner, sink))

    assert (await sink.next()).type == "turn_started"
    requested = await sink.next()
    assert requested.type == "permission_requested"
    done, _ = await asyncio.wait({task}, timeout=0.02)
    assert done == set()
    assert runner.decisions.answer_permission("stale", "always") is False
    done, _ = await asyncio.wait({task}, timeout=0.02)
    assert done == set()
    assert runner.decisions.answer_permission(requested.request_id, "always") is True

    await asyncio.wait_for(task, timeout=2)
    events = [requested, *(await _events_until(sink, "turn_completed"))]

    assert calls == ["ran", "ran"]
    assert runtime.policy.session_allowlist == {"change"}
    assert sum(event.type == "permission_requested" for event in events) == 1
    assert any(event.type == "safety_updated" for event in events)


@pytest.mark.asyncio
async def test_plan_review_waits_for_matching_id_and_restores_base_mode(make_runtime):
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {"name": "exit_plan_mode", "arguments": {"plan": "Do it"}}
                ],
            },
            {"type": "text", "content": "approved and done"},
        ]
    )
    runtime = make_runtime(llm, mode="acceptAll")
    runner = TurnRunner(runtime)
    sink = _sink()
    task = asyncio.create_task(_run(runner, sink, mode="plan"))

    assert (await sink.next()).type == "turn_started"
    assert runtime.policy.mode == "plan"
    assert (await sink.next()).type == "activity_started"
    requested = await sink.next()
    assert requested.type == "plan_approval_requested"
    done, _ = await asyncio.wait({task}, timeout=0.02)
    assert done == set()
    assert runner.decisions.answer_plan("stale", True, "") is False
    done, _ = await asyncio.wait({task}, timeout=0.02)
    assert done == set()
    assert runner.decisions.answer_plan(requested.request_id, True, "ship") is True

    await asyncio.wait_for(task, timeout=2)
    events = [requested, *(await _events_until(sink, "turn_completed"))]

    resolved = next(event for event in events if event.type == "plan_approval_resolved")
    assert resolved.approved is True
    assert resolved.feedback == "ship"
    assert runtime.policy.mode == "acceptAll"


class _BlockingLLM:
    context_window = 128_000

    def __init__(self):
        self.streaming = threading.Event()
        self.release = threading.Event()

    def complete(
        self,
        messages,
        tools=None,
        system=None,
        on_text_delta=None,
        projection_hash=None,
        on_stream_reset=None,
    ):
        if on_text_delta is not None:
            on_text_delta("partial")
        self.streaming.set()
        assert self.release.wait(timeout=2)
        return {"role": "assistant", "content": "must roll back"}


@pytest.mark.asyncio
async def test_cancellation_rolls_back_partial_turn_to_transcript_boundary(
    make_runtime,
):
    llm = _BlockingLLM()
    runtime = make_runtime(llm)
    runtime.messages.extend(
        [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "boundary"},
        ]
    )
    runtime.session_log.record_turn(runtime.messages)
    runner = TurnRunner(runtime)
    sink = _sink()
    token = CancellationToken()
    task = asyncio.create_task(
        asyncio.to_thread(runner.run, "hello", "base", "turn-1", sink, token)
    )

    assert await asyncio.to_thread(llm.streaming.wait, 1)
    token.cancel()
    llm.release.set()
    await asyncio.wait_for(task, timeout=2)
    events = await _events_until(sink, "turn_cancelled")

    assert any(event.type == "assistant_delta" for event in events)
    assert runtime.messages == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "boundary"},
    ]
    assert runtime.session_log.load() == runtime.messages


@pytest.mark.asyncio
async def test_cancellation_after_in_place_compaction_emits_one_terminal_event(
    make_runtime,
):
    token = CancellationToken()

    class CancelAfterSummaryLLM:
        context_window = 128_000

        def __init__(self):
            self.calls = 0

        def complete(
            self,
            messages,
            tools=None,
            system=None,
            on_text_delta=None,
            projection_hash=None,
            on_stream_reset=None,
        ):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "older turns summary"}
            token.cancel()
            return {"role": "assistant", "content": "must be discarded"}

    runtime = make_runtime(CancelAfterSummaryLLM(), compact_threshold=1)
    for number in range(6):
        runtime.messages.extend(
            [
                {"role": "user", "content": f"question {number}"},
                {"role": "assistant", "content": f"answer {number}"},
            ]
        )
    runtime.session_log.record_turn(runtime.messages)
    original_length = len(runtime.messages)
    runner = TurnRunner(runtime)
    sink = _sink()

    await asyncio.to_thread(
        runner.run, "new question", "base", "turn-1", sink, token
    )
    events = await _events_until(sink, "turn_cancelled")
    await asyncio.sleep(0)

    assert [event.type for event in events].count("turn_cancelled") == 1
    assert events[-1].type == "turn_cancelled"
    assert len(runtime.messages) < original_length
    assert runtime.messages[-1] == {"role": "assistant", "content": "answer 5"}
    assert runtime.session_log.load() == runtime.messages
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sink.next(), timeout=0.01)


@pytest.mark.asyncio
async def test_cancellation_during_permission_wait_wakes_and_cancels_turn(
    make_runtime,
):
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "change", "arguments": {}}]},
            {"type": "text", "content": "must not continue"},
        ]
    )
    runtime = make_runtime(llm)
    runtime.tools["change"] = _tool("change", lambda: "changed")
    runner = TurnRunner(runtime)
    sink = _sink()
    token = CancellationToken()
    task = asyncio.create_task(
        asyncio.to_thread(runner.run, "hello", "base", "turn-1", sink, token)
    )

    assert (await sink.next()).type == "turn_started"
    requested = await sink.next()
    assert requested.type == "permission_requested"
    token.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.1)
    if not done:
        runner.decisions.disconnect()
    await asyncio.wait_for(task, timeout=2)
    events = await _events_until(sink, "turn_cancelled")

    assert done == {task}
    assert any(event.type == "permission_resolved" for event in events)
    assert runtime.messages == []


@pytest.mark.asyncio
async def test_runner_serializes_two_turns_in_one_runtime_worker_lane(make_runtime):
    class LaneLLM:
        context_window = 128_000

        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.calls = 0

        def complete(
            self,
            messages,
            tools=None,
            system=None,
            on_text_delta=None,
            projection_hash=None,
            on_stream_reset=None,
        ):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls += 1
                call = self.calls
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            content = f"answer {call}"
            if on_text_delta is not None:
                on_text_delta(content)
            return {"role": "assistant", "content": content}

    llm = LaneLLM()
    runtime = make_runtime(llm)
    first = TurnRunner(runtime)
    second = TurnRunner(runtime)
    first_sink = _sink()
    second_sink = EventSink("session", 2, asyncio.get_running_loop())

    await asyncio.gather(
        asyncio.to_thread(
            first.run,
            "first",
            "base",
            "turn-1",
            first_sink,
            CancellationToken(),
        ),
        asyncio.to_thread(
            second.run,
            "second",
            "base",
            "turn-2",
            second_sink,
            CancellationToken(),
        ),
    )

    assert llm.max_active == 1
    assert runtime.messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "answer 2"},
    ]


@pytest.mark.asyncio
async def test_two_runners_route_each_plan_review_to_the_active_turn_broker(
    make_runtime,
):
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {"name": "exit_plan_mode", "arguments": {"plan": "Plan A"}}
                ],
            },
            {"type": "text", "content": "A done"},
            {
                "type": "tool_calls",
                "calls": [
                    {"name": "exit_plan_mode", "arguments": {"plan": "Plan B"}}
                ],
            },
            {"type": "text", "content": "B done"},
        ]
    )
    runtime = make_runtime(llm, mode="acceptAll")
    runner_a = TurnRunner(runtime)
    runner_b = TurnRunner(runtime)

    async def run_and_approve(runner, label, generation):
        sink = EventSink("session", generation, asyncio.get_running_loop())
        task = asyncio.create_task(
            asyncio.to_thread(
                runner.run,
                f"question {label}",
                "plan",
                f"turn-{label}",
                sink,
                CancellationToken(),
            )
        )
        assert (await sink.next()).type == "turn_started"
        assert (await sink.next()).type == "activity_started"
        requested = await sink.next()
        assert requested.type == "plan_approval_requested"
        accepted = runner.decisions.answer_plan(requested.request_id, True, "")
        if not accepted:
            other = runner_b if runner is runner_a else runner_a
            other.decisions.answer_plan(requested.request_id, False, "cleanup")
        await asyncio.wait_for(task, timeout=2)
        await _events_until(sink, "turn_completed")
        return accepted

    assert await run_and_approve(runner_a, "a", 1) is True
    assert await run_and_approve(runner_b, "b", 2) is True


@pytest.mark.asyncio
async def test_turn_start_failure_restores_mode_and_context_and_emits_failure(
    make_runtime,
):
    runtime = make_runtime(FakeLLM([{"type": "text", "content": "unused"}]))
    runner = TurnRunner(runtime)

    class RejectStartedSink(EventSink):
        def emit(self, event_type: str, **payload: object) -> None:
            if event_type == "turn_started":
                raise ValueError("invalid turn_started")
            super().emit(event_type, **payload)

    sink = RejectStartedSink("session", 1, asyncio.get_running_loop())
    observed: list[tuple[BaseException | None, object, object, str]] = []

    def run_and_inspect_context():
        import server.runner as runner_module

        caught = None
        try:
            runner.run("hello", "plan", "turn-1", sink, CancellationToken())
        except BaseException as error:
            caught = error
        observed.append(
            (
                caught,
                runner_module._current_turn.get(),
                runner_module._current_activity.get(),
                runtime.policy.mode,
            )
        )

    worker = threading.Thread(target=run_and_inspect_context)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    failed = await asyncio.wait_for(sink.next(), timeout=1)

    assert observed == [(None, None, None, "default")]
    assert failed.type == "turn_failed"
    assert failed.error_category == "invalid_response"
    assert failed.message == "invalid turn_started"


def test_closed_event_loop_failure_restores_base_mode_without_escaping(make_runtime):
    runtime = make_runtime(FakeLLM([{"type": "text", "content": "unused"}]))
    runner = TurnRunner(runtime)
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    sink = EventSink("session", 1, closed_loop)

    runner.run("hello", "plan", "turn-1", sink, CancellationToken())

    assert runtime.policy.mode == "default"


@pytest.mark.asyncio
async def test_runner_rolls_back_and_categorizes_non_cancellation_failures(
    make_runtime,
):
    runtime = make_runtime(FakeLLM([{"type": "broken"}]))
    runner = TurnRunner(runtime)
    sink = _sink()

    await _run(runner, sink)
    events = await _events_until(sink, "turn_failed")

    failed = events[-1]
    assert failed.error_category == "invalid_response"
    assert failed.message == "unknown FakeLLM script entry type 'broken'"
    assert runtime.messages == []
    assert runtime.session_log.load() == []


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("stream lost"),
        RetryableHTTPError(503),
        RuntimeError("codex HTTP 401: unauthorized"),
        RuntimeError("codex stream ended without response.completed"),
    ],
)
@pytest.mark.asyncio
async def test_runner_categorizes_concrete_provider_failures(
    make_runtime, error
):
    class FailingLLM:
        context_window = 128_000

        def complete(self, *_args, **_kwargs):
            raise error

    runtime = make_runtime(FailingLLM())
    runner = TurnRunner(runtime)
    sink = _sink()

    await _run(runner, sink)
    events = await _events_until(sink, "turn_failed")

    assert events[-1].error_category == "provider"


@pytest.mark.asyncio
async def test_runner_keeps_unrecognized_runtime_errors_internal(make_runtime):
    class FailingLLM:
        context_window = 128_000

        def complete(self, *_args, **_kwargs):
            raise RuntimeError("programming bug")

    runtime = make_runtime(FailingLLM())
    runner = TurnRunner(runtime)
    sink = _sink()

    await _run(runner, sink)
    events = await _events_until(sink, "turn_failed")

    assert events[-1].error_category == "internal"
