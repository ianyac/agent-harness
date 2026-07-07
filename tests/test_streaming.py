from harness.loop import run_turn
from harness.tools.agent import agent_tool
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool


def test_chunks_join_to_exactly_the_returned_content():
    llm = FakeLLM([{"type": "text", "content": "streaming is fun"}])
    chunks = []
    reply = llm.complete([], on_text_delta=chunks.append)
    # the box is still the truth; the texts spell the same words
    assert reply["content"] == "streaming is fun"
    assert "".join(chunks) == "streaming is fun"
    assert len(chunks) > 1  # actually chunked, not one blob


def test_no_on_text_delta_is_the_default_and_streams_nothing():
    llm = FakeLLM([{"type": "text", "content": "quiet"}])
    assert llm.complete([])["content"] == "quiet"


def test_run_turn_forwards_on_text_delta_through_tool_iterations():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    chunks = []
    run_turn([], "go", llm, tools={"noop": noop_tool()}, on_text_delta=chunks.append)
    # the reply after the tool round streamed: the callback survived the loop
    assert "".join(chunks) == "done"


def test_summarizer_output_never_streams():
    messages = []
    for i in range(6):
        messages.append({"role": "user", "content": f"q{i} " + "detail " * 30})
        messages.append({"role": "assistant", "content": f"a{i} " + "detail " * 30})
    llm = FakeLLM(
        [
            {"type": "text", "content": "SUMMARY"},
            {"type": "text", "content": "done"},
        ]
    )
    chunks = []
    run_turn(
        messages,
        "next",
        llm,
        compact_threshold=50,
        keep_recent=2,
        on_text_delta=chunks.append,
    )
    # compaction is internal bookkeeping — the human never watches it
    assert "".join(chunks) == "done"


def test_narration_before_a_tool_call_streams_too():
    # the Responses API can put text AND function calls in one reply; that
    # narration streams like any text, so consumers must expect chunks
    # from more than just the final reply
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "content": "let me check. ",
                "calls": [{"name": "noop", "arguments": {}}],
            },
            {"type": "text", "content": "done"},
        ]
    )
    chunks = []
    reply = run_turn(
        [], "go", llm, tools={"noop": noop_tool()}, on_text_delta=chunks.append
    )
    assert "".join(chunks) == "let me check. done"
    assert reply["content"] == "done"


def test_subagent_output_never_streams():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "agent", "arguments": {"task": "x"}}]},
            {"type": "text", "content": "sub answer"},
            {"type": "text", "content": "parent answer"},
        ]
    )
    tools = {}
    tools["agent"] = agent_tool(llm, tools=tools, policy=None)
    chunks = []
    run_turn([], "go", llm, tools=tools, on_text_delta=chunks.append)
    # the sub's words are internal; only the parent's reply is watched
    assert "".join(chunks) == "parent answer"
