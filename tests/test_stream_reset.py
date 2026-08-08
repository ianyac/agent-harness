import inspect
import json
import pathlib

import httpx
import pytest

from harness.folding import FoldConfig, FoldingContext
from harness.llm import CodexAdapter, LLMClient
from harness.loop import run_turn
from harness.tools.agent import agent_tool
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool


def test_stream_reset_callback_is_exposed_by_every_public_completion_seam():
    for complete in (
        LLMClient.complete,
        CodexAdapter.complete,
        FakeLLM.complete,
        run_turn,
    ):
        parameter = inspect.signature(complete).parameters["on_stream_reset"]
        assert parameter.default is None


def test_stream_reset_is_appended_after_every_preexisting_parameter():
    for complete in (LLMClient.complete, CodexAdapter.complete, FakeLLM.complete):
        assert list(inspect.signature(complete).parameters)[-2:] == [
            "projection_hash",
            "on_stream_reset",
        ]
    assert list(inspect.signature(run_turn).parameters)[-2:] == [
        "context",
        "on_stream_reset",
    ]


def test_fake_llm_preserves_legacy_positional_projection_hash_behavior():
    positional = FakeLLM([{"type": "text", "content": "same"}])
    keyword = FakeLLM([{"type": "text", "content": "same"}])
    positional_chunks = []
    keyword_chunks = []

    positional_reply = positional.complete(
        [], None, None, positional_chunks.append, "legacy-hash"
    )
    keyword_reply = keyword.complete(
        [],
        on_text_delta=keyword_chunks.append,
        projection_hash="legacy-hash",
    )

    assert positional_reply == keyword_reply
    assert positional_chunks == keyword_chunks
    assert positional.turns[0]["projection_hash"] == "legacy-hash"
    assert positional.turns[0] == keyword.turns[0]


def test_run_turn_preserves_legacy_positional_folding_context_behavior(tmp_path):
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "legacy-position",
        config=FoldConfig(min_span_tokens=0),
    )
    llm = FakeLLM([{"type": "text", "content": "same"}])
    messages = []
    try:
        reply = run_turn(
            messages,
            "go",
            llm,
            None,
            20,
            None,
            None,
            None,
            None,
            None,
            8,
            None,
            None,
            None,
            context,
        )
    finally:
        context.close()

    assert reply == {"role": "assistant", "content": "same"}
    assert llm.turns[0]["projection_hash"] is not None


class _CapturingLLM:
    def __init__(self, script):
        self.inner = FakeLLM(script)
        self.reset_callbacks = []

    def complete(
        self,
        messages,
        tools=None,
        system=None,
        on_text_delta=None,
        projection_hash=None,
        on_stream_reset=None,
    ):
        self.reset_callbacks.append(on_stream_reset)
        return self.inner.complete(
            messages,
            tools=tools,
            system=system,
            on_text_delta=on_text_delta,
            on_stream_reset=on_stream_reset,
            projection_hash=projection_hash,
        )


def test_run_turn_forwards_one_reset_callback_through_every_model_iteration():
    llm = _CapturingLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    resets = []
    callback = lambda: resets.append("reset")

    run_turn(
        [],
        "go",
        llm,
        tools={"noop": noop_tool()},
        on_stream_reset=callback,
    )

    assert llm.reset_callbacks == [callback, callback]
    assert resets == []


def test_run_turn_does_not_forward_reset_callbacks_to_compaction():
    messages = []
    for i in range(6):
        messages.append({"role": "user", "content": f"q{i} " + "detail " * 30})
        messages.append(
            {"role": "assistant", "content": f"a{i} " + "detail " * 30}
        )
    llm = _CapturingLLM(
        [
            {"type": "text", "content": "SUMMARY"},
            {"type": "text", "content": "done"},
        ]
    )
    callback = lambda: None

    run_turn(
        messages,
        "next",
        llm,
        compact_threshold=50,
        keep_recent=2,
        on_stream_reset=callback,
    )

    assert llm.reset_callbacks == [None, callback]


def test_run_turn_does_not_forward_reset_callbacks_to_subagents():
    llm = _CapturingLLM(
        [
            {
                "type": "tool_calls",
                "calls": [{"name": "agent", "arguments": {"task": "x"}}],
            },
            {"type": "text", "content": "sub answer"},
            {"type": "text", "content": "parent answer"},
        ]
    )
    tools = {}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
    callback = lambda: None

    run_turn([], "go", llm, tools=tools, on_stream_reset=callback)

    assert llm.reset_callbacks == [callback, None, callback]


def test_omitting_reset_callback_keeps_legacy_llm_callers_compatible():
    class LegacyLLM:
        def complete(
            self,
            messages,
            tools=None,
            system=None,
            on_text_delta=None,
            projection_hash=None,
        ):
            return {"role": "assistant", "content": "unchanged"}

    reply = run_turn([], "go", LegacyLLM())

    assert reply == {"role": "assistant", "content": "unchanged"}


def test_fake_llm_scripts_a_reset_before_the_following_output():
    llm = FakeLLM(
        [
            {"type": "stream_reset"},
            {"type": "text", "content": "fresh"},
        ]
    )
    events = []

    reply = llm.complete(
        [],
        on_stream_reset=lambda: events.append("reset"),
        on_text_delta=lambda delta: events.append(f"text:{delta}"),
    )

    assert reply == {"role": "assistant", "content": "fresh"}
    assert events == ["reset", "text:fresh"]


class _StreamResponse:
    def __init__(self, events, status_code=200):
        self.events = events
        self.status_code = status_code
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_lines(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield "data: " + json.dumps(event)

    def read(self):
        return b""


class _StreamClient:
    def __init__(self, responses, events):
        self.responses = iter(responses)
        self.events = events
        self.attempts = 0
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def stream(self, *_args, **kwargs):
        self.attempts += 1
        self.events.append("attempt")
        self.requests.append(kwargs)
        return next(self.responses)


def _completed(text):
    return {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        },
    }


def _delta(text):
    return {"type": "response.output_text.delta", "delta": text}


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text(
        json.dumps({"tokens": {"access_token": "offline", "account_id": "test"}})
    )
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    return CodexAdapter()


def _install_stream(monkeypatch, responses, events):
    client = _StreamClient(responses, events)
    monkeypatch.setattr("harness.llm.httpx.Client", lambda **_kwargs: client)
    return client


def test_adapter_resets_immediately_before_retrying_an_attempt_that_streamed_text(
    adapter, monkeypatch
):
    events = []
    _install_stream(
        monkeypatch,
        [
            _StreamResponse([_delta("stale"), httpx.ReadError("lost")]),
            _StreamResponse([_delta("fresh"), _completed("fresh")]),
        ],
        events,
    )

    reply = adapter.complete(
        [],
        on_text_delta=lambda text: events.append(f"text:{text}"),
        on_stream_reset=lambda: events.append("reset"),
    )

    assert reply["content"] == "fresh"
    assert events == ["attempt", "text:stale", "reset", "attempt", "text:fresh"]


def test_adapter_does_not_reset_on_the_first_attempt(adapter, monkeypatch):
    resets = []
    _install_stream(
        monkeypatch,
        [_StreamResponse([_delta("only"), _completed("only")])],
        [],
    )

    adapter.complete([], on_stream_reset=lambda: resets.append("reset"))

    assert resets == []


def test_adapter_preserves_legacy_positional_projection_hash_behavior(
    adapter, monkeypatch
):
    client = _install_stream(
        monkeypatch,
        [_StreamResponse([_completed("same")])],
        [],
    )

    reply = adapter.complete([], None, None, None, "legacy-hash")

    assert reply == {"role": "assistant", "content": "same"}
    assert client.requests[0]["headers"]["x-agent-projection-hash"] == "legacy-hash"


@pytest.mark.parametrize("first_events", [[], [_delta("")]])
def test_adapter_does_not_reset_before_retry_without_nonempty_text(
    adapter, monkeypatch, first_events
):
    resets = []
    _install_stream(
        monkeypatch,
        [
            _StreamResponse([*first_events, httpx.ReadError("lost")]),
            _StreamResponse([_completed("fresh")]),
        ],
        [],
    )

    reply = adapter.complete([], on_stream_reset=lambda: resets.append("reset"))

    assert reply["content"] == "fresh"
    assert resets == []


def test_reset_callback_exceptions_propagate_and_prevent_the_retry(
    adapter, monkeypatch
):
    class StopRetry(Exception):
        pass

    client = _install_stream(
        monkeypatch,
        [
            _StreamResponse([_delta("stale"), httpx.ReadError("lost")]),
            _StreamResponse([_completed("must not run")]),
        ],
        [],
    )

    with pytest.raises(StopRetry):
        adapter.complete(
            [],
            on_text_delta=lambda _text: None,
            on_stream_reset=lambda: (_ for _ in ()).throw(StopRetry()),
        )

    assert client.attempts == 1


def test_retry_shaped_reset_callback_exceptions_are_not_retried(
    adapter, monkeypatch
):
    client = _install_stream(
        monkeypatch,
        [
            _StreamResponse([_delta("stale"), httpx.ReadError("stream lost")]),
            _StreamResponse([_completed("must not run")]),
        ],
        [],
    )
    callback_error = httpx.ReadError("cancel")
    callback_calls = []

    def cancel():
        callback_calls.append("called")
        raise callback_error

    with pytest.raises(httpx.ReadError) as caught:
        adapter.complete(
            [],
            on_text_delta=lambda _text: None,
            on_stream_reset=cancel,
        )

    assert caught.value is callback_error
    assert callback_calls == ["called"]
    assert client.attempts == 1


def test_adapter_retry_behavior_is_unchanged_when_reset_callback_is_omitted(
    adapter, monkeypatch
):
    client = _install_stream(
        monkeypatch,
        [
            _StreamResponse([httpx.ReadError("lost")]),
            _StreamResponse([_completed("fresh")]),
        ],
        [],
    )

    reply = adapter.complete([])

    assert reply["content"] == "fresh"
    assert client.attempts == 2
