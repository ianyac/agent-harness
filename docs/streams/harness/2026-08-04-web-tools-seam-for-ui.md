# New tools the ui lane can pick up (lesson 24), and where the ui registry now stands

**From:** harness lane · **Date:** 2026-08-04 · **Context:** lesson 24 adds web
access. Nothing here is a breaking change — these are **additive** tools plus a
status note. Spec:
`docs/superpowers/specs/2026-08-04-web-access-design.md`.

## 1. Two new tools, both optional to adopt

```python
from harness.tools.web import web_fetch_tool, web_search_tool
from harness.search import default_provider

registry = [..., web_fetch_tool()]              # always available

provider = default_provider()                    # None unless BRAVE_API_KEY is set
if provider is not None:
    tools["web_search"] = web_search_tool(provider)
```

- `web_fetch_tool(client=None, char_limit=10000)` — fetch a URL, convert HTML to
  text, truncate, wrap in an untrusted-content marker. Pass your own `client`
  (anything with `.get(url, follow_redirects=False)`) to test without sockets.
- `web_search_tool(provider)` — needs a `SearchProvider`
  (`search(query, count) -> [{"title","url","snippet"}]`). Register it **only**
  when a provider exists; a missing key means a missing tool, not a broken one.

Both are `read_only=True` and `spawns_subagents=False`, so they flow to
subagents like any read-only tool.

## 2. Two things to know before adopting

**The sandbox does not apply.** A fetch runs in-process, so the OS sandbox —
whose profile says `deny network*` — never sees it. `harness/tools/web.py`
carries its own guard (`check_url`) refusing any URL that *resolves* into
private address space, re-checked on every redirect hop. If the ui lane wraps
or replaces the fetch path, that guard is the only thing standing between the
model and `localhost` / `169.254.169.254`.

**Exfiltration is an open channel, by decision.** `read_only=True` means a
fetch happens with no prompt, in any mode, including from a subagent that
cannot ask. A URL carries data outward. This was chosen over per-call consent
and over a domain allowlist (spec, decisions table). If the ui surfaces a
permission UI, this is the tool where "read-only" is most misleading, and worth
labelling as network egress rather than as a read.

## 3. Status note: the ui registry is behind

Observed in a running ui session: the permission gate lists `read_file`,
`write_file`, `list_dir`, `bash`, `agent`. Missing relative to `main.py`:

| tool | lesson |
|---|---|
| `skill` | 18–21 (executing skill tool, args, fork, slash) |
| `exit_plan_mode` | 22 |
| `web_fetch` | 24 |

Not a defect — the ui lane owns its registry — but the two earlier seam notes
(`2026-07-11-plan-mode-seam-changes-for-ui.md`,
`2026-08-03-subagent-registry-seam-for-ui.md`) describe changes that only bite
once `skill` is registered there, so they can be absorbed in one pass together
with these tools.
