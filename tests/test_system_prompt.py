from harness.loop import run_turn
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool


def test_run_turn_forwards_system_to_every_model_call():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    run_turn([], "go", llm, tools={"noop": noop_tool()}, system="SYSTEM-XYZ")
    # the system prompt must ride on iteration 1 AND the post-tool iteration 2
    assert llm.turns[0]["system"] == "SYSTEM-XYZ"
    assert llm.turns[1]["system"] == "SYSTEM-XYZ"


def test_run_turn_defaults_system_to_none():
    llm = FakeLLM([{"type": "text", "content": "hi"}])
    run_turn([], "go", llm)
    assert llm.turns[0]["system"] is None
