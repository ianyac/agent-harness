import os

import pytest

from tests.fake_llm import FakeLLM


def available_clients():
    clients = [FakeLLM([{"type": "text", "content": "scripted reply"}])]
    if os.environ.get("RUN_CODEX_TESTS") == "1":
        from harness.llm import CodexAdapter

        clients.append(CodexAdapter())
    return clients


@pytest.mark.parametrize("client", available_clients())
def test_complete_returns_a_nonempty_assistant_message(client):
    reply = client.complete(
        [{"role": "user", "content": "Reply with the single word: ping"}]
    )
    assert type(reply) is dict  # plain dict — SDK objects must not leak
    assert reply["role"] == "assistant"
    assert isinstance(reply["content"], str) and reply["content"]


@pytest.mark.parametrize("client", available_clients())
def test_complete_does_not_mutate_the_input(client):
    messages = [{"role": "user", "content": "Reply with the single word: ping"}]
    snapshot = [dict(m) for m in messages]
    client.complete(messages)
    assert messages == snapshot
