import json
from pathlib import Path

from harness.llm import normalize


def test_normalize_extracts_a_plain_assistant_message():
    raw = json.loads(Path("tests/fixtures/codex_response.json").read_text())
    msg = normalize(raw)
    assert isinstance(msg["content"], str) and msg["content"]
    # exactly two keys: provider extras must not leak into the conversation
    assert msg == {"role": "assistant", "content": msg["content"]}
