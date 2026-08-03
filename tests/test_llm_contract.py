import os

import pytest

from harness import llm as llm_module
from harness.llm import CONTEXT_WINDOWS, make_llm
from tests.fake_llm import FakeLLM


@pytest.fixture
def clean_llm_cache():
    # make_llm memoizes per slug in a module-level cache; a test that injects a
    # fake builder must not leak object() clients into it for later tests
    saved = dict(llm_module._LLM_CACHE)
    llm_module._LLM_CACHE.clear()
    try:
        yield
    finally:
        llm_module._LLM_CACHE.clear()
        llm_module._LLM_CACHE.update(saved)


def test_context_windows_has_the_api_models():
    for slug in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"):
        assert CONTEXT_WINDOWS[slug] == 272_000


def test_make_llm_defaults_to_gpt55_and_caches(clean_llm_cache):
    built = []

    def fake_build(slug):
        built.append(slug)
        return object()

    a = make_llm(build=fake_build)          # default slug
    b = make_llm("gpt-5.4", build=fake_build)
    a2 = make_llm(build=fake_build)          # cached — no second build
    assert built == ["gpt-5.5", "gpt-5.4"]
    assert a is a2 and a is not b


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
