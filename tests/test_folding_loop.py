import hashlib
import json
from copy import deepcopy

import pytest

from harness.folding import FoldConfig, FoldError, FoldingContext, ProjectionError
from harness.loop import run_turn
from harness.tools.base import Tool
from harness.tools.folding import fold_tool
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool
from tests.test_folding import rich_note, tool_exchange


def completed_history(result: str = "full evidence") -> list[dict]:
    return tool_exchange("read_file", {"path": "a.py"}, result) + [
        {"role": "assistant", "content": "read complete"}
    ]


def context_for(tmp_path, **config) -> FoldingContext:
    return FoldingContext(
        tmp_path / "folds.sqlite3",
        "session",
        config=FoldConfig(min_span_tokens=0, **config),
    )


def test_loop_sends_projection_but_retains_full_shadow_history(tmp_path):
    # Regression caught: projection markers must never overwrite the caller's
    # durable transcript, or unfold/replay loses the original evidence.
    messages = completed_history()
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": noop_tool(name="read_file")})
    context.fold("m2.r0", "finished", rich_note())
    context.checkpoint()
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, context=context)

    assert llm.turns[0]["messages"][2]["content"].startswith("[folded m2.r0")
    assert messages[2]["content"] == "full evidence"


def test_each_model_dispatch_records_the_exact_projection_hash(tmp_path):
    messages = completed_history()
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": noop_tool(name="read_file")})
    context.fold("m2.r0", "finished", rich_note())
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, context=context)

    exact = json.dumps(
        llm.turns[0]["messages"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    record = context.projection_chain()[-1]
    assert record["kind"] == "request"
    assert record["projection_hash"] == hashlib.sha256(exact.encode()).hexdigest()
    assert llm.turns[0]["projection_hash"] == record["projection_hash"]
    assert "workspace after checkpoint" in exact


def test_each_model_request_can_be_reconstructed_after_reopen(tmp_path):
    # Regression caught: inferring a request from created_turn loses distinct
    # model boundaries when a tool turn makes more than one request.
    path = tmp_path / "folds.sqlite3"
    context = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    tool = noop_tool(name="noop")
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "noop", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    messages: list[dict] = []

    run_turn(messages, "inspect", llm, tools={"noop": tool}, context=context)

    expected = deepcopy([turn["messages"] for turn in llm.turns])
    request_ids = [
        row["projection_id"]
        for row in context.projection_chain()
        if row["kind"] == "request"
    ]
    context.close()

    resumed = FoldingContext(path, "session", config=FoldConfig(min_span_tokens=0))
    assert [resumed.reconstruct_projection(row_id) for row_id in request_ids] == expected


def test_loop_applies_pending_folds_at_the_next_turn_boundary(tmp_path):
    # Regression caught: a low-volume fold can wait within its current phase,
    # but it must not remain unapplied forever once a new user turn begins.
    messages = completed_history()
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": noop_tool(name="read_file")})
    context.fold("m2.r0", "finished", rich_note())
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, context=context)

    assert llm.turns[0]["messages"][2]["content"].startswith("[folded m2.r0")


def test_loop_refuses_folding_and_compaction_before_mutating_history(tmp_path):
    # Regression caught: two independent context managers produce a projection
    # neither ledger can reconstruct.
    messages = []
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_turn(
            messages,
            "hello",
            FakeLLM([]),
            context=context_for(tmp_path),
            compact_threshold=10,
        )
    assert messages == []


def test_loop_labels_new_tool_results_before_the_next_model_call(tmp_path):
    # Regression caught: IDs are copy handles for the agent; assigning them only
    # at session end makes fold calls impossible during the active turn.
    dump = Tool(
        name="dump",
        description="return evidence",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "fresh evidence",
    )
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {"type": "text", "content": "done"},
        ]
    )
    messages = []

    run_turn(messages, "inspect", llm, tools={"dump": dump}, context=context_for(tmp_path))

    assert llm.turns[1]["messages"][2]["content"].startswith("[m2.r0 · ~")
    assert messages[2]["content"] == "fresh evidence"


def test_marked_share_crossing_threshold_rebuilds_mid_turn(tmp_path):
    # Regression caught: waiting until the next user turn after a very large fold
    # wastes every model call in the remainder of the current phase.
    dump = Tool(
        name="dump",
        description="return evidence",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "evidence " * 2_000,
    )
    context = context_for(tmp_path, checkpoint_ratio=0.01)
    tools = {"dump": dump, "fold": fold_tool(context)}
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "fold",
                        "arguments": {
                            "span_id": "m2.r0",
                            "reason": "finished",
                            "note": rich_note(),
                        },
                    }
                ],
            },
            {"type": "text", "content": "done"},
        ]
    )

    run_turn([], "inspect", llm, tools=tools, context=context)

    assert llm.turns[2]["messages"][2]["content"].startswith("[folded m2.r0")


def test_small_mark_stays_visible_mid_turn_then_folds_on_next_turn(tmp_path):
    context = context_for(tmp_path, checkpoint_ratio=1.0)
    dump = Tool(
        name="dump",
        description="return evidence",
        parameters={"type": "object", "properties": {}},
        execute=lambda: "small evidence",
    )
    tools = {"dump": dump, "fold": fold_tool(context)}
    first_llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "dump", "arguments": {}}]},
            {
                "type": "tool_calls",
                "calls": [
                    {
                        "name": "fold",
                        "arguments": {
                            "span_id": "m2.r0",
                            "reason": "finished",
                            "note": rich_note(),
                        },
                    }
                ],
            },
            {"type": "text", "content": "phase done"},
        ]
    )
    messages: list[dict] = []

    run_turn(messages, "inspect", first_llm, tools=tools, context=context)
    assert "small evidence" in first_llm.turns[2]["messages"][2]["content"]

    second_llm = FakeLLM([{"type": "text", "content": "done"}])
    run_turn(messages, "next phase", second_llm, tools=tools, context=context)
    assert second_llm.turns[0]["messages"][2]["content"].startswith("[folded m2.r0")


def test_following_turn_receives_auto_fold_notice_without_polluting_shadow(tmp_path):
    context = context_for(tmp_path)
    messages = completed_history("same")
    messages.extend(
        [
            {"role": "user", "content": "read again"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "a.py"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "same"},
            {"role": "assistant", "content": "read complete"},
        ]
    )
    tools = {"read_file": noop_tool(name="read_file")}
    context.sync(messages, tools)
    llm = FakeLLM([{"type": "text", "content": "done"}])

    run_turn(messages, "next", llm, tools=tools, context=context)

    projected_user = next(
        message for message in reversed(llm.turns[0]["messages"])
        if message["role"] == "user" and "next" in message["content"]
    )
    assert projected_user["content"].startswith("[auto-folded m6.r0")
    assert messages[-2] == {"role": "user", "content": "next"}


def test_scanner_redaction_reaches_the_model_and_shadow_immediately(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    leak = Tool(
        name="leak",
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: secret,
    )
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "leak", "arguments": {}}]},
            {"type": "text", "content": "handled"},
        ]
    )
    messages: list[dict] = []

    run_turn(messages, "inspect", llm, tools={"leak": leak}, context=context_for(tmp_path))

    assert secret not in json.dumps(llm.turns[1]["messages"])
    assert secret not in json.dumps(messages)
    assert messages[2]["content"] == "[redacted — credential detected in tool output]"


def test_scanner_remaps_tool_definitions_and_resolves_the_alias_for_execution(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    executions: list[str] = []

    def leak() -> str:
        executions.append("ran")
        return f"diagnostic {secret}"

    credential_tool = Tool(
        name=secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=leak,
    )
    ordinary_tool = noop_tool(name="ordinary")
    tools = {secret: credential_tool, "ordinary": ordinary_tool}
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": secret, "arguments": {}}]},
            {"type": "tool_calls", "calls": [{"name": alias, "arguments": {}}]},
            {"type": "text", "content": "handled"},
        ]
    )
    context = context_for(tmp_path)
    messages: list[dict] = []

    run_turn(messages, "inspect", llm, tools=tools, context=context)

    second_boundary = llm.turns[1]
    offered_names = [item["function"]["name"] for item in second_boundary["tools"]]
    historical_name = second_boundary["messages"][1]["tool_calls"][0]["function"][
        "name"
    ]
    assert offered_names == [alias, "ordinary"]
    assert historical_name == alias
    assert secret not in json.dumps(second_boundary)
    assert executions == ["ran", "ran"]
    assert messages[3]["tool_calls"][0]["function"]["name"] == alias
    assert messages[3]["tool_calls"][0]["id"] == messages[4]["tool_call_id"]
    assert secret not in json.dumps(llm.turns[2])
    assert llm.turns[2]["tools"][0]["function"]["name"] == alias
    assert [
        message["tool_calls"][0]["function"]["name"]
        for message in llm.turns[2]["messages"]
        if message["role"] == "assistant" and message.get("tool_calls")
    ] == [alias, alias]
    stored_names = context._db.execute(
        "SELECT tool_name FROM tool_calls ORDER BY tool_call_id"
    ).fetchall()
    assert [row["tool_name"] for row in stored_names] == [alias, alias]
    assert all(
        secret not in json.dumps(context.reconstruct_projection(row["projection_id"]))
        for row in context.projection_chain()
        if row["kind"] == "request"
    )
    assert list(tools) == [secret, "ordinary"]
    assert tools[secret] is credential_tool
    assert credential_tool.name == secret


def test_scanner_avoids_aliases_occupied_by_registered_tools(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    occupied_alias = (
        "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    )
    remapped_name = (
        "redacted_q3c4jdidtkzxytqnv7vgwvilzalkuejx7djs7t6umey5q4kyvdrq"
    )
    executions: list[str] = []

    def leak() -> str:
        executions.append("credential")
        return f"diagnostic {secret}"

    def ordinary() -> str:
        executions.append("ordinary")
        return "ordinary result"

    credential_tool = Tool(
        name=secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=leak,
    )
    ordinary_tool = Tool(
        name=occupied_alias,
        description="ordinary tool whose name happens to occupy an alias",
        parameters={"type": "object", "properties": {}},
        execute=ordinary,
    )
    tools = {secret: credential_tool, occupied_alias: ordinary_tool}
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": secret, "arguments": {}}]},
            {
                "type": "tool_calls",
                "calls": [{"name": remapped_name, "arguments": {}}],
            },
            {"type": "text", "content": "handled"},
        ]
    )

    run_turn([], "inspect", llm, tools=tools, context=context_for(tmp_path))

    offered_names = [
        definition["function"]["name"] for definition in llm.turns[1]["tools"]
    ]
    assert offered_names == [remapped_name, occupied_alias]
    assert len(set(offered_names)) == 2
    assert executions == ["credential", "credential"]
    assert secret not in json.dumps(llm.turns[1])


def test_scanner_restores_tool_name_aliases_after_reopen(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    tool = Tool(
        name=secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {secret}",
    )
    tools = {secret: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    first_llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": secret, "arguments": {}}]},
            {"type": "text", "content": "first done"},
        ]
    )
    run_turn(messages, "inspect", first_llm, tools=tools, context=first)
    first.close()

    resumed = context_for(tmp_path)
    second_llm = FakeLLM([{"type": "text", "content": "second done"}])
    run_turn(messages, "inspect again", second_llm, tools=tools, context=resumed)

    boundary = second_llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == alias
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == alias
    assert secret not in json.dumps(boundary)
    assert all(
        secret not in json.dumps(resumed.reconstruct_projection(row["projection_id"]))
        for row in resumed.projection_chain()
        if row["kind"] == "request"
    )
    assert list(tools) == [secret]
    assert tool.name == secret


def test_scanner_recovers_a_legacy_v3_alias_without_mapping_metadata(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    tool = Tool(
        name=secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {secret}",
    )
    tools = {secret: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": secret, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools=tools,
        context=first,
    )
    for row in first._db.execute("SELECT span_id, meta_json FROM entries").fetchall():
        metadata = json.loads(row["meta_json"])
        metadata.pop("scanner_aliases", None)
        first._db.execute(
            "UPDATE entries SET meta_json = ? WHERE span_id = ?",
            (json.dumps(metadata), row["span_id"]),
        )
    first._db.execute("DROP TABLE IF EXISTS scanner_aliases")
    first._db.commit()
    first.close()

    resumed = context_for(tmp_path)
    second_llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(messages, "inspect again", second_llm, tools=tools, context=resumed)

    boundary = second_llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == alias
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == alias
    assert secret not in json.dumps(boundary)
    assert all(
        secret not in json.dumps(resumed.reconstruct_projection(row["projection_id"]))
        for row in resumed.projection_chain()
        if row["kind"] == "request"
    )


def test_scanner_recovers_all_aliases_in_a_legacy_multi_secret_tool_name(tmp_path):
    first_secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    second_secret = "sk-987654321zyxwvutsrqponmlkjihgfedcba"
    raw_name = f"inspect:{first_secret}:{second_secret}"
    first_alias = FoldingContext._identifier_alias(first_secret)
    second_alias = FoldingContext._identifier_alias(second_secret)
    historical_name = f"inspect:{first_alias}:{second_alias}"
    tool = Tool(
        name=raw_name,
        description="returns credentials",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {first_secret} and {second_secret}",
    )
    tools = {raw_name: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": raw_name, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools=tools,
        context=first,
    )
    for row in first._db.execute("SELECT span_id, meta_json FROM entries").fetchall():
        metadata = json.loads(row["meta_json"])
        metadata.pop("scanner_aliases", None)
        first._db.execute(
            "UPDATE entries SET meta_json = ? WHERE span_id = ?",
            (json.dumps(metadata), row["span_id"]),
        )
    first._db.commit()
    first.close()

    resumed = context_for(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(messages, "inspect again", llm, tools=tools, context=resumed)

    boundary = llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == historical_name
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == (
        historical_name
    )
    assert first_secret not in json.dumps(boundary)
    assert second_secret not in json.dumps(boundary)


def test_scanner_recovers_a_legacy_alias_after_an_identifier_underscore(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = FoldingContext._identifier_alias(secret)
    raw_name = f"x_{secret}"
    historical_name = f"x_{alias}"
    tool = Tool(
        name=raw_name,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {secret}",
    )
    tools = {raw_name: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": raw_name, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools=tools,
        context=first,
    )
    for row in first._db.execute("SELECT span_id, meta_json FROM entries").fetchall():
        metadata = json.loads(row["meta_json"])
        metadata.pop("scanner_aliases", None)
        first._db.execute(
            "UPDATE entries SET meta_json = ? WHERE span_id = ?",
            (json.dumps(metadata), row["span_id"]),
        )
    first._db.commit()
    first.close()

    resumed = context_for(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(messages, "inspect again", llm, tools=tools, context=resumed)

    boundary = llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == historical_name
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == (
        historical_name
    )
    assert secret not in json.dumps(boundary)


def test_scanner_recovers_a_legacy_alias_for_overlapping_secret_patterns(tmp_path):
    inner_secret = "ghp_" + "a" * 30
    outer_secret = f"sk-{inner_secret}"
    alias = FoldingContext._identifier_alias(outer_secret)
    tool = Tool(
        name=outer_secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {outer_secret}",
    )
    tools = {outer_secret: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": outer_secret, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools=tools,
        context=first,
    )
    for row in first._db.execute("SELECT span_id, meta_json FROM entries").fetchall():
        metadata = json.loads(row["meta_json"])
        metadata.pop("scanner_aliases", None)
        first._db.execute(
            "UPDATE entries SET meta_json = ? WHERE span_id = ?",
            (json.dumps(metadata), row["span_id"]),
        )
    first._db.commit()
    first.close()

    resumed = context_for(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(messages, "inspect again", llm, tools=tools, context=resumed)

    boundary = llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == alias
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == alias
    assert outer_secret not in json.dumps(boundary)
    assert inner_secret not in json.dumps(boundary)


def test_legacy_recovery_reserves_an_embedded_alias_against_takeover(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = FoldingContext._identifier_alias(secret)
    raw_name = f"inspect:{secret}"
    tool = Tool(
        name=raw_name,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {secret}",
    )
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": raw_name, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={raw_name: tool},
        context=first,
    )
    for row in first._db.execute("SELECT span_id, meta_json FROM entries").fetchall():
        metadata = json.loads(row["meta_json"])
        metadata.pop("scanner_aliases", None)
        first._db.execute(
            "UPDATE entries SET meta_json = ? WHERE span_id = ?",
            (json.dumps(metadata), row["span_id"]),
        )
    first._db.commit()
    first.close()
    before = deepcopy(messages)
    executions: list[str] = []
    takeover = Tool(
        name=alias,
        description="unrelated tool",
        parameters={"type": "object", "properties": {}},
        execute=lambda: executions.append("ran") or "ordinary result",
    )
    resumed = context_for(tmp_path)

    with pytest.raises(FoldError, match="reserved scanner alias"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools={raw_name: tool, alias: takeover},
            context=resumed,
        )

    assert messages == before
    assert executions == []


def test_scanner_rejects_a_late_tool_that_occupies_a_persisted_alias(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    credential_tool = Tool(
        name=secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {secret}",
    )
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": secret, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={secret: credential_tool},
        context=first,
    )
    first.close()
    before = deepcopy(messages)
    ordinary_tool = noop_tool(name=alias)
    tools = {secret: credential_tool, alias: ordinary_tool}
    resumed = context_for(tmp_path)

    with pytest.raises(FoldError, match="scanner tool alias conflicts"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools=tools,
            context=resumed,
        )

    assert messages == before
    assert list(tools) == [secret, alias]
    assert tools[secret] is credential_tool
    assert tools[alias] is ordinary_tool


def test_scanner_rejects_registry_takeover_of_a_persisted_alias(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456789"
    alias = "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    credential_tool = Tool(
        name=secret,
        description="returns a credential",
        parameters={"type": "object", "properties": {}},
        execute=lambda: f"diagnostic {secret}",
    )
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": secret, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={secret: credential_tool},
        context=first,
    )
    first.close()
    before = deepcopy(messages)
    executions: list[str] = []
    takeover = Tool(
        name=alias,
        description="unrelated tool",
        parameters={"type": "object", "properties": {}},
        execute=lambda: executions.append("ran") or "ordinary result",
    )
    resumed = context_for(tmp_path)

    with pytest.raises(FoldError, match="reserved scanner alias"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools={alias: takeover},
            context=resumed,
        )

    assert messages == before
    assert executions == []
    assert takeover.name == alias
