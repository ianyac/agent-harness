# Seam change: `LLMClient.complete` gained a `system` kwarg (lesson 10)

As of lesson 10, `run_turn` always calls
`llm.complete(messages, tools=..., system=...)` — the keyword is passed on
every iteration even when no system prompt is set (`system=None`).

Any `LLMClient` implementation written against the pre-lesson-10
two-parameter signature will crash on its first call with
`TypeError: complete() got an unexpected keyword argument 'system'`.

Action for implementers: add `system: str | None = None` to `complete()`.

Semantics: a non-`None` value replaces the backend's default instructions
outright — empty string included. `None` means "use your default". There is
no merging.
