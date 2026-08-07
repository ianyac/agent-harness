import json

import pytest

from harness.folding import FoldConfig, FoldingContext, ProjectionError
from tests.helpers import noop_tool


def tool_exchange(
    name: str,
    arguments: dict,
    result: str,
    *,
    call_id: str = "call_0",
) -> list[dict]:
    return [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def test_sync_assigns_stable_result_span_and_projection_labels_it(tmp_path):
    # Regression caught: re-syncing a transcript must not allocate new IDs,
    # because every existing fold record points at the original handle.
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0),
    )
    messages = tool_exchange("read_file", {"path": "a.py"}, "print('a')")
    context.sync(messages, {"read_file": noop_tool(name="read_file")})

    projected = context.project(messages)

    assert projected[2]["content"].startswith("[m2.r0 · ~")
    assert projected[2]["content"].endswith("print('a')")
    assert context.span_ids() == ["m0", "m1", "m2.r0"]
    context.sync(messages, {"read_file": noop_tool(name="read_file")})
    assert context.span_ids() == ["m0", "m1", "m2.r0"]


def test_large_result_is_chunked_once_with_stable_child_ids(tmp_path):
    # Regression caught: re-chunking on projection would make an existing
    # chunk handle name different content after a tokenizer/config change.
    context = FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0, chunk_tokens=4),
    )
    messages = tool_exchange(
        "dump",
        {},
        "alpha beta\ngamma delta\nepsilon zeta\neta theta",
    )
    context.sync(messages, {"dump": noop_tool(name="dump")})

    first_ids = context.child_ids("m2.r0")
    first_projection = context.project(messages)
    context.sync(messages, {"dump": noop_tool(name="dump")})

    assert first_ids == ["m2.r0.c0", "m2.r0.c1", "m2.r0.c2", "m2.r0.c3"]
    assert context.child_ids("m2.r0") == first_ids
    for span_id in first_ids:
        assert f"[{span_id} · ~" in first_projection[2]["content"]


def test_projection_rejects_an_orphaned_tool_result(tmp_path):
    # Regression caught: sending this shape would produce a provider 400 and
    # poison every retry, so the local projection must fail before dispatch.
    context = FoldingContext(tmp_path / "folds.sqlite3", "session")
    messages = [
        {"role": "user", "content": "start"},
        {"role": "tool", "tool_call_id": "missing", "content": "orphan"},
    ]
    context.sync(messages)

    with pytest.raises(ProjectionError, match="orphaned tool result.*missing"):
        context.project(messages)


def test_projection_rejects_a_call_without_exactly_one_result(tmp_path):
    # Regression caught: folding or projection code must not silently drop a
    # result while leaving the assistant's call in the provider transcript.
    context = FoldingContext(tmp_path / "folds.sqlite3", "session")
    messages = tool_exchange("noop", {}, "ok")[:-1]
    context.sync(messages)

    with pytest.raises(ProjectionError, match="has no result"):
        context.project(messages)


def test_resume_replays_the_same_projection_and_hash(tmp_path):
    # Regression caught: persisted metadata must fully determine what the
    # resumed model sees; process-local counters cannot affect projection.
    path = tmp_path / "folds.sqlite3"
    messages = tool_exchange("read_file", {"path": "a.py"}, "body")
    first = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    first.sync(messages, {"read_file": noop_tool(name="read_file")})
    expected_projection = first.project(messages)
    expected_hash = first.projection_hash(messages)
    first.close()

    resumed = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    resumed.sync(messages, {"read_file": noop_tool(name="read_file")})

    assert resumed.project(messages) == expected_projection
    assert resumed.projection_hash(messages) == expected_hash


def test_reconstruct_uses_the_persisted_shadow_ledger(tmp_path):
    # Regression caught: replay must not require the caller to retain an
    # in-memory transcript after a process exits.
    path = tmp_path / "folds.sqlite3"
    messages = tool_exchange("noop", {}, "durable result")
    context = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    context.sync(messages, {"noop": noop_tool()})
    live_projection = context.project(messages)
    context.close()

    resumed = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))

    assert resumed.reconstruct() == live_projection
