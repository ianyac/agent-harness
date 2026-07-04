from harness.loop import run_turn
from tests.fake_llm import FakeLLM


def test_fake_llm_returns_scripted_responses_in_order():
    llm = FakeLLM([{"type": "text", "content": "first"}, {"type": "text", "content": "second"}])
    assert llm.complete([{"role": "user", "content": "hi"}]) == {
        "role": "assistant",
        "content": "first",
    }
    assert llm.complete([{"role": "user", "content": "again"}]) == {
        "role": "assistant",
        "content": "second",
    }


def test_fake_llm_records_what_it_was_shown():
    llm = FakeLLM([{"type": "text", "content": "ok"}])
    llm.complete([{"role": "user", "content": "hi"}])
    assert llm.turns[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_run_turn_appends_user_and_assistant_messages():
    llm = FakeLLM([{"type": "text", "content": "hello there"}])
    messages = []
    reply = run_turn(messages, "hi", llm)
    assert messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
    ]
    assert reply == {"role": "assistant", "content": "hello there"}


def test_model_sees_full_history_each_turn():
    llm = FakeLLM([{"type": "text", "content": "a"}, {"type": "text", "content": "b"}])
    messages = []
    run_turn(messages, "one", llm)
    run_turn(messages, "two", llm)
    assert llm.turns[1]["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "two"},
    ]
