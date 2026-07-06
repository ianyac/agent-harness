import json

from harness.session import SessionLog


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


def test_a_torn_final_line_is_tolerated(tmp_path):
    path = tmp_path / "session.jsonl"
    log = SessionLog(path)
    messages = exchange("q1", "a1")
    log.record_turn(messages)
    with path.open("a") as f:
        f.write('{"type": "message", "message": {"role": "u')  # crash mid-write
    assert SessionLog(path).load() == messages


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
