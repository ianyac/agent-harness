# Agent Harness UI

This is the standalone local UI service for `agent-harness`.

## Setup

From this directory, create the service environment and install its dependencies:

```bash
uv sync
```

## Tests

Run the UI test suite with:

```bash
uv run pytest
```

The test bootstrap uses the repository's vendored `vendor/tiktoken` cache, so
test execution does not need network access for tokenizer assets.
