from main import FakeLLM, Message

def test_fake_llm_return_scripted_responses_in_order():
    llm = FakeLLM(["first", "second"])
    assert llm.complete([Message("user", "hi")]) == Message("assistant", "first")
    assert llm.complete([Message("user", "again")]) == Message("assistant", "second")
    
def test_fake_llm_records_what_it_was_shown():
    llm = FakeLLM(["ok"])
    llm.complete([Message("user", "hi")])
    assert llm.calls == [[Message("user", "hi")]]