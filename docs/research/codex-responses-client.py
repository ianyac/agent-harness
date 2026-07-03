from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator
import httpx

from openai import AsyncAPIResponse


DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)


class AsyncResponsesClient:
    """Async ``/v1/responses`` client.

    Example::

        async with AsyncResponsesClient("lmstudio") as client:
            resp = await client.create(model="openai/gpt-oss-20b", input="hi")
            print(resp.output_text)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        referer: str
        | None = None,  # OpenRouter HTTP-Referer (optional ranking metadata)
        title: str | None = None,  # OpenRouter X-Title (optional ranking metadata)
        default_headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
        max_retries: int = 3,
        retry_base: float = 0.5,
        retry_max: float = 8.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Resolve the provider preset, API key, base URL, and base request headers.

        The API key (a string or a :class:`Secret`) is wrapped/kept as an opaque
        ``Secret`` and stored privately as ``_api_key``; it is revealed only when a
        request's ``Authorization`` header is built. Raises ValueError if the provider
        requires a key but none is supplied (via ``api_key`` or its env var).
        ``create`` / ``stream`` retry transient failures (429, 5xx, network errors) up to
        ``max_retries`` times with jittered exponential backoff (``retry_base`` →
        ``retry_max`` seconds), honoring a ``Retry-After`` header; 4xx errors are not retried.
        """
        self.base_url = (base_url or self.config.base_url).rstrip("/")

        if self.config.requires_key and self._api_key is None:
            raise ValueError(
                f"Provider {self.config.name!r} requires an API key. "
                f"Pass api_key=... or set ${self.config.api_key_env}."
            )

        # Base headers never include Authorization; it is added per request so the
        # plaintext key is not retained on the client object.
        headers = dict(self.config.default_headers)
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        if default_headers:
            headers.update(default_headers)
        self._headers = headers

        self._timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self._max_retries = max_retries
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ lifecycle
    def _ensure_client(self) -> httpx.AsyncClient:
        """Return the httpx client, lazily creating an owned one if none was injected."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client if this instance owns it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> AsyncResponsesClient:
        """Enter the async context manager, ensuring an httpx client exists."""
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the async context manager, closing the httpx client if owned."""
        await self.aclose()

    @property
    def url(self) -> str:
        """The full ``/responses`` endpoint URL for the configured provider."""
        return f"{self.base_url}/responses"

    def _request_headers(self) -> dict[str, str]:
        """Build per-request headers, revealing the API key into Authorization only here."""
        headers = dict(self._headers)
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.reveal()}"
        return headers

    def _scrub(self, text: str) -> str:
        """Mask this client's own API key if a provider reflected it in returned text."""
        if self._api_key is not None:
            secret = self._api_key.reveal()
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """Sleep before a retry: jittered exponential backoff, honoring ``Retry-After``."""
        delay = min(self._retry_base * 2**attempt, self._retry_max)
        delay *= 0.5 + random.random() * 0.5  # 50–100% jitter to avoid thundering herds
        if retry_after:
            try:
                delay = max(
                    delay, float(retry_after)
                )  # honor a seconds Retry-After as a floor
            except ValueError:
                pass  # an HTTP-date Retry-After — fall back to the computed delay
        await asyncio.sleep(delay)

    # -------------------------------------------------------------- request body
    def build_body(
        self,
        *,
        model: str,
        input: str | list[dict[str, Any]],
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        reasoning: dict[str, Any] | None = None,
        text_format: dict[str, Any] | None = None,
        text: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        previous_response_id: str | None = None,
        parallel_tool_calls: bool | None = None,
        store: bool | None = None,
        extra: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Assemble a Responses-API request body, omitting unset fields.

        ``text_format`` (from :func:`messages.json_schema_format`) is wrapped into
        ``text.format`` for structured output. ``extra`` is merged last for provider-
        specific or not-yet-modeled fields.
        """
        if self.config.requires_list_input and isinstance(input, str):
            input = [{"role": "user", "content": input}]
        body: dict[str, Any] = {"model": model, "input": input}
        if instructions is not None:
            body["instructions"] = instructions
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if max_output_tokens is not None:
            body["max_output_tokens"] = max_output_tokens
        if reasoning is not None:
            body["reasoning"] = reasoning

        merged_text = dict(text) if text else {}
        if text_format is not None:
            merged_text["format"] = text_format
        if merged_text:
            body["text"] = merged_text

        if metadata is not None:
            body["metadata"] = metadata
        if previous_response_id is not None:
            body["previous_response_id"] = previous_response_id
        if parallel_tool_calls is not None:
            body["parallel_tool_calls"] = parallel_tool_calls
        if store is None:
            store = self.config.default_store
        if store is not None:
            body["store"] = store
        # Stateless reasoning continuity: with store=false the server keeps no reasoning state,
        # so a reasoning item echoed back on the next turn must carry its *encrypted* content —
        # request it here (codex/gpt-5.x). Without it the model returns a content-less reasoning
        # item, and echoing that back hangs the follow-up turn. Harmless on non-reasoning calls.
        if store is False and reasoning is not None and "include" not in body:
            body["include"] = ["reasoning.encrypted_content"]
        if extra:
            body.update(extra)
        if stream:
            body["stream"] = True
        return body

    # ------------------------------------------------------------------ requests
    async def create(self, **kwargs: Any) -> Response:
        """Non-streaming call. Returns the parsed :class:`Response`.

        Stream-only providers (``config.stream_only``, e.g. Codex, which rejects
        non-streaming requests) are served by consuming a stream internally and
        returning its final aggregated response, so callers need not special-case them.
        """
        if self.config.stream_only:
            return await self._create_via_stream(**kwargs)
        body = self.build_body(stream=False, **kwargs)
        client = self._ensure_client()
        for attempt in range(self._max_retries + 1):
            try:
                resp = await client.post(
                    self.url, headers=self._request_headers(), json=body
                )
            except httpx.TransportError:  # connect/read/timeout — transient
                if attempt >= self._max_retries:
                    raise
                await self._backoff(attempt, None)
                continue
            if resp.status_code >= 400:
                if (
                    resp.status_code in _RETRYABLE_STATUS
                    and attempt < self._max_retries
                ):
                    await self._backoff(attempt, resp.headers.get("retry-after"))
                    continue
                raise ResponsesError(
                    resp.status_code,
                    self._scrub(resp.text),
                    provider=self.config.name,
                )
            return Response(resp.json())
        raise RuntimeError("unreachable: retry loop exited")  # pragma: no cover

    async def _create_via_stream(self, **kwargs: Any) -> Response:
        """Drive a streaming request and return only the final aggregated response.

        Backs :meth:`create` for stream-only providers. The terminal event's response
        already has its ``output`` backfilled by :meth:`stream`.
        """
        final: Response | None = None
        async for event in self.stream(**kwargs):
            if event.type in _TERMINAL_EVENT_TYPES and event.response is not None:
                final = event.response
        if final is None:
            raise RuntimeError(
                f"[{self.config.name}] stream ended without a terminal response event"
            )
        return final

    async def list_models(self) -> list[str]:
        """Fetch the available model ids from the provider's ``/models`` endpoint.

        Handles both the OpenAI shape (``{"data": [{"id": ...}]}``) and Codex's
        (``{"models": [{"slug": ...}]}``), and sends any provider-specific query params
        (Codex needs a ``client_version``).
        """
        client = self._ensure_client()
        resp = await client.get(
            f"{self.base_url}/models",
            headers=self._request_headers(),
            params=self.config.models_query or None,
        )
        if resp.status_code >= 400:
            raise ResponsesError(
                resp.status_code,
                self._scrub(resp.text),
                provider=self.config.name,
            )
        data = resp.json()
        items = data.get("data") or data.get("models") or []
        return [
            mid
            for m in items
            if isinstance(m, dict) and (mid := m.get("id") or m.get("slug"))
        ]

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        """Streaming call. Yields :class:`StreamEvent` objects as they arrive.

        Output items streamed via ``response.output_item.done`` are accumulated and
        backfilled into the terminal event when a provider reports an empty ``output``
        there (Codex does this), so ``StreamEvent.response.output`` — and everything
        derived from it — is correct regardless of provider.
        """
        body = self.build_body(stream=True, **kwargs)
        client = self._ensure_client()
        yielded = False  # once events are out, a mid-stream failure can't be retried
        for attempt in range(self._max_retries + 1):
            try:
                async with client.stream(
                    "POST", self.url, headers=self._request_headers(), json=body
                ) as resp:
                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        if (
                            resp.status_code in _RETRYABLE_STATUS
                            and attempt < self._max_retries
                        ):
                            await self._backoff(
                                attempt, resp.headers.get("retry-after")
                            )
                            continue
                        raise ResponsesError(
                            resp.status_code,
                            self._scrub(raw.decode("utf-8", "replace")),
                            provider=self.config.name,
                        )
                    output_items: list[dict[str, Any]] = []
                    async for event in _iter_sse(resp):
                        if event.type == "response.output_item.done":
                            item = event.data.get("item")
                            if isinstance(item, dict):
                                output_items.append(item)
                        elif event.type in _TERMINAL_EVENT_TYPES:
                            _backfill_output(event.data, output_items)
                        yielded = True
                        yield event
                    return
            except httpx.TransportError:  # connect/read error before/at open
                if yielded or attempt >= self._max_retries:
                    raise
                await self._backoff(attempt, None)
                continue
