import asyncio
import threading

import pytest

from server.bridge import CancellationToken, DecisionBroker, EventSink, TurnCancelled


def test_cancel_token_raises_only_after_request():
    token = CancellationToken()
    token.check()
    token.cancel()
    with pytest.raises(TurnCancelled):
        token.check()


@pytest.mark.asyncio
async def test_event_sink_numbers_events_in_one_generation():
    sink = EventSink(session_id="s1", generation=3, loop=asyncio.get_running_loop())
    sink.emit("turn_started", turn_id="t1", mode="base")
    sink.emit("assistant_delta", turn_id="t1", text="hi")
    assert (await sink.next()).sequence == 1
    assert (await sink.next()).sequence == 2


@pytest.mark.asyncio
async def test_event_sink_bridges_worker_threads_into_typed_events():
    sink = EventSink(session_id="s1", generation=1, loop=asyncio.get_running_loop())
    worker = threading.Thread(
        target=sink.emit,
        args=("assistant_delta",),
        kwargs={"turn_id": "t1", "text": "threaded"},
    )
    worker.start()
    worker.join()

    event = await asyncio.wait_for(sink.next(), timeout=1)

    assert event.type == "assistant_delta"
    assert event.text == "threaded"


def test_decision_broker_accepts_only_one_matching_permission_answer():
    broker = DecisionBroker()
    ready = threading.Event()
    answer: list[str] = []
    worker = threading.Thread(
        target=lambda: answer.append(
            broker.request_permission("permission-1", ready.set)
        )
    )
    worker.start()
    assert ready.wait(timeout=1)

    assert broker.answer_permission("stale", "yes") is False
    assert broker.answer_plan("permission-1", True, "wrong kind") is False
    assert broker.answer_permission("permission-1", "always") is True
    assert broker.answer_permission("permission-1", "yes") is False
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert answer == ["always"]


@pytest.mark.parametrize(
    ("request_fn", "expected"),
    [
        (lambda broker, ready: broker.request_permission("p1", ready.set), "no"),
        (lambda broker, ready: broker.request_plan("r1", ready.set), (False, "")),
    ],
)
def test_decision_broker_disconnect_resolves_pending_request_safely(
    request_fn, expected
):
    broker = DecisionBroker()
    ready = threading.Event()
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(request_fn(broker, ready))
    )
    worker.start()
    assert ready.wait(timeout=1)

    broker.disconnect()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result == [expected]
