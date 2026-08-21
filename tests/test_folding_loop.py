import hashlib
import json
from copy import deepcopy

import pytest

from harness.folding import FoldError, FoldingContext
from harness.loop import run_turn
from harness.tools.folding import fold_tool
from tests.fake_llm import FakeLLM
from tests.helpers import canonical, canonical_hash, noop_tool, simple_tool
from tests.test_folding import ALIAS, SECRET, context_for, rich_note, tool_exchange

SECOND_SECRET = "sk-987654321zyxwvutsrqponmlkjihgfedcba"
SECOND_ALIAS = FoldingContext._identifier_alias(SECOND_SECRET)


def completed_history(result: str = "full evidence") -> list[dict]:
    return tool_exchange("read_file", {"path": "a.py"}, result) + [
        {"role": "assistant", "content": "read complete"}
    ]


def strip_legacy_alias_metadata(
    context: FoldingContext, key: str = "scanner_aliases"
) -> None:
    """Rewrite every entry as an older ledger left it: without the persisted
    scanner alias map (or, for ``scanner_alias_lengths``, the alias lengths)."""
    for row in context._db.execute("SELECT span_id, meta_json FROM entries").fetchall():
        metadata = json.loads(row["meta_json"])
        metadata.pop(key, None)
        context._db.execute(
            "UPDATE entries SET meta_json = ? WHERE span_id = ?",
            (json.dumps(metadata), row["span_id"]),
        )
    context._db.commit()


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

    exact = canonical(llm.turns[0]["messages"])
    record = context.projection_chain()[-1]
    assert record["kind"] == "request"
    assert record["projection_hash"] == canonical_hash(llm.turns[0]["messages"])
    assert llm.turns[0]["projection_hash"] == record["projection_hash"]
    assert "workspace after checkpoint" in exact


def test_each_model_request_can_be_reconstructed_after_reopen(tmp_path):
    # Regression caught: inferring a request from created_turn loses distinct
    # model boundaries when a tool turn makes more than one request.
    context = context_for(tmp_path)
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

    resumed = context_for(tmp_path)
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
    dump = simple_tool("dump", lambda: "fresh evidence", description="return evidence")
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
    dump = simple_tool(
        "dump",
        lambda: "evidence " * 2_000,
        description="return evidence",
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
    dump = simple_tool("dump", lambda: "small evidence", description="return evidence")
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
        tool_exchange("read_file", {"path": "a.py"}, "same", call_id="call_1", user="read again")
        + [{"role": "assistant", "content": "read complete"}]
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
    leak = simple_tool("leak", lambda: SECRET, description="returns a credential")
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": "leak", "arguments": {}}]},
            {"type": "text", "content": "handled"},
        ]
    )
    messages: list[dict] = []

    run_turn(messages, "inspect", llm, tools={"leak": leak}, context=context_for(tmp_path))

    assert SECRET not in json.dumps(llm.turns[1]["messages"])
    assert SECRET not in json.dumps(messages)
    assert messages[2]["content"] == "[redacted — credential detected in tool output]"


def test_scanner_remaps_tool_definitions_and_resolves_the_alias_for_execution(tmp_path):
    # pinned literally: the alias format is part of the persisted ledger contract
    alias = "redacted_db3qobsommarzjeek2fytgek5zgie47kkosagadvfuywhxw7mraa"
    executions: list[str] = []

    def leak() -> str:
        executions.append("ran")
        return f"diagnostic {SECRET}"

    credential_tool = simple_tool(SECRET, leak, description="returns a credential")
    ordinary_tool = noop_tool(name="ordinary")
    tools = {SECRET: credential_tool, "ordinary": ordinary_tool}
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": SECRET, "arguments": {}}]},
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
    assert SECRET not in json.dumps(second_boundary)
    assert executions == ["ran", "ran"]
    assert messages[3]["tool_calls"][0]["function"]["name"] == alias
    assert messages[3]["tool_calls"][0]["id"] == messages[4]["tool_call_id"]
    assert SECRET not in json.dumps(llm.turns[2])
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
        SECRET not in json.dumps(context.reconstruct_projection(row["projection_id"]))
        for row in context.projection_chain()
        if row["kind"] == "request"
    )
    assert list(tools) == [SECRET, "ordinary"]
    assert tools[SECRET] is credential_tool
    assert credential_tool.name == SECRET


def test_scanner_avoids_aliases_occupied_by_registered_tools(tmp_path):
    remapped_name = (
        "redacted_q3c4jdidtkzxytqnv7vgwvilzalkuejx7djs7t6umey5q4kyvdrq"
    )
    executions: list[str] = []

    def leak() -> str:
        executions.append("credential")
        return f"diagnostic {SECRET}"

    def ordinary() -> str:
        executions.append("ordinary")
        return "ordinary result"

    credential_tool = simple_tool(SECRET, leak, description="returns a credential")
    ordinary_tool = simple_tool(
        ALIAS,
        ordinary,
        description="ordinary tool whose name happens to occupy an alias",
    )
    tools = {SECRET: credential_tool, ALIAS: ordinary_tool}
    llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": SECRET, "arguments": {}}]},
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
    assert offered_names == [remapped_name, ALIAS]
    assert len(set(offered_names)) == 2
    assert executions == ["credential", "credential"]
    assert SECRET not in json.dumps(llm.turns[1])


def test_scanner_restores_tool_name_aliases_after_reopen(tmp_path):
    tool = simple_tool(
        SECRET,
        lambda: f"diagnostic {SECRET}",
        description="returns a credential",
    )
    tools = {SECRET: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    first_llm = FakeLLM(
        [
            {"type": "tool_calls", "calls": [{"name": SECRET, "arguments": {}}]},
            {"type": "text", "content": "first done"},
        ]
    )
    run_turn(messages, "inspect", first_llm, tools=tools, context=first)
    first.close()

    resumed = context_for(tmp_path)
    second_llm = FakeLLM([{"type": "text", "content": "second done"}])
    run_turn(messages, "inspect again", second_llm, tools=tools, context=resumed)

    boundary = second_llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == ALIAS
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == ALIAS
    assert SECRET not in json.dumps(boundary)
    assert all(
        SECRET not in json.dumps(resumed.reconstruct_projection(row["projection_id"]))
        for row in resumed.projection_chain()
        if row["kind"] == "request"
    )
    assert list(tools) == [SECRET]
    assert tool.name == SECRET


@pytest.mark.parametrize(
    ("secret", "drop_length_metadata"),
    [("abcdefghijklmnopqrstuvwxyz123456", True), ("a" * 257, False)],
)
def test_scanner_recovers_a_generic_secret_from_its_persisted_hash(
    tmp_path,
    secret,
    drop_length_metadata,
):
    raw_name = f"x_{secret}"
    alias = FoldingContext._identifier_alias(secret)
    historical_name = f"x_{alias}"
    executions: list[str] = []

    def leak() -> str:
        executions.append("ran")
        return f"password={secret}"

    tool = simple_tool(raw_name, leak, description="returns a credential")
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
    if drop_length_metadata:
        strip_legacy_alias_metadata(first, "scanner_alias_lengths")
    first.close()

    resumed = context_for(tmp_path)
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [{"name": historical_name, "arguments": {}}],
            },
            {"type": "text", "content": "continued"},
        ]
    )
    llm._call_counter = 1
    run_turn(messages, "inspect again", llm, tools=tools, context=resumed)

    boundary = llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == historical_name
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == (
        historical_name
    )
    assert secret not in json.dumps(boundary)
    assert executions == ["ran", "ran"]


@pytest.mark.parametrize("reverse_registry", [False, True])
def test_legacy_recovery_resolves_every_tool_before_applying_registry_order(
    tmp_path,
    reverse_registry,
):
    first_raw = f"first_{SECRET}"
    second_raw = f"second_{SECOND_SECRET}"
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"name": first_raw, "arguments": {}},
                        {"name": second_raw, "arguments": {}},
                    ],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={
            first_raw: simple_tool(
                first_raw,
                lambda: f"diagnostic {SECRET}",
                description="returns the first credential",
            ),
            second_raw: simple_tool(
                second_raw,
                lambda: f"diagnostic {SECOND_SECRET}",
                description="returns the second credential",
            ),
        },
        context=first,
    )
    strip_legacy_alias_metadata(first)
    first.close()

    reopened_specs = [
        (
            first_raw,
            simple_tool(
                first_raw,
                lambda: "ordinary result",
                description="first credential after reopen",
            ),
            f"first_{ALIAS}",
        ),
        (
            f"renamed_{SECOND_SECRET}",
            simple_tool(
                f"renamed_{SECOND_SECRET}",
                lambda: "ordinary result",
                description="second credential after wrapper rename",
            ),
            f"renamed_{SECOND_ALIAS}",
        ),
    ]
    if reverse_registry:
        reopened_specs.reverse()
    tools = {raw: tool for raw, tool, _offered in reopened_specs}
    resumed = context_for(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "continued"}])

    run_turn(messages, "inspect again", llm, tools=tools, context=resumed)

    offered_names = [
        definition["function"]["name"] for definition in llm.turns[0]["tools"]
    ]
    assert offered_names == [offered for _raw, _tool, offered in reopened_specs]
    assert SECRET not in json.dumps(llm.turns[0])
    assert SECOND_SECRET not in json.dumps(llm.turns[0])


def test_alias_shaped_ordinary_tool_remains_available_after_unrelated_scan(tmp_path):
    ordinary_name = "redacted_" + "a" * 52
    assert ordinary_name != ALIAS
    executions: list[str] = []

    def leak() -> str:
        executions.append("ran")
        return f"diagnostic {SECRET}"

    tool = simple_tool(ordinary_name, leak, description="ordinary alias-shaped tool")
    tools = {ordinary_name: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [{"name": ordinary_name, "arguments": {}}],
            },
            {"type": "text", "content": "done"},
        ]
    )

    run_turn(messages, "inspect", llm, tools=tools, context=first)
    first.close()

    resumed = context_for(tmp_path)
    reopened_llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(
        messages,
        "inspect again",
        reopened_llm,
        tools=tools,
        context=resumed,
    )

    assert llm.turns[1]["tools"][0]["function"]["name"] == ordinary_name
    assert reopened_llm.turns[0]["tools"][0]["function"]["name"] == ordinary_name
    assert executions == ["ran"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_scanner_does_not_claim_an_occupied_alias_shaped_tool_name(
    tmp_path,
    wrapped,
):
    ordinary_name = f"wrapped_{ALIAS}" if wrapped else ALIAS
    executions: list[str] = []

    def leak() -> str:
        executions.append("ran")
        return f"diagnostic {SECRET}"

    tool = simple_tool(
        ordinary_name,
        leak,
        description="ordinary tool occupying a scanner alias",
    )
    tools = {ordinary_name: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "calls": [{"name": ordinary_name, "arguments": {}}],
            },
            {"type": "text", "content": "done"},
        ]
    )

    run_turn(messages, "inspect", llm, tools=tools, context=first)

    sensitive_metadata = [
        json.loads(row["meta_json"])
        for row in first._db.execute(
            "SELECT e.meta_json FROM entries e JOIN folds f USING(span_id) "
            "WHERE f.reason = 'sensitive'"
        ).fetchall()
    ]
    persisted_alias = sensitive_metadata[0]["scanner_aliases"][
        hashlib.sha256(SECRET.encode()).hexdigest()
    ]
    assert persisted_alias != ALIAS
    assert persisted_alias not in ordinary_name
    assert llm.turns[1]["tools"][0]["function"]["name"] == ordinary_name
    first.close()

    resumed = context_for(tmp_path)
    reopened_llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(
        messages,
        "inspect again",
        reopened_llm,
        tools=tools,
        context=resumed,
    )

    assert reopened_llm.turns[0]["tools"][0]["function"]["name"] == ordinary_name
    assert executions == ["ran"]


def test_legacy_recovery_does_not_claim_an_ordinary_alias_shaped_history(tmp_path):
    raw_name = f"inspect:{SECRET}"
    ordinary_name = "redacted_" + "a" * 52
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"name": raw_name, "arguments": {}},
                        {"name": ordinary_name, "arguments": {}},
                    ],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={
            raw_name: simple_tool(
                raw_name,
                lambda: f"diagnostic {SECRET}",
                description="returns a credential",
            ),
            ordinary_name: simple_tool(
                ordinary_name,
                lambda: f"diagnostic {SECOND_SECRET}",
                description="ordinary alias-shaped tool",
            ),
        },
        context=first,
    )
    strip_legacy_alias_metadata(first)
    first.close()

    resumed = context_for(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(
        messages,
        "inspect again",
        llm,
        tools={
            raw_name: simple_tool(
                raw_name,
                lambda: "ordinary result",
                description="credential tool after reopen",
            ),
            ordinary_name: simple_tool(
                ordinary_name,
                lambda: "ordinary result",
                description="ordinary alias-shaped tool after reopen",
            ),
        },
        context=resumed,
    )

    assert [
        definition["function"]["name"] for definition in llm.turns[0]["tools"]
    ] == [f"inspect:{ALIAS}", ordinary_name]
    assert SECRET not in json.dumps(llm.turns[0])


def test_legacy_recovery_rejects_takeover_before_exempting_ordinary_names(tmp_path):
    first_raw = f"old_a_{SECRET}"
    second_raw = f"old_b_{SECOND_SECRET}"
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"name": first_raw, "arguments": {}},
                        {"name": second_raw, "arguments": {}},
                    ],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={
            first_raw: simple_tool(
                first_raw,
                lambda: f"diagnostic {SECRET}",
                description="returns the first credential",
            ),
            second_raw: simple_tool(
                second_raw,
                lambda: f"diagnostic {SECOND_SECRET}",
                description="returns the second credential",
            ),
        },
        context=first,
    )
    strip_legacy_alias_metadata(first)
    first.close()

    takeover_name = f"old_a_{ALIAS}"
    executions: list[str] = []
    tools = {
        takeover_name: simple_tool(
            takeover_name,
            lambda: executions.append("takeover") or "ordinary result",
            description="unrelated alias takeover",
        ),
        f"new_a_{SECRET}": simple_tool(
            f"new_a_{SECRET}",
            lambda: executions.append("first") or "ordinary result",
            description="first credential under a new wrapper",
        ),
        second_raw: simple_tool(
            second_raw,
            lambda: executions.append("second") or "ordinary result",
            description="second credential under its original wrapper",
        ),
    }
    resumed = context_for(tmp_path)
    before = deepcopy(messages)

    with pytest.raises(FoldError, match="reserved scanner alias"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools=tools,
            context=resumed,
        )

    assert messages == before
    assert executions == []


def test_scanner_recovers_a_legacy_v3_alias_without_mapping_metadata(tmp_path):
    tool = simple_tool(
        SECRET,
        lambda: f"diagnostic {SECRET}",
        description="returns a credential",
    )
    tools = {SECRET: tool}
    messages: list[dict] = []
    first = context_for(tmp_path)
    run_turn(
        messages,
        "inspect",
        FakeLLM(
            [
                {
                    "type": "tool_calls",
                    "calls": [{"name": SECRET, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools=tools,
        context=first,
    )
    strip_legacy_alias_metadata(first)
    first._db.execute("DROP TABLE IF EXISTS scanner_aliases")
    first._db.commit()
    first.close()

    resumed = context_for(tmp_path)
    second_llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(messages, "inspect again", second_llm, tools=tools, context=resumed)

    boundary = second_llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == ALIAS
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == ALIAS
    assert SECRET not in json.dumps(boundary)
    assert all(
        SECRET not in json.dumps(resumed.reconstruct_projection(row["projection_id"]))
        for row in resumed.projection_chain()
        if row["kind"] == "request"
    )


INNER_SECRET = "ghp_" + "a" * 30
OUTER_SECRET = f"sk-{INNER_SECRET}"  # one credential pattern nested inside another


@pytest.mark.parametrize(
    ("raw_name", "leaked", "historical_name", "scrubbed"),
    [
        pytest.param(
            f"x_{SECRET}",
            f"diagnostic {SECRET}",
            f"x_{ALIAS}",
            [SECRET],
            id="after_an_identifier_underscore",
        ),
        pytest.param(
            f"inspect:{SECRET}:{SECOND_SECRET}",
            f"diagnostic {SECRET} and {SECOND_SECRET}",
            f"inspect:{ALIAS}:{SECOND_ALIAS}",
            [SECRET, SECOND_SECRET],
            id="multi_secret_tool_name",
        ),
        pytest.param(
            OUTER_SECRET,
            f"diagnostic {OUTER_SECRET}",
            FoldingContext._identifier_alias(OUTER_SECRET),
            [OUTER_SECRET, INNER_SECRET],
            id="overlapping_secret_patterns",
        ),
    ],
)
def test_scanner_recovers_legacy_aliases_in_a_tool_name(
    tmp_path, raw_name, leaked, historical_name, scrubbed
):
    # An older ledger persisted no alias map: on reopen the scanner must rebuild
    # every alias from its hash, wherever the secret sits inside the tool name.
    tool = simple_tool(raw_name, lambda: leaked, description="returns a credential")
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
    strip_legacy_alias_metadata(first)
    first.close()

    resumed = context_for(tmp_path)
    llm = FakeLLM([{"type": "text", "content": "continued"}])
    run_turn(messages, "inspect again", llm, tools=tools, context=resumed)

    boundary = llm.turns[0]
    assert boundary["tools"][0]["function"]["name"] == historical_name
    assert boundary["messages"][1]["tool_calls"][0]["function"]["name"] == (
        historical_name
    )
    for secret in scrubbed:
        assert secret not in json.dumps(boundary), secret


def test_legacy_overlap_recovery_does_not_license_a_separate_inner_tool(tmp_path):
    inner_secret = "ghp_" + "a" * 30
    outer_secret = f"sk-{inner_secret}"
    outer_tool = simple_tool(
        outer_secret,
        lambda: f"diagnostic {outer_secret}",
        description="returns overlapping credentials",
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
                    "calls": [{"name": outer_secret, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={outer_secret: outer_tool},
        context=first,
    )
    strip_legacy_alias_metadata(first)
    first.close()

    inner_tool = simple_tool(
        inner_secret,
        lambda: "ordinary result",
        description="separate inner credential tool",
    )
    resumed = context_for(tmp_path)
    before = deepcopy(messages)

    with pytest.raises(FoldError, match="cannot safely recover every"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools={outer_secret: outer_tool, inner_secret: inner_tool},
            context=resumed,
        )

    assert messages == before


def test_legacy_recovery_reserves_an_embedded_alias_against_takeover(tmp_path):
    raw_name = f"inspect:{SECRET}"
    tool = simple_tool(
        raw_name,
        lambda: f"diagnostic {SECRET}",
        description="returns a credential",
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
    strip_legacy_alias_metadata(first)
    first.close()
    before = deepcopy(messages)
    executions: list[str] = []
    takeover = simple_tool(
        ALIAS,
        lambda: executions.append("ran") or "ordinary result",
        description="unrelated tool",
    )
    resumed = context_for(tmp_path)

    with pytest.raises(FoldError, match="reserved scanner alias"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools={raw_name: tool, ALIAS: takeover},
            context=resumed,
        )

    assert messages == before
    assert executions == []


def test_scanner_rejects_a_late_tool_that_occupies_a_persisted_alias(tmp_path):
    credential_tool = simple_tool(
        SECRET,
        lambda: f"diagnostic {SECRET}",
        description="returns a credential",
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
                    "calls": [{"name": SECRET, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={SECRET: credential_tool},
        context=first,
    )
    first.close()
    before = deepcopy(messages)
    ordinary_tool = noop_tool(name=ALIAS)
    tools = {SECRET: credential_tool, ALIAS: ordinary_tool}
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
    assert list(tools) == [SECRET, ALIAS]
    assert tools[SECRET] is credential_tool
    assert tools[ALIAS] is ordinary_tool


@pytest.mark.parametrize("wrapper", ["", "wrapped:"], ids=["bare_alias", "wrapped_alias"])
def test_scanner_rejects_registry_takeover_of_a_persisted_alias(tmp_path, wrapper):
    credential_tool = simple_tool(
        SECRET,
        lambda: f"diagnostic {SECRET}",
        description="returns a credential",
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
                    "calls": [{"name": SECRET, "arguments": {}}],
                },
                {"type": "text", "content": "done"},
            ]
        ),
        tools={SECRET: credential_tool},
        context=first,
    )
    first.close()
    before = deepcopy(messages)
    executions: list[str] = []
    takeover_name = f"{wrapper}{ALIAS}"
    takeover = simple_tool(
        takeover_name,
        lambda: executions.append("ran") or "ordinary result",
        description="unrelated tool",
    )
    resumed = context_for(tmp_path)

    with pytest.raises(FoldError, match="reserved scanner alias"):
        run_turn(
            messages,
            "inspect again",
            FakeLLM([{"type": "text", "content": "must not dispatch"}]),
            tools={takeover_name: takeover},
            context=resumed,
        )

    assert messages == before
    assert executions == []
    assert takeover.name == takeover_name
