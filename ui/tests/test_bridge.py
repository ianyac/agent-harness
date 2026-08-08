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


@pytest.mark.asyncio
async def test_event_sink_serializes_sequence_allocation_through_queue_delivery():
    class CoordinatedLoop:
        def __init__(self):
            self.second_delivered = threading.Event()

        def call_soon_threadsafe(self, callback, event):
            if event.sequence == 1:
                self.second_delivered.wait(timeout=0.1)
            callback(event)
            if event.sequence == 2:
                self.second_delivered.set()

    sink = EventSink("s1", 1, CoordinatedLoop())
    first = threading.Thread(
        target=sink.emit,
        args=("assistant_delta",),
        kwargs={"turn_id": "t1", "text": "first"},
    )
    second = threading.Thread(
        target=sink.emit,
        args=("assistant_delta",),
        kwargs={"turn_id": "t1", "text": "second"},
    )

    first.start()
    second.start()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert [(await sink.next()).sequence, (await sink.next()).sequence] == [1, 2]


@pytest.mark.asyncio
async def test_event_sink_rejects_reserved_envelope_overrides_without_a_gap():
    sink = EventSink("s1", 1, asyncio.get_running_loop())

    with pytest.raises(ValueError, match="reserved event field"):
        sink.emit(
            "assistant_delta",
            turn_id="t1",
            text="bad",
            session_id="other",
            sequence=99,
        )

    sink.emit("assistant_delta", turn_id="t1", text="good")
    event = await sink.next()
    assert event.session_id == "s1"
    assert event.sequence == 1


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


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("permission", "no"), ("plan", (False, ""))],
)
def test_decision_broker_disconnect_before_request_fails_closed(
    kind, expected
):
    broker = DecisionBroker()
    token = CancellationToken()
    announced = threading.Event()
    broker.disconnect()

    if kind == "permission":
        result = broker.request_permission("p1", announced.set, token=token)
        accepted = broker.answer_permission("p1", "yes")
    else:
        result = broker.request_plan("r1", announced.set, token=token)
        accepted = broker.answer_plan("r1", True, "approve after disconnect")

    assert result == expected
    assert announced.is_set() is False
    assert broker.pending_request_id is None
    assert accepted is False


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("permission", "no"), ("plan", (False, ""))],
)
def test_decision_broker_cancellation_wakes_an_already_pending_wait(
    kind, expected
):
    broker = DecisionBroker()
    token = CancellationToken()
    ready = threading.Event()
    result: list[object] = []
    errors: list[BaseException] = []

    def request_decision():
        try:
            if kind == "permission":
                answer = broker.request_permission("p1", ready.set, token=token)
            else:
                answer = broker.request_plan("r1", ready.set, token=token)
            result.append(answer)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=request_decision)
    worker.start()
    assert ready.wait(timeout=1)

    token.cancel()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert errors == []
    assert result == [expected]
    assert broker.pending_request_id is None
