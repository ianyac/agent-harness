# Web Access Implementation Plan (Lesson 24)

**Goal:** `web_fetch` and `web_search` tools, with the confinement a network
capability needs when neither the workspace guard nor the OS sandbox applies.

**Architecture:** One new tool module (`harness/tools/web.py`) holding the
converter, the URL guard, the redirect-following fetch, and both tool
factories; one provider adapter module (`harness/search.py`) mirroring the
`LLMClient`/`CodexAdapter` seam. `main.py` registers `web_fetch` always and
`web_search` only when a provider is configured.

**Tech stack:** Python 3.14, `httpx` (already a direct dependency), stdlib
`ipaddress` / `socket` / `html` / `re`. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-04-web-access-design.md`.
- Both tools are `read_only=True`. Exfiltration is an **accepted** open
  channel; the SSRF guard is the only bound. Do not add consent prompts or
  allowlists — that was decided against.
- Every failure becomes tool-result text; a tool must never raise into the loop.
- The default test suite stays **offline**: no test may open a socket.
- Commit style: `lesson 24: <what changed>`.

---

### Task 1: HTML → text, and the untrusted marker

**Files:** Create `harness/tools/web.py`, `tests/test_web.py`

- [ ] Write failing tests: `html_to_text` strips `<script>`/`<style>` *with their
      contents*, strips remaining tags, unescapes `&amp;`/`&lt;`/`&#39;`,
      collapses 3+ blank lines to one, and leaves plain text unchanged.
- [ ] Run: `uv run pytest tests/test_web.py -v` → FAIL (module missing)
- [ ] Implement `html_to_text` with `re` + `html.unescape`, plus module
      constants `UNTRUSTED_HEADER` / `UNTRUSTED_FOOTER`. Docstring states the
      converter is deliberately crude.
- [ ] Run tests → PASS. Commit.

### Task 2: the URL guard (SSRF)

**Files:** Modify `harness/tools/web.py`, `tests/test_web.py`

- [ ] Write failing tests: `check_url` raises on `file://`/`ftp://`; raises when
      the host resolves to loopback / private / link-local (monkeypatch
      `socket.getaddrinfo` so a *public-looking hostname* resolving to
      `127.0.0.1` is still refused — the point of resolving); passes a public
      address. Include `169.254.169.254` (cloud metadata).
- [ ] Run → FAIL
- [ ] Implement `check_url(url)`: `urlparse`; scheme in `("http","https")`;
      `socket.getaddrinfo`; every resolved address checked with
      `ipaddress.ip_address` against `is_private`, `is_loopback`,
      `is_link_local`, `is_multicast`, `is_reserved`, `is_unspecified`.
      Raise `ValueError` naming the reason. Comment the DNS-rebinding gap.
- [ ] Run → PASS. Commit.

### Task 3: fetch with re-checked redirects

**Files:** Modify `harness/tools/web.py`, `tests/test_web.py`

- [ ] Write failing tests using a fake client (no sockets): a 200 returns body;
      a 301 chain is followed and the final body returned; **a public URL
      redirecting to `127.0.0.1` is refused** (the guard runs per hop); a chain
      longer than `MAX_REDIRECTS` raises.
- [ ] Run → FAIL
- [ ] Implement `MAX_REDIRECTS = 5` and `fetch(url, client, *, max_redirects)`:
      loop, `check_url` each hop, `client.get(url, follow_redirects=False)`,
      follow `Location` on 3xx, return `(final_url, status, text)`.
- [ ] Run → PASS. Commit.

### Task 4: the `web_fetch` tool

**Files:** Modify `harness/tools/web.py`, `tests/test_web.py`

- [ ] Write failing tests: result is wrapped in the untrusted marker and names
      the *final* URL; long pages are truncated; a timeout and a 404 become
      result text (no raise); a refused URL returns the guard's reason;
      `tool.read_only is True`.
- [ ] Run → FAIL
- [ ] Implement `web_fetch_tool(client=None, char_limit=10000)`: default client
      `httpx.Client(timeout=15, headers={"User-Agent": ...})`; call `fetch`;
      `html_to_text` when the content type is HTML, else raw; `truncate`; wrap.
      Catch `Exception` → error text.
- [ ] Run → PASS. Commit.

### Task 5: the search seam + `web_search` tool

**Files:** Create `harness/search.py`; modify `harness/tools/web.py`,
`tests/test_web.py`

- [ ] Write failing tests: `web_search_tool(fake_provider)` formats
      `title — url` lines plus snippets; an empty result list says so
      explicitly rather than returning ""; a provider raising becomes result
      text; `read_only is True`; `default_provider()` returns `None` when the
      env key is absent (monkeypatch `os.environ`).
- [ ] Run → FAIL
- [ ] Implement `SearchProvider` Protocol in `harness/tools/web.py` and
      `web_search_tool(provider)`. Implement `harness/search.py` with
      `BraveSearch` (endpoint, `X-Subscription-Token`, maps JSON →
      `{"title","url","snippet"}`) and `default_provider()` reading
      `BRAVE_API_KEY`.
- [ ] Run → PASS. Commit.

### Task 6: wire into `main.py`

**Files:** Modify `main.py`

- [ ] Add `web_fetch_tool()` to the base `registry` list.
- [ ] After the registry is built: `provider = default_provider()`; if present,
      `register_builtin("web_search", web_search_tool(provider))` and print
      `(web search: enabled via <name>)`; else print
      `(web search: no API key — set BRAVE_API_KEY to enable)`.
- [ ] Verify by hand: `uv run python main.py --help` still works; `uv run python
      -c "import main"` clean.
- [ ] Run the whole suite → PASS. Commit.

## Self-review checks

- No test opens a socket (grep for `httpx.Client(` in tests — there should be
  none outside a fake).
- `web_fetch` appears in a subagent's registry (it is `read_only`,
  `spawns_subagents=False`) — confirm that is intended and noted.
- The spec's accepted-exfiltration paragraph is reflected in the tool
  description, so the model-facing text does not overclaim safety.
