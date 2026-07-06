import json
from functools import cache

import tiktoken

from harness.llm import LLMClient
from harness.truncate import truncate

DEFAULT_SUMMARY_INSTRUCTION = (
    "Summarize the conversation so far for your own future reference. "
    "Begin with the line 'Summary of the conversation so far:' and cover, "
    "under these headings:\n"
    "- Goal: what the user is trying to accomplish\n"
    "- Current state: where the work stands now\n"
    "- Decisions: choices made and why\n"
    "- Learnings and warnings: facts discovered along the way, approaches "
    "that failed, constraints to respect\n"
    "- Unfinished work: what remains to be done\n"
    "Reply with only the summary."
)

# per-message cap on what the summarizer is shown: the rescue call must
# stay well under the window even when tool results were huge
_SUMMARIZER_CONTENT_LIMIT = 2_000


@cache
def _encoding() -> tiktoken.Encoding:
    # lazy: get_encoding fetches the BPE file on first ever use (then caches
    # on disk), so importing this module stays network-free; the test suite
    # loads it from the vendored copy via TIKTOKEN_CACHE_DIR (conftest.py)
    return tiktoken.get_encoding("o200k_base")


def _count(text: str) -> int:
    # disallowed_special=(): a special-token literal inside content (e.g. a
    # file containing "<|endoftext|>") is data to count, not a control
    # token to reject — the default raises ValueError on it
    return len(_encoding().encode(text, disallowed_special=()))


def estimate_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
) -> int:
    """What a complete() call with these arguments costs the provider, near
    enough to trust as a compaction trigger. Counts the system prompt, the
    tool definitions (as serialized JSON — the provider re-renders schemas
    its own way, so this part stays an estimate), and each message's text
    and tool-call payloads plus the ~4-token structural overhead of
    OpenAI's accounting."""
    total = 0
    if system:
        total += _count(system)
    for definition in tools or []:
        total += _count(json.dumps(definition))
    for m in messages:
        total += 4
        if m.get("content"):
            total += _count(m["content"])
        for call in m.get("tool_calls") or []:
            total += _count(call["function"]["name"])
            total += _count(call["function"]["arguments"])
    return total


def _is_plain_assistant(message: dict) -> bool:
    return message["role"] == "assistant" and not message.get("tool_calls")


def _safe_cut(messages: list[dict], keep_recent: int) -> int:
    """Choose the cut index (messages[:cut] get summarized). Prefer the
    largest safe cut that keeps at least keep_recent messages; when tool
    traffic leaves no boundary there, fall forward to the smallest safe cut
    beyond it — keeping less history beats keeping everything and
    overflowing the window. Cuts below 2 are useless (a cut of 1 would just
    re-summarize a previous summary forever), so 0 means: nothing worth
    doing."""
    target = len(messages) - keep_recent
    for cut in range(min(target, len(messages) - 1), 1, -1):
        if _is_plain_assistant(messages[cut - 1]):
            return cut
    for cut in range(max(target + 1, 2), len(messages)):
        if _is_plain_assistant(messages[cut - 1]):
            return cut
    return 0


def compact(
    messages: list[dict],
    llm: LLMClient,
    keep_recent: int,
    summary_instruction: str = DEFAULT_SUMMARY_INSTRUCTION,
    breadcrumbs: str | None = None,
) -> list[dict]:
    """Replace everything older than the last keep_recent messages with a
    single summary message: the model's sectioned summary plus, when the
    caller provides one, a mechanical breadcrumb note (e.g. a pointer to
    a durable action log) marked as auto-generated so it never passes
    through judgment. Pure: returns a new list, never mutates.

    The cut only ever lands just after a plain assistant reply — never
    between an assistant tool_calls message and its tool results (the
    dangling-call corruption from lessons 4/8); see _safe_cut for how the
    boundary is chosen. The summarizer sees old content truncated per
    message, so the rescue call itself stays small. Returns the input
    unchanged when there is no useful cut or the summarizer returns no
    text (e.g. a spurious tool call) — the trigger simply retries later.
    """
    if len(messages) <= keep_recent:
        return messages
    cut = _safe_cut(messages, keep_recent)
    if cut == 0:
        return messages
    old = [
        {**m, "content": truncate(m["content"], _SUMMARIZER_CONTENT_LIMIT)}
        if m.get("content")
        else m
        for m in messages[:cut]
    ]
    summary = llm.complete(old + [{"role": "user", "content": summary_instruction}])
    content = summary.get("content")
    if not content:
        return messages
    if breadcrumbs is not None:
        content += "\n\n[Auto-generated — not summarized]\n" + breadcrumbs
    return [{"role": "assistant", "content": content}] + messages[cut:]
