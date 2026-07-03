from tests.fake_llm import FakeLLM
from main import run_turn


def test_fake_llm_returns_scripted_responses_in_order():
    llm = FakeLLM(["first", "second"])
    assert llm.complete([{"role": "user", "content": "hi"}]) == {
        "role": "assistant",
        "content": "first",
    }
    assert llm.complete([{"role": "user", "content": "again"}]) == {
        "role": "assistant",
        "content": "second",
    }


def test_fake_llm_records_what_it_was_shown():
    llm = FakeLLM(["ok"])
    llm.complete([{"role": "user", "content": "hi"}])
    assert llm.calls == [[{"role": "user", "content": "hi"}]]


def test_run_turn_appends_user_and_assistant_messages():
    llm = FakeLLM(["hello there"])
    messages = []
    reply = run_turn(messages, "hi", llm)
    assert messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
    ]
    assert reply == {"role": "assistant", "content": "hello there"}


def test_model_sees_full_history_each_turn():
    llm = FakeLLM(["a", "b"])
    messages = []
    run_turn(messages, "one", llm)
    run_turn(messages, "two", llm)
    assert llm.calls[1] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "two"},
    ]
