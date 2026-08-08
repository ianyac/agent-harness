"""Outer authentication, browser bootstrap, origin, and CORS boundary."""

from __future__ import annotations

import hmac
import secrets
import threading
from urllib.parse import urlencode

from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket


WEBSOCKET_PROTOCOL = "harness-ui"
STATIC_PATH_PREFIX = "/_app/"
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_STATIC_HTTP_METHODS = frozenset({"GET", "HEAD"})
_CORS_METHODS = ("GET", "HEAD", "POST", "PATCH", "DELETE", "OPTIONS")
_CORS_HEADERS = ("Authorization", "Content-Type")
_CORS_HEADER_NAMES = frozenset(value.casefold() for value in _CORS_HEADERS)
_REFERRER_POLICY = "no-referrer"


class LaunchAuth:
    """Authenticate one Tauri launch or one exchanged browser launch."""

    def __init__(self, secret: str, allowed_origins: set[str]):
        if not isinstance(secret, str) or not secret:
            raise ValueError("launch secret must be a non-empty string")
        self._secret = secret
        self._allowed_origins = frozenset(allowed_origins)
        self._bootstrap_used = False
        self._launch_secret_enabled = True
        self._browser_static_token: str | None = None
        self._browser_api_token: str | None = None
        self._bootstrap_lock = threading.Lock()

    @staticmethod
    def _matches(expected: str, candidate: object) -> bool:
        if not isinstance(candidate, str):
            return False
        try:
            return hmac.compare_digest(expected, candidate)
        except TypeError:
            # compare_digest's str form rejects non-ASCII text. Credentials are
            # untrusted decoded input, so unsupported text is simply invalid.
            return False

    def matches(self, candidate: object) -> bool:
        """Compare a bootstrap candidate without ordinary equality timing."""
        return self._matches(self._secret, candidate)

    def _new_capability(self, excluded: set[str]) -> str:
        while True:
            candidate = secrets.token_urlsafe(32)
            if candidate and candidate not in excluded:
                return candidate

    def consume_bootstrap(self, token: object) -> bool:
        """Atomically exchange the launch secret and retire it from reuse."""
        with self._bootstrap_lock:
            if self._bootstrap_used or not self.matches(token):
                return False
            static_token = self._new_capability({self._secret})
            api_token = self._new_capability({self._secret, static_token})
            self._browser_static_token = static_token
            self._browser_api_token = api_token
            self._launch_secret_enabled = False
            self._bootstrap_used = True
            return True

    def bootstrap_response(self, token: object) -> RedirectResponse:
        """Exchange the one-use token for separate path and API capabilities."""
        if not self.consume_bootstrap(token):
            raise HTTPException(status_code=401, detail="Unauthorized")

        with self._bootstrap_lock:
            static_token = self._browser_static_token
            api_token = self._browser_api_token
        if static_token is None or api_token is None:  # pragma: no cover - invariant
            raise RuntimeError("browser bootstrap state is unavailable")
        fragment = urlencode({"token": api_token})
        return RedirectResponse(
            url=f"{STATIC_PATH_PREFIX}{static_token}/#{fragment}",
            status_code=303,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": _REFERRER_POLICY,
            },
        )

    def browser_static_path(self, path: str) -> str | None:
        """Return a relative static path only for the current path capability."""
        if not path.startswith(STATIC_PATH_PREFIX):
            return None
        token_and_path = path[len(STATIC_PATH_PREFIX) :]
        token, separator, relative = token_and_path.partition("/")
        if not token:
            return None
        with self._bootstrap_lock:
            expected = self._browser_static_token
        if expected is None or not self._matches(expected, token):
            return None
        return relative if separator else ""

    def require_http(self, request: Request) -> None:
        """Require an exact origin where applicable and one bearer capability."""
        if request.method.upper() not in _SAFE_HTTP_METHODS:
            self.require_origin(request)
        else:
            self.validate_optional_origin(request)
        if not self._http_credentials_valid(request):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require_websocket(self, websocket: WebSocket) -> str:
        """Validate a socket and return the only subprotocol safe to select."""
        try:
            self.require_origin(websocket)
        except HTTPException:
            self._reject_websocket()

        protocol_headers = websocket.headers.getlist("sec-websocket-protocol")
        protocols = [
            value.strip()
            for header in protocol_headers
            for value in header.split(",")
        ]
        if (
            len(protocols) == 2
            and protocols[0] == WEBSOCKET_PROTOCOL
            and self._api_credential_matches(protocols[1])
        ):
            return WEBSOCKET_PROTOCOL
        self._reject_websocket()

    def validate_optional_origin(self, connection: HTTPConnection) -> str | None:
        """Accept no Origin or one exact configured Origin."""
        origins = connection.headers.getlist("origin")
        if not origins:
            return None
        if len(origins) == 1 and origins[0] in self._allowed_origins:
            return origins[0]
        raise HTTPException(status_code=403, detail="Forbidden")

    def require_origin(self, connection: HTTPConnection) -> str:
        """Require exactly one configured Origin."""
        origins = connection.headers.getlist("origin")
        if len(origins) == 1 and origins[0] in self._allowed_origins:
            return origins[0]
        raise HTTPException(status_code=403, detail="Forbidden")

    def allowed_origin(self, connection: HTTPConnection) -> str | None:
        """Return an exact configured Origin without accepting malformed input."""
        origins = connection.headers.getlist("origin")
        if len(origins) == 1 and origins[0] in self._allowed_origins:
            return origins[0]
        return None

    def _http_credentials_valid(self, request: Request) -> bool:
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1:
            return False
        scheme, separator, candidate = authorization[0].partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and bool(candidate)
            and " " not in candidate
            and self._api_credential_matches(candidate)
        )

    def _api_credential_matches(self, candidate: object) -> bool:
        with self._bootstrap_lock:
            launch_secret_enabled = self._launch_secret_enabled
            browser_api_token = self._browser_api_token
        browser_match = (
            browser_api_token is not None
            and self._matches(browser_api_token, candidate)
        )
        launch_match = launch_secret_enabled and self.matches(candidate)
        return browser_match or launch_match

    @staticmethod
    def _reject_websocket() -> None:
        raise WebSocketException(
            code=WS_1008_POLICY_VIOLATION,
            reason="Forbidden",
        )


class AuthBoundary:
    """Authenticate HTTP and WebSocket scopes before routing or body parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: LaunchAuth,
        static_enabled: bool,
    ) -> None:
        self._app = app
        self._auth = auth
        self._static_enabled = static_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive=receive)
        is_api = self._is_api_path(scope.get("path", ""))
        cors_origin = self._auth.allowed_origin(request) if is_api else None

        if self._is_bootstrap_exchange(scope):
            await self._app(
                scope,
                receive,
                self._secured_send(send, cors_origin=None),
            )
            return

        if self._is_cors_preflight(request, is_api=is_api):
            try:
                origin = self._auth.require_origin(request)
                self._validate_preflight(request)
            except HTTPException as error:
                await self._send_http_error(
                    scope,
                    receive,
                    send,
                    error,
                    cors_origin=cors_origin,
                )
                return
            response = Response(
                status_code=204,
                headers=self._cors_preflight_headers(origin),
            )
            await response(
                scope,
                receive,
                self._secured_send(send, cors_origin=None),
            )
            return

        static_path = (
            self._auth.browser_static_path(scope.get("path", ""))
            if self._static_enabled
            else None
        )
        try:
            if static_path is not None:
                if request.method.upper() in _STATIC_HTTP_METHODS:
                    self._auth.validate_optional_origin(request)
                else:
                    self._auth.require_origin(request)
                state = scope.setdefault("state", {})
                state["browser_static_path"] = static_path
            else:
                self._auth.require_http(request)
        except HTTPException as error:
            await self._send_http_error(
                scope,
                receive,
                send,
                error,
                cors_origin=cors_origin,
            )
            return

        await self._app(
            scope,
            receive,
            self._secured_send(send, cors_origin=cors_origin if is_api else None),
        )

    async def _websocket(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        websocket = WebSocket(scope, receive=receive, send=send)
        try:
            selected = self._auth.require_websocket(websocket)
        except WebSocketException as error:
            await send(
                {
                    "type": "websocket.close",
                    "code": error.code,
                    "reason": error.reason or "Forbidden",
                }
            )
            return
        state = scope.setdefault("state", {})
        state["auth_websocket_subprotocol"] = selected
        await self._app(scope, receive, send)

    @staticmethod
    def _is_api_path(path: str) -> bool:
        return path == "/api" or path.startswith("/api/")

    @staticmethod
    def _is_bootstrap_exchange(scope: Scope) -> bool:
        if scope.get("method", "").upper() != "GET":
            return False
        if scope.get("path") != "/bootstrap":
            return False
        raw_path = scope.get("raw_path")
        return raw_path is None or raw_path == b"/bootstrap"

    @staticmethod
    def _is_cors_preflight(request: Request, *, is_api: bool) -> bool:
        return (
            is_api
            and request.method.upper() == "OPTIONS"
            and bool(request.headers.getlist("access-control-request-method"))
        )

    @staticmethod
    def _validate_preflight(request: Request) -> None:
        methods = request.headers.getlist("access-control-request-method")
        if len(methods) != 1 or methods[0].upper() not in _CORS_METHODS:
            raise HTTPException(status_code=403, detail="Forbidden")
        requested_header_fields = request.headers.getlist(
            "access-control-request-headers"
        )
        if len(requested_header_fields) > 1:
            raise HTTPException(status_code=403, detail="Forbidden")
        if not requested_header_fields:
            return
        names = [
            value.strip().casefold()
            for value in requested_header_fields[0].split(",")
        ]
        if (
            not names
            or any(not name for name in names)
            or len(names) != len(set(names))
            or any(name not in _CORS_HEADER_NAMES for name in names)
        ):
            raise HTTPException(status_code=403, detail="Forbidden")

    @staticmethod
    def _cors_preflight_headers(origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": ", ".join(_CORS_METHODS),
            "Access-Control-Allow-Headers": ", ".join(_CORS_HEADERS),
            "Access-Control-Max-Age": "600",
            "Vary": (
                "Origin, Access-Control-Request-Method, "
                "Access-Control-Request-Headers"
            ),
        }

    async def _send_http_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        error: HTTPException,
        *,
        cors_origin: str | None,
    ) -> None:
        response = JSONResponse(
            status_code=error.status_code,
            content={"detail": error.detail},
            headers=error.headers,
        )
        await response(
            scope,
            receive,
            self._secured_send(send, cors_origin=cors_origin),
        )

    @staticmethod
    def _secured_send(send: Send, *, cors_origin: str | None) -> Send:
        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                AuthBoundary._set_header(
                    headers,
                    b"referrer-policy",
                    _REFERRER_POLICY.encode("latin-1"),
                )
                if cors_origin is not None:
                    AuthBoundary._set_header(
                        headers,
                        b"access-control-allow-origin",
                        cors_origin.encode("latin-1"),
                    )
                    AuthBoundary._merge_vary(headers, "Origin")
                message = {**message, "headers": headers}
            await send(message)

        return secured

    @staticmethod
    def _set_header(
        headers: list[tuple[bytes, bytes]],
        name: bytes,
        value: bytes,
    ) -> None:
        headers[:] = [
            (key, existing)
            for key, existing in headers
            if key.lower() != name
        ]
        headers.append((name, value))

    @staticmethod
    def _merge_vary(headers: list[tuple[bytes, bytes]], value: str) -> None:
        existing_values = [
            existing.decode("latin-1")
            for key, existing in headers
            if key.lower() == b"vary"
        ]
        tokens = [
            token.strip()
            for existing in existing_values
            for token in existing.split(",")
            if token.strip()
        ]
        if value.casefold() not in {token.casefold() for token in tokens}:
            tokens.append(value)
        AuthBoundary._set_header(
            headers,
            b"vary",
            ", ".join(tokens).encode("latin-1"),
        )
