import json

from harness.tools.agent import agent_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool
from tests.fake_llm import FakeLLM
from tests.helpers import noop_tool
from tests.test_folding import context_for, tool_exchange


def test_identical_tool_results_fold_the_later_copy_to_the_earliest(tmp_path):
    # Regression caught: reversing survivor direction breaks every prior
    # reference to the earliest result and needlessly invalidates more prefix.
    messages = tool_exchange("read_file", {"path": "a.py"}, "same bytes")
    messages.extend(
        tool_exchange("read_file", {"path": "a.py"}, "same bytes", call_id="call_1", user="again")
    )
    context = context_for(tmp_path)

    context.sync(messages, {"read_file": read_file_tool(tmp_path)})
    context.checkpoint()

    assert context.state("m2.r0") == "visible"
    assert context.state("m5.r0") == "folded"
    assert context.reason("m5.r0") == "duplicate"
    assert context.project(messages)[5]["content"] == "[dup of m2.r0]"


def test_later_read_of_same_path_supersedes_the_older_result(tmp_path):
    # Regression caught: stale file contents left visible after a later read can
    # anchor reasoning to a world state the agent has already replaced.
    messages = tool_exchange("read_file", {"path": "a.py"}, "old body")
    messages.extend(
        tool_exchange("read_file", {"path": "a.py"}, "new body", call_id="call_1", user="again")
    )
    context = context_for(tmp_path)

    context.sync(messages, {"read_file": read_file_tool(tmp_path)})
    context.checkpoint()

    assert context.reason("m2.r0") == "superseded"
    assert "m5.r0" in context.note("m2.r0")
    assert "new body" in context.project(messages)[5]["content"]
    assert "superseded" in context.project(messages)[2]["content"]


def test_successful_exact_retry_folds_the_handled_failure(tmp_path):
    # Regression caught: matching only tool name would hide failures from a
    # different operation; the canonicalized arguments are part of identity.
    messages = tool_exchange("build", {"target": "app"}, "Error: compiler unavailable")
    messages.extend(
        tool_exchange("build", {"target": "app"}, "exit code: 0", call_id="call_1", user="again")
    )
    context = context_for(tmp_path)

    context.sync(messages, {"build": noop_tool(name="build")})

    assert context.reason("m2.r0") == "handled_failure"
    assert "m5.r0" in context.note("m2.r0")


def test_failure_for_different_arguments_stays_visible(tmp_path):
    messages = tool_exchange("build", {"target": "app"}, "Error: compiler unavailable")
    messages.extend(
        tool_exchange("build", {"target": "docs"}, "exit code: 0", call_id="call_1", user="again")
    )
    context = context_for(tmp_path)

    context.sync(messages, {"build": noop_tool(name="build")})

    assert context.state("m2.r0") == "visible"


def test_successful_write_folds_only_the_registered_payload_value(tmp_path):
    # Regression caught: replacing the entire input or JSON string would alter
    # call identity/schema; only the large string value is safe surgery.
    content = "line = 1\n" * 200
    messages = tool_exchange(
        "write_file",
        {"path": "a.py", "content": content},
        f"wrote {len(content)} characters to a.py",
    )
    context = context_for(tmp_path)

    context.sync(messages, {"write_file": write_file_tool(tmp_path)})
    context.checkpoint()
    projected_args = json.loads(
        context.project(messages)[1]["tool_calls"][0]["function"]["arguments"]
    )

    assert context.reason("m1.i0") == "superseded"
    assert projected_args["path"] == "a.py"
    assert projected_args["content"].startswith("[folded m1.i0")
    assert "line = 1" not in projected_args["content"]


def test_failed_write_keeps_its_payload_visible(tmp_path):
    content = "line = 1\n" * 200
    messages = tool_exchange(
        "write_file",
        {"path": "a.py", "content": content},
        "Error: PermissionError: denied",
    )
    context = context_for(tmp_path)

    context.sync(messages, {"write_file": write_file_tool(tmp_path)})

    assert context.state("m1.i0") == "visible"
    projected_args = json.loads(
        context.project(messages)[1]["tool_calls"][0]["function"]["arguments"]
    )
    assert projected_args["content"] == content


def test_completed_delegation_folds_its_consumed_brief_as_scaffolding(tmp_path):
    task = "Inspect every auth module and report only the verified conclusion. " * 20
    messages = tool_exchange("agent", {"task": task}, "Auth modules are clean.")
    context = context_for(tmp_path)
    agent = agent_tool(FakeLLM([]), {}, policy=None)

    context.sync(messages, {"agent": agent})

    assert context.reason("m1.i0") == "scaffolding"


def test_auto_fold_notice_is_delivered_on_the_following_turn_once(tmp_path):
    messages = tool_exchange("read_file", {"path": "a.py"}, "same bytes")
    messages.extend(
        tool_exchange("read_file", {"path": "a.py"}, "same bytes", call_id="call_1", user="again")
    )
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": read_file_tool(tmp_path)})

    context.begin_turn(messages)
    first = context.turn_notice()
    context.begin_turn(messages)

    assert "auto-folded m5.r0" in first
    assert "duplicate" in first
    assert context.turn_notice() == ""


def test_refetch_of_a_folded_operation_is_logged_without_content_or_arguments(tmp_path):
    messages = tool_exchange("search", {"pattern": "jwt", "path": "src"}, "12 hits")
    context = context_for(tmp_path, decision_log=True)
    context.sync(messages, {"search": noop_tool(name="search")})
    context.fold(
        "m2.r0",
        "finished",
        "JWT search found 12 hits, all confined to auth and its tests.",
    )
    context.checkpoint()
    messages.extend(
        tool_exchange(
            "search", {"path": "src", "pattern": "jwt"}, "12 hits", call_id="call_1", user="again"
        )
    )

    context.sync(messages, {"search": noop_tool(name="search")})

    events = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    refetch = next(event for event in events if event["ev"] == "refetch_candidate")
    assert refetch["span"] == "m2.r0"
    assert not ({"content", "note", "path", "args"} & refetch.keys())


def test_pressure_notice_surfaces_three_older_open_spans_once(tmp_path):
    # Regression caught: without ambient candidates, long sessions rely on a
    # standing prompt whose compliance decays; repeated identical notices are
    # equally bad because they become background noise.
    messages = tool_exchange("one", {}, "first")
    messages.extend(
        tool_exchange("two", {}, "second", call_id="call_1", user="again")
    )
    messages.extend(
        tool_exchange("three", {}, "third", call_id="call_2", user="again")
    )
    context = context_for(tmp_path)
    tools = {
        "one": noop_tool(name="one"),
        "two": noop_tool(name="two"),
        "three": noop_tool(name="three"),
    }
    context.sync(messages, tools)

    context.begin_turn(messages, tools)
    notice = context.turn_notice()
    context.begin_turn(messages, tools)

    assert notice.startswith("[workspace: 3 spans look closed")
    assert all(span in notice for span in ("m2.r0", "m5.r0", "m8.r0"))
    assert "spans look closed" not in context.turn_notice()


def test_pressure_notice_is_bound_to_the_user_message_for_replay(tmp_path):
    messages = tool_exchange("one", {}, "first")
    messages.extend(
        tool_exchange("two", {}, "second", call_id="call_1", user="again")
    )
    messages.extend(
        tool_exchange("three", {}, "third", call_id="call_2", user="again")
    )
    context = context_for(tmp_path)
    context.sync(
        messages,
        {
            "one": noop_tool(name="one"),
            "two": noop_tool(name="two"),
            "three": noop_tool(name="three"),
        },
    )
    context.begin_turn(messages)
    messages.append({"role": "user", "content": "continue"})

    context.sync(messages)

    assert context.reconstruct(turn=context.turn)[-1]["content"].startswith(
        "[workspace: 3 spans look closed"
    )


def test_checkpoint_notice_reports_open_and_folded_workspace_shape(tmp_path):
    messages = tool_exchange("read_file", {"path": "a.py"}, "evidence")
    context = context_for(tmp_path)
    context.sync(messages, {"read_file": read_file_tool(tmp_path)})
    context.fold(
        "m2.r0",
        "finished",
        "Read completed: a.py defines only the validated implementation path.",
    )

    context.checkpoint(reason="phase boundary")

    notice = context.turn_notice()
    assert notice.startswith("[workspace after checkpoint:")
    assert "1 folded" in notice


def test_user_reference_to_folded_verdict_is_surfaced_and_replayable(tmp_path):
    messages = tool_exchange("install", {}, "node-gyp failed") + [
        {"role": "assistant", "content": "fixed"}
    ]
    context = context_for(tmp_path)
    context.sync(messages, {"install": noop_tool(name="install")})
    context.fold(
        "m2.r0",
        "handled_failure",
        "npm install error came from node-gyp; Python 3.11 resolved the failure.",
    )
    context.checkpoint()
    context.begin_turn(messages)
    messages.append(
        {"role": "user", "content": "Was the earlier npm install error fully resolved?"}
    )

    context.sync(messages)

    assert "user may be referring to folded m2.r0" in context.turn_notice()
    historical = context.reconstruct(turn=context.turn)
    assert historical[-1]["content"].startswith(
        "[user may be referring to folded m2.r0"
    )
