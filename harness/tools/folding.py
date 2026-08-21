"""Agent-facing tools for the session-local context-folding control plane."""

from harness.folding import AGENT_REASONS, FoldingContext
from harness.tools.base import Tool


_SPAN_PROPERTY = {
    "type": "string",
    "pattern": r"^m\d+(\.r\d+(\.c\d+)?|\.i\d+|\.t\d+)?$",
    "description": (
        "One span id exactly as shown in a context label or fold marker. "
        "Copy it; unknown ids are rejected with a nearest-match suggestion. "
        "Ids appear next to what they name: [m8.r0 · ~41K tok] on a tool "
        "result (m8.r0.c1 for a chunk), m7.i0 for a written payload, "
        "[m7 · ~1.2K tok] on your own earlier text, [m7.t0 · ~3K tok "
        "thinking] on its reasoning."
    ),
}


def fold_tool(context: FoldingContext) -> Tool:
    return Tool(
        name="fold",
        description=(
            "Collapse one span after its line of work closes, replacing it at "
            "the next checkpoint with a marker carrying your written verdict. "
            "One span per call, each with its own note; when several spans "
            "close together, fold them in the same step as parallel fold calls "
            "rather than one call per step. The note is what future-you will "
            "have instead of the content: state the conclusion, key support, "
            "paths, lines, and exact values you would otherwise re-check. "
            "Never fold mid-investigation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "span_id": _SPAN_PROPERTY,
                "reason": {
                    "type": "string",
                    "enum": list(AGENT_REASONS),
                    "description": (
                        "Why the content can leave working context. `poisoned` "
                        "means the content is wrong and requires a correction."
                    ),
                },
                "note": {
                    "type": "string",
                    "minLength": 20,
                    "maxLength": 1_500,
                    "description": (
                        "A declarative, evidence-backed verdict. Generic and "
                        "instruction-shaped notes are rejected."
                    ),
                },
            },
            "required": ["span_id", "reason", "note"],
        },
        execute=lambda span_id, reason, note: context.fold(span_id, reason, note),
        # It changes only the agent's recoverable visibility projection, not
        # the user's workspace, so no filesystem mutation prompt is warranted.
        read_only=True,
        inheritable=False,
    )


def unfold_tool(context: FoldingContext) -> Tool:
    return Tool(
        name="unfold",
        description=(
            "Reinstate one folded span in full at the context tail and close "
            "its fold record. Prefer re-reading a live file when freshness "
            "matters. Quarantined and purged spans cannot be unfolded."
        ),
        parameters={
            "type": "object",
            "properties": {"span_id": _SPAN_PROPERTY},
            "required": ["span_id"],
        },
        execute=lambda span_id: context.unfold(span_id),
        read_only=True,
        inheritable=False,
    )
