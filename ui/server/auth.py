"""Per-launch authentication for local HTTP and WebSocket clients."""

from __future__ import annotations

import hmac
import threading

from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import HTTPConnection, Request
from starlette.responses import RedirectResponse
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.websockets import WebSocket


COOKIE_NAME = "harness_ui_session"
WEBSOCKET_PROTOCOL = "harness-ui"
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class LaunchAuth:
    """Authenticate browser and Tauri clients with one per-launch secret."""

    def __init__(self, secret: str, allowed_origins: set[str]):
        if not isinstance(secret, str) or not secret:
            raise ValueError("launch secret must be a non-empty string")
        self._secret = secret
        self._allowed_origins = frozenset(allowed_origins)
        self._bootstrap_used = False
        self._bootstrap_lock = threading.Lock()

    def matches(self, candidate: object) -> bool:
        """Compare a candidate without exposing ordinary equality timing."""
        return isinstance(candidate, str) and hmac.compare_digest(
            self._secret, candidate
        )

    def consume_bootstrap(self, token: object) -> bool:
        """Atomically consume the launch token once for browser bootstrap."""
        with self._bootstrap_lock:
            if self._bootstrap_used or not self.matches(token):
                return False
            self._bootstrap_used = True
            return True

    def bootstrap_response(self, token: object) -> RedirectResponse:
        """Exchange the one-use bootstrap token for a session cookie."""
        if not self.consume_bootstrap(token):
            raise HTTPException(status_code=401, detail="Unauthorized")

        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            self._secret,
            httponly=True,
            samesite="strict",
        )
        return response

    def require_http(self, request: Request) -> None:
        """Require origin protection where needed and a valid HTTP credential."""
        if request.method.upper() not in _SAFE_HTTP_METHODS:
            self._require_origin(request, websocket=False)
        if not self._http_credentials_valid(request):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require_websocket(self, websocket: WebSocket) -> str | None:
        """Validate a socket and return the only subprotocol safe to select."""
        self._require_origin(websocket, websocket=True)

        protocol_headers = websocket.headers.getlist("sec-websocket-protocol")
        if protocol_headers:
            protocols = [
                value.strip()
                for header in protocol_headers
                for value in header.split(",")
            ]
            if (
                len(protocols) == 2
                and protocols[0] == WEBSOCKET_PROTOCOL
                and self.matches(protocols[1])
            ):
                return WEBSOCKET_PROTOCOL
            self._reject_websocket()

        if self._cookie_valid(websocket):
            return None
        self._reject_websocket()

    def _http_credentials_valid(self, request: Request) -> bool:
        authorization = request.headers.getlist("authorization")
        if authorization:
            if len(authorization) != 1:
                return False
            scheme, separator, candidate = authorization[0].partition(" ")
            return (
                separator == " "
                and scheme.casefold() == "bearer"
                and bool(candidate)
                and " " not in candidate
                and self.matches(candidate)
            )
        return self._cookie_valid(request)

    def _cookie_valid(self, connection: HTTPConnection) -> bool:
        candidates: list[str] = []
        for header in connection.headers.getlist("cookie"):
            for field in header.split(";"):
                name, separator, value = field.partition("=")
                if separator and name.strip() == COOKIE_NAME:
                    candidates.append(value.strip())
        return len(candidates) == 1 and self.matches(candidates[0])

    def _require_origin(
        self, connection: HTTPConnection, *, websocket: bool
    ) -> None:
        origins = connection.headers.getlist("origin")
        if len(origins) == 1 and origins[0] in self._allowed_origins:
            return
        if websocket:
            self._reject_websocket()
        raise HTTPException(status_code=403, detail="Forbidden")

    @staticmethod
    def _reject_websocket() -> None:
        raise WebSocketException(
            code=WS_1008_POLICY_VIOLATION,
            reason="Forbidden",
        )
