import json
from pathlib import Path


class SessionLog:
    """The conversation made durable: an append-only JSONL event log.

    Two event kinds — {"type": "message", "message": ...} for each message
    of a completed turn, and {"type": "compact", "cut": ..., "summary": ...}
    when compaction replaced history. History is never rewritten on disk;
    load() folds the events back into the current messages state.

    The log tracks how much of the in-memory list it has already recorded
    (as a folded-state length), so record_turn(messages) appends exactly
    the messages the last turn added — positions survive compaction because
    record_compaction applies the same splice arithmetic the loop does.
    """

    def __init__(self, path: Path):
        self.path = path
        self._length = 0  # folded-state length of what is on disk

    def load(self) -> list[dict]:
        """Fold the log into the current messages state and prime this
        SessionLog to continue recording after it."""
        messages: list[dict] = []
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break  # a crash can tear the final line; stop at the tear
            match event["type"]:
                case "message":
                    messages.append(event["message"])
                case "compact":
                    messages = [event["summary"]] + messages[event["cut"] :]
        self._length = len(messages)
        return messages

    def record_turn(self, messages: list[dict]) -> None:
        """Append every message beyond what is already recorded. Call at
        turn boundaries only: a crash then leaves the file ending at the
        last completed exchange, matching the REPL's rollback."""
        for message in messages[self._length :]:
            self._append({"type": "message", "message": message})
            self._length += 1

    def record_compaction(self, cut: int, summary: dict) -> None:
        """Record compaction as an event. Safe mid-turn: the cut invariant
        means a cut never reaches past the recorded prefix into the
        current turn's unrecorded messages."""
        self._append({"type": "compact", "cut": cut, "summary": summary})
        self._length = self._length - cut + 1

    def _append(self, event: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(event) + "\n")
