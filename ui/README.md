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

## Run in a browser

Build the shared frontend, then start the loopback-only service from this
directory:

```bash
cd frontend
npm run build
cd ..
uv run python -m server --workspace /absolute/path/to/workspace --port 0
```

The command prints one complete `http://127.0.0.1:<port>/bootstrap?token=...`
URL. Open it once to exchange the per-launch secret for two independent,
32-byte capabilities. The redirect uses an unguessable `/_app/<token>/` path
for the frontend and puts the API token only in the URL fragment as
`#token=...`; fragments are not sent in HTTP requests. No credential cookie is
created. The bootstrap and frontend responses set
`Referrer-Policy: no-referrer`, and the original launch secret is retired as
soon as the browser exchange succeeds.

The browser keeps the fragment token in memory and sends it as
`Authorization: Bearer <token>` for API requests and as the second WebSocket
subprotocol after `harness-ui`. Static assets and SPA routes must stay under
the capability path (the frontend build therefore uses relative asset URLs).
The token cannot be exchanged twice. API routes, WebSocket upgrades, and
static files remain authenticated, and the service never binds a non-loopback
interface.

Use `--metadata-db /absolute/path/to/metadata.sqlite3` to override the default
platform user-data database.

## Sidecar startup

Native hosts use `--secret-stdin` and write exactly one newline-delimited JSON
bootstrap record to stdin. Its canonical workspace is authoritative; an
optional `--workspace` argument must resolve to the same directory:

```json
{"type":"bootstrap","secret":"generated-in-rust","workspace":"/canonical/path"}
```

Close the child's stdin immediately after writing the line. EOF is part of the
one-shot framing contract; empty, unterminated, oversized, or additional input
is rejected before the server binds.

After the loopback socket is serving, stdout contains exactly one readiness
record with the OS-assigned port. The launch secret is never written to sidecar
stdout. The Tauri webview uses that launch secret directly as the API bearer
and WebSocket subprotocol credential; exact `tauri://localhost` CORS and
preflight responses are enabled without cookies. Send SIGTERM and wait for the
process so the graceful application lifespan can close session locks and
metadata.
