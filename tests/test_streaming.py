from harness.loop import run_turn
from harness.tools.agent import agent_tool
from harness.tools.base import Tool
from tests.fake_llm import FakeLLM


def noop_tool() -> Tool:
    return Tool(
        name="noop",
        description="A tool that does nothing, for tests.",
        parameters={"type": "object", "properties": {}},
        execute=lambda **args: "ok",
    )


def test_chunks_join_to_exactly_the_returned_content():
    llm = FakeLLM([{"type": "text", "content": "streaming is fun"}])
    chunks = []
    reply = llm.complete([], on_chunk=chunks.append)
    # the box is still the truth; the texts spell the same words
    assert reply["content"] == "streaming is fun"
    assert "".join(chunks) == "streaming is fun"
    assert len(chunks) > 1  # actually chunked, not one blob


def test_no_on_chunk_is_the_default_and_streams_nothing():
    llm = FakeLLM([{"type": "text", "content": "quiet"}])
    assert llm.complete([])["content"] == "quiet"


def test_run_turn_forwards_on_chunk_through_tool_iterations():
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    chunks = []
    run_turn([], "go", llm, tools={"noop": noop_tool()}, on_chunk=chunks.append)
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
        on_chunk=chunks.append,
    )
    # compaction is internal bookkeeping — the human never watches it
    assert "".join(chunks) == "done"


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
    run_turn([], "go", llm, tools=tools, on_chunk=chunks.append)
    # the sub's words are internal; only the parent's reply is watched
    assert "".join(chunks) == "parent answer"
