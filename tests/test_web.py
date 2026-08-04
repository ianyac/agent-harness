"""Lesson 24: web access. Every test is offline — no socket is opened."""

import socket

import pytest

from harness.search import BraveSearch, default_provider
from harness.tools.web import (
    MAX_REDIRECTS,
    UNTRUSTED_FOOTER,
    check_url,
    fetch,
    html_to_text,
    web_fetch_tool,
    web_search_tool,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "text/html"}

    def json(self):
        import json

        return json.loads(self.text)

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Serves a scripted {url: FakeResponse} map; records what was requested."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, follow_redirects=False, **kwargs):
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected fetch of {url}")
        return self.pages[url]


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every host to a public address unless the test says otherwise."""
    mapping = {}

    def fake_getaddrinfo(host, port, *a, **k):
        address = mapping.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return mapping


# --------------------------------------------------------------- html_to_text


def test_html_to_text_drops_script_and_style_with_their_contents():
    html = "<p>keep</p><script>var x = 1;</script><style>.a{color:red}</style>"
    out = html_to_text(html)
    assert "keep" in out
    assert "var x" not in out and "color:red" not in out


def test_html_to_text_strips_tags_and_unescapes_entities():
    assert html_to_text("<h1>A &amp; B</h1>") == "A & B"
    assert html_to_text("<p>&lt;not a tag&gt;</p>") == "<not a tag>"


def test_html_to_text_collapses_blank_runs_and_keeps_paragraphs():
    assert html_to_text("<p>one</p>\n\n\n\n<p>two</p>").count("\n\n") == 1


def test_html_to_text_leaves_plain_text_alone():
    assert html_to_text("just words") == "just words"


# ------------------------------------------------------------------ check_url


def test_check_url_refuses_non_http_schemes(public_dns):
    for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi"):
        with pytest.raises(ValueError, match="only http and https"):
            check_url(url)


def test_check_url_refuses_a_public_name_that_resolves_to_loopback(public_dns):
    # the whole reason the check resolves instead of matching on "localhost":
    # a perfectly public-looking name can point at this machine
    public_dns["totally-normal.com"] = "127.0.0.1"
    with pytest.raises(ValueError, match="private address"):
        check_url("https://totally-normal.com/x")


def test_check_url_refuses_private_and_metadata_addresses(public_dns):
    for host, address in (
        ("a.com", "10.0.0.5"),
        ("b.com", "192.168.1.1"),
        ("c.com", "169.254.169.254"),   # cloud metadata
        ("d.com", "::1"),
    ):
        public_dns[host] = address
        with pytest.raises(ValueError, match="private address"):
            check_url(f"http://{host}/")


def test_check_url_allows_a_public_address(public_dns):
    check_url("https://example.com/page")  # does not raise


def test_check_url_reports_an_unresolvable_host(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="cannot resolve"):
        check_url("https://nx.example/")


# ---------------------------------------------------------------------- fetch


def test_fetch_returns_the_body(public_dns):
    client = FakeClient({"https://ex.com/a": FakeResponse(text="<p>hi</p>")})
    url, status, text, _ = fetch("https://ex.com/a", client)
    assert (url, status, text) == ("https://ex.com/a", 200, "<p>hi</p>")


def test_fetch_follows_a_redirect_chain(public_dns):
    client = FakeClient({
        "https://ex.com/a": FakeResponse(301, headers={"location": "/b"}),
        "https://ex.com/b": FakeResponse(text="done"),
    })
    url, _, text, _ = fetch("https://ex.com/a", client)
    assert url == "https://ex.com/b" and text == "done"   # relative Location resolved


def test_fetch_rechecks_every_hop_so_a_redirect_cannot_reach_localhost(public_dns):
    # the reason redirects are followed by hand: auto-following would let a
    # public URL land on the loopback interface, defeating check_url
    public_dns["internal.example"] = "127.0.0.1"
    client = FakeClient({
        "https://ex.com/a": FakeResponse(302, headers={"location": "http://internal.example/"}),
    })
    with pytest.raises(ValueError, match="private address"):
        fetch("https://ex.com/a", client)


def test_fetch_caps_the_redirect_chain(public_dns):
    pages = {
        f"https://ex.com/{i}": FakeResponse(301, headers={"location": f"/{i + 1}"})
        for i in range(MAX_REDIRECTS + 2)
    }
    with pytest.raises(ValueError, match="more than"):
        fetch("https://ex.com/0", FakeClient(pages))


# -------------------------------------------------------------- web_fetch_tool


def test_web_fetch_marks_the_result_untrusted(public_dns):
    client = FakeClient({"https://ex.com/a": FakeResponse(text="<p>hello</p>")})
    out = web_fetch_tool(client).execute(url="https://ex.com/a")
    assert "untrusted third-party text" in out
    assert "https://ex.com/a" in out
    assert out.endswith(UNTRUSTED_FOOTER)
    assert "hello" in out


def test_web_fetch_names_the_final_url_after_a_redirect(public_dns):
    client = FakeClient({
        "https://ex.com/a": FakeResponse(301, headers={"location": "/b"}),
        "https://ex.com/b": FakeResponse(text="body"),
    })
    out = web_fetch_tool(client).execute(url="https://ex.com/a")
    assert "https://ex.com/b" in out.splitlines()[0]


def test_web_fetch_truncates_a_huge_page(public_dns):
    client = FakeClient({"https://ex.com/a": FakeResponse(text="x" * 50_000)})
    out = web_fetch_tool(client, char_limit=500).execute(url="https://ex.com/a")
    assert "truncated" in out and len(out) < 2000


def test_web_fetch_turns_a_refusal_into_result_text(public_dns):
    public_dns["evil.com"] = "127.0.0.1"
    out = web_fetch_tool(FakeClient({})).execute(url="http://evil.com/")
    assert out.startswith("Error:") and "private address" in out


def test_web_fetch_turns_a_network_failure_into_result_text(public_dns):
    class Boom:
        def get(self, *a, **k):
            raise TimeoutError("timed out")

    out = web_fetch_tool(Boom()).execute(url="https://ex.com/a")
    assert out.startswith("Error fetching") and "TimeoutError" in out


def test_web_fetch_reports_a_non_2xx_status(public_dns):
    client = FakeClient({"https://ex.com/a": FakeResponse(404, text="<p>gone</p>")})
    out = web_fetch_tool(client).execute(url="https://ex.com/a")
    assert "HTTP 404" in out and "gone" in out


def test_web_fetch_is_read_only():
    # deliberate: it works in plan/readOnly turns. The outbound channel that
    # comes with that is the accepted tradeoff recorded in the spec.
    tool = web_fetch_tool(FakeClient({}))
    assert tool.read_only is True and tool.spawns_subagents is False


# ------------------------------------------------------------- web_search_tool


class FakeProvider:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error

    def search(self, query, count=5):
        if self.error:
            raise self.error
        return self.results[:count]


def test_web_search_formats_results():
    provider = FakeProvider([
        {"title": "Docs", "url": "https://d.com", "snippet": "how to"},
    ])
    out = web_search_tool(provider).execute(query="how to")
    assert "- Docs — https://d.com" in out and "how to" in out


def test_web_search_states_an_empty_result_set():
    out = web_search_tool(FakeProvider([])).execute(query="zzz")
    assert "No results" in out   # an empty string would read as a broken tool


def test_web_search_turns_a_provider_failure_into_result_text():
    out = web_search_tool(FakeProvider(error=RuntimeError("503"))).execute(query="x")
    assert out.startswith("Error searching") and "503" in out


def test_web_search_is_read_only():
    assert web_search_tool(FakeProvider()).read_only is True


# ------------------------------------------------------------------- provider


def test_default_provider_is_none_without_a_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert default_provider() is None   # no key → no tool, not a broken tool


def test_default_provider_builds_one_with_a_key(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    assert isinstance(default_provider(), BraveSearch)


def test_brave_maps_the_api_payload_to_the_protocol_shape():
    import json

    payload = json.dumps({"web": {"results": [
        {"title": "T", "url": "https://u", "description": "S", "extra": "ignored"},
    ]}})

    class OneShot:
        def get(self, url, **kwargs):
            self.sent = kwargs
            return FakeResponse(text=payload, headers={"content-type": "application/json"})

    client = OneShot()
    # the API's `description` is normalised to the protocol's `snippet`
    assert BraveSearch("k", client=client).search("q") == [
        {"title": "T", "url": "https://u", "snippet": "S"}
    ]
    assert client.sent["headers"]["X-Subscription-Token"] == "k"
