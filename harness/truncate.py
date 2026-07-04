def truncate(text: str, limit: int) -> str:
    """Clip text to roughly `limit` chars, keeping head and tail.

    Tool results feed a context window, not a program: the middle of a huge
    output is the least useful part, so we keep both ends and mark the gap
    so the model knows content was removed (never silently).
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    head, tail = text[:half], text[-half:]
    removed = len(text) - len(head) - len(tail)
    return f"{head}\n[... {removed} chars truncated ...]\n{tail}"
