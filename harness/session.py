import json
import os
from pathlib import Path


def _is_boundary(message: dict) -> bool:
    # a completed exchange ends with a plain assistant reply — the same
    # boundary compaction cuts on and the REPL rolls back to
    return message["role"] == "assistant" and not message.get("tool_calls")


class SessionLog:
    """The conversation made durable: an append-only JSONL event log.

    Two event kinds — {"type": "message", "message": ...} for each message
    of a completed turn, and {"type": "compact", "cut": ..., "summary": ...}
    when compaction replaced history. History is never rewritten on disk;
    load() folds the events back into the current messages state.

    The log tracks how much of the in-memory list it has already recorded
    (as a folded-state length), so record_turn(messages) appends exactly
    the messages the last turn added. The bookkeeping follows MEMORY, not
    disk: a failed write degrades to events missing from resume, never to
    misaligned arithmetic corrupting every later record.
    """

    def __init__(self, path: Path):
        self.path = path
        self._length = 0  # folded-state length of what is on disk

    def load(self) -> list[dict]:
        """Fold the log into the last completed-exchange state, healing the
        file as a side effect: a torn final line (crash mid-write) or a
        dangling mid-turn tail (crash mid-record) is truncated away, so
        appends always continue from the state this method returns."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            raw = ""
        messages: list[dict] = []
        checkpoint: list[dict] = []
        consumed = 0  # chars of raw folded so far
        good = 0  # chars up to the last boundary-terminated state
        for line in raw.splitlines(keepends=True):
            consumed += len(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                break  # a crash can tear the final line; stop at the tear
            match event["type"]:
                case "message":
                    messages.append(event["message"])
                case "compact":
                    messages = [event["summary"]] + messages[event["cut"] :]
            if messages and _is_boundary(messages[-1]):
                good = consumed
                checkpoint = list(messages)
        if good < len(raw):
            self.path.write_text(raw[:good])
        elif raw and not raw.endswith("\n"):
            # never leave a final line for the next append to weld onto
            self.path.write_text(raw + "\n")
        self._length = len(checkpoint)
        return checkpoint

    def record_turn(self, messages: list[dict]) -> None:
        """Append every message beyond what is already recorded. Call at
        turn boundaries; the events go down in one write so a turn lands
        whole or not at all."""
        new = messages[self._length :]
        if not new:
            return
        payload = "".join(
            json.dumps({"type": "message", "message": m}) + "\n" for m in new
        )
        try:
            self._append(payload)
        finally:
            self._length = len(messages)

    def record_compaction(self, cut: int, summary: dict) -> None:
        """Record compaction as an event. Safe mid-turn: the cut invariant
        means a cut never reaches past the recorded prefix into the
        current turn's unrecorded messages."""
        payload = json.dumps({"type": "compact", "cut": cut, "summary": summary})
        try:
            self._append(payload + "\n")
        finally:
            self._length = self._length - cut + 1

    def _append(self, payload: str) -> None:
        # self-heal: the agent's own tools can delete .agent mid-session
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(payload)


def lock(path: Path) -> None:
    """Claim exclusive use of a session file: two live processes appending
    to one log interleave events into an unloadable history. Raises
    RuntimeError while a live process holds the claim; locks left behind
    by dead processes are reclaimed."""
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            holder = int(lock_path.read_text().strip())
        except ValueError:
            holder = None
        if holder is not None and _alive(holder):
            raise RuntimeError(f"session {path.name} is in use by pid {holder}")
    lock_path.write_text(str(os.getpid()))


def unlock(path: Path) -> None:
    path.with_suffix(".lock").unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0: existence probe, delivers nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True
