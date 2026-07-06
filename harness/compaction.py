import json
from functools import cache

import tiktoken

from harness.llm import LLMClient

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

@cache
def _encoding() -> tiktoken.Encoding:
    # lazy: get_encoding fetches the BPE file on first ever use (then caches
    # on disk), so importing this module stays network-free
    return tiktoken.get_encoding("o200k_base")


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
        total += len(_encoding().encode(system))
    for definition in tools or []:
        total += len(_encoding().encode(json.dumps(definition)))
    for m in messages:
        total += 4
        if m.get("content"):
            total += len(_encoding().encode(m["content"]))
        for call in m.get("tool_calls") or []:
            total += len(_encoding().encode(call["function"]["name"]))
            total += len(_encoding().encode(call["function"]["arguments"]))
    return total


def _is_plain_assistant(message: dict) -> bool:
    return message["role"] == "assistant" and not message.get("tool_calls")


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
    dangling-call corruption from lessons 4/8). When no safe cut exists,
    the boundary widens toward the start; snapping to zero means nothing
    to summarize and the original list is returned as-is.
    """
    cut = len(messages) - keep_recent
    while cut > 0 and not _is_plain_assistant(messages[cut - 1]):
        cut -= 1
    if cut <= 0:
        return messages
    old = messages[:cut]
    summary = llm.complete(old + [{"role": "user", "content": summary_instruction}])
    content = summary["content"]
    if breadcrumbs is not None:
        content += "\n\n[Auto-generated — not summarized]\n" + breadcrumbs
    return [{"role": "assistant", "content": content}] + messages[cut:]
