import json
import os
import subprocess

import pytest

from harness.session import SessionLog, lock, unlock


def exchange(question: str, answer: str) -> list[dict]:
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]


def test_recorded_turns_round_trip(tmp_path):
    path = tmp_path / "session.jsonl"
    messages = exchange("q1", "a1") + exchange("q2", "a2")
    log = SessionLog(path)
    log.record_turn(messages)
    assert SessionLog(path).load() == messages


def test_record_turn_appends_only_the_new_messages(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1")
    log.record_turn(messages)
    messages += exchange("q2", "a2")
    log.record_turn(messages)
    # four message events, no duplicates
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(events) == 4
    assert SessionLog(path).load() == messages


def test_compaction_folds_as_an_event_not_an_edit(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1") + exchange("q2", "a2")
    log.record_turn(messages)
    # the loop compacts: [summary] + everything from the cut on
    summary = {"role": "assistant", "content": "SUMMARY"}
    messages = [summary] + messages[2:]
    log.record_compaction(cut=2, summary=summary)
    # the session continues after the compaction
    messages += exchange("q3", "a3")
    log.record_turn(messages)
    assert SessionLog(path).load() == messages
    # and the file never rewrote history: the q1 exchange is still there
    assert "q1" in path.read_text()


def test_a_torn_final_line_is_healed_not_welded(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1")
    log.record_turn(messages)
    with path.open("a") as f:
        f.write('{"type": "message", "message": {"role": "u')  # crash mid-write
    resumed = SessionLog(path)
    assert resumed.load() == messages
    # the tear was truncated on load, so appending never welds onto it:
    # a SECOND resume must still see everything recorded after the first
    messages += exchange("q2", "a2")
    resumed.record_turn(messages)
    assert SessionLog(path).load() == messages


def test_resume_rolls_back_a_turn_interrupted_mid_record(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1")
    log.record_turn(messages)
    # a crash inside record_turn: complete JSON lines, but the turn's tail
    # (its final plain assistant reply) never made it to disk
    with path.open("a") as f:
        f.write(json.dumps({"type": "message", "message": {"role": "user", "content": "q2"}}) + "\n")
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "noop", "arguments": "{}"},
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    resumed = SessionLog(path)
    # a dangling tool_call would poison every later request: resume folds
    # back to the last completed exchange, and heals the file to match
    assert resumed.load() == messages
    messages += exchange("q3", "a3")
    resumed.record_turn(messages)
    assert SessionLog(path).load() == messages


def test_mid_turn_compaction_folds_exactly(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1") + exchange("q2", "a2")
    log.record_turn(messages)
    # the production sequence: the next turn's user message is in memory
    # (unrecorded) when the trigger fires mid-turn
    messages.append({"role": "user", "content": "q3"})
    summary = {"role": "assistant", "content": "SUMMARY"}
    cut = 2  # the invariant keeps any cut inside the recorded prefix
    messages[:] = [summary] + messages[cut:]
    log.record_compaction(cut=cut, summary=summary)
    messages.append({"role": "assistant", "content": "a3"})
    log.record_turn(messages)
    assert SessionLog(path).load() == messages


def test_a_failed_write_degrades_without_corrupting_later_turns(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1")
    log.record_turn(messages)
    blocker = tmp_path / "blocked"
    blocker.write_text("")  # a file where a directory would be needed
    log.path = blocker / "session.jsonl"
    messages += exchange("q2", "a2")
    with pytest.raises(OSError):
        log.record_turn(messages)
    # the disk write failed but the bookkeeping followed memory: after the
    # path recovers, later turns land intact — q2 is lost, nothing else
    log.path = path
    messages += exchange("q3", "a3")
    log.record_turn(messages)
    assert SessionLog(path).load() == exchange("q1", "a1") + exchange("q3", "a3")


def test_a_live_lock_refuses_a_second_writer(tmp_path):
    path = tmp_path / "session.jsonl"
    lock(path)
    with pytest.raises(RuntimeError):
        lock(path)
    unlock(path)
    lock(path)  # free again after release
    unlock(path)


def test_a_dead_holders_lock_is_reclaimed(tmp_path):
    path = tmp_path / "session.jsonl"
    proc = subprocess.Popen(["true"])
    proc.wait()  # a pid guaranteed to be dead
    path.with_suffix(".lock").write_text(str(proc.pid))
    lock(path)
    assert path.with_suffix(".lock").read_text() == str(os.getpid())
    unlock(path)


def test_missing_and_empty_files_load_to_nothing(tmp_path):
    assert SessionLog(tmp_path / "absent.jsonl").load() == []
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert SessionLog(empty).load() == []


def test_load_primes_the_log_to_continue_recording(tmp_path):
    path = tmp_path / "session.jsonl"
    first = SessionLog(path)
    messages = exchange("q1", "a1")
    first.record_turn(messages)
    # a new process resumes: load, then keep recording without duplicates
    resumed = SessionLog(path)
    messages = resumed.load()
    messages += exchange("q2", "a2")
    resumed.record_turn(messages)
    assert SessionLog(path).load() == messages
