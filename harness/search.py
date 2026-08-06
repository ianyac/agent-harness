"""A search provider adapter, and the factory that decides whether the harness
has search at all.

Same shape as harness/llm.py: a Protocol lives with its consumer
(tools/web.py), the concrete backend lives here, and the factory returns None
rather than raising when the key is absent — a missing capability is not an
error, it is one fewer tool (the mcp.json rule).
"""

import os

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearch:
    """Brave Search API. Chosen for a plain REST + header-key interface; any
    backend returning {"title", "url", "snippet"} dicts substitutes freely."""

    name = "brave"

    def __init__(self, api_key: str, client=None, timeout: int = 15):
        self.api_key = api_key
        self._client = client
        self.timeout = timeout

    def search(self, query: str, count: int = 5) -> list[dict]:
        if self._client is not None:
            return self._parse(self._get(self._client, query, count), count)
        import httpx

        # closed on the way out, like harness/llm.py — a client per call that
        # is never closed leaks sockets over a long session
        with httpx.Client(timeout=self.timeout) as client:
            return self._parse(self._get(client, query, count), count)

    def _get(self, client, query: str, count: int):
        response = client.get(
            BRAVE_ENDPOINT,
            params={"q": query, "count": count},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
        )
        response.raise_for_status()
        return response

    def _parse(self, response, count: int) -> list[dict]:
        payload = response.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                # the API calls it `description`; normalise to the protocol's name
                "snippet": item.get("description", ""),
            }
            for item in payload.get("web", {}).get("results", [])[:count]
        ]


def default_provider():
    """The configured provider, or None when no key is set. None means the
    harness registers no web_search tool — the model is never offered a
    capability that would fail on every call."""
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return None
    return BraveSearch(key)
