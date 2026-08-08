from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import Request
from starlette.websockets import WebSocket

from server.auth import LaunchAuth


LOOPBACK_ORIGIN = "http://127.0.0.1:8000"
TAURI_ORIGIN = "tauri://localhost"
SECRET = "url-safe_secret"


def request(
    method: str = "GET", *, headers: list[tuple[str, str]] | None = None
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": "/api/sessions",
            "raw_path": b"/api/sessions",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers or []
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
    )


def websocket(*, headers: list[tuple[str, str]] | None = None) -> WebSocket:
    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(_: dict) -> None:
        return None

    return WebSocket(
        {
            "type": "websocket",
            "scheme": "ws",
            "path": "/ws/sessions/s1",
            "raw_path": b"/ws/sessions/s1",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers or []
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
            "subprotocols": [],
        },
        receive,
        send,
    )


@pytest.fixture
def auth() -> LaunchAuth:
    return LaunchAuth(SECRET, {LOOPBACK_ORIGIN, TAURI_ORIGIN})


def test_bootstrap_token_is_single_use():
    auth = LaunchAuth("secret", {LOOPBACK_ORIGIN})

    assert auth.consume_bootstrap("secret") is True
    assert auth.consume_bootstrap("secret") is False


def test_invalid_bootstrap_attempt_does_not_consume_the_token(auth: LaunchAuth):
    assert auth.consume_bootstrap("wrong") is False
    assert auth.consume_bootstrap(SECRET) is True


def test_non_ascii_bootstrap_token_fails_closed_without_consuming_secret(
    auth: LaunchAuth,
):
    assert auth.consume_bootstrap("é") is False

    with pytest.raises(HTTPException) as raised:
        auth.bootstrap_response("é")

    assert raised.value.status_code == 401
    assert raised.value.detail == "Unauthorized"
    assert "é" not in repr(raised.value)
    assert SECRET not in repr(raised.value)
    assert auth.consume_bootstrap(SECRET) is True


def test_bootstrap_consumption_is_atomic(auth: LaunchAuth):
    workers = 16
    gate = threading.Barrier(workers)

    def consume() -> bool:
        gate.wait()
        return auth.consume_bootstrap(SECRET)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _: consume(), range(workers)))

    assert results.count(True) == 1
    assert results.count(False) == workers - 1


def test_tokens_are_compared_with_compare_digest(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "server.auth.hmac.compare_digest",
        lambda expected, candidate: seen.append((expected, candidate)) or True,
    )

    assert LaunchAuth("secret", set()).matches("candidate")
    assert seen == [("secret", "candidate")]


def test_bootstrap_response_redirects_without_a_credential_cookie(auth: LaunchAuth):
    response = auth.bootstrap_response(SECRET)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/_app/")
    assert "#token=" in response.headers["location"]
    assert "set-cookie" not in response.headers
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("token", ["wrong", SECRET])
def test_bootstrap_response_rejects_invalid_or_reused_tokens_without_disclosure(
    auth: LaunchAuth, token: str
):
    if token == SECRET:
        assert auth.consume_bootstrap(SECRET)

    with pytest.raises(HTTPException) as raised:
        auth.bootstrap_response(token)

    assert raised.value.status_code == 401
    assert SECRET not in str(raised.value)
    assert token not in str(raised.value)
    assert SECRET not in repr(raised.value)
    assert token not in repr(raised.value)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_http_accepts_bearer_without_origin_for_safe_methods(
    auth: LaunchAuth, method: str
):
    assert (
        auth.require_http(
            request(
                method,
                headers=[("Authorization", f"Bearer {SECRET}")],
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT"]
)
def test_http_accepts_allowed_origin_for_unsafe_method(
    auth: LaunchAuth, method: str
):
    assert (
        auth.require_http(
            request(
                method,
                headers=[
                    ("Authorization", f"Bearer {SECRET}"),
                    ("Origin", TAURI_ORIGIN),
                ],
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Bearer wrong")],
        [("Authorization", SECRET)],
        [("Authorization", f"Basic {SECRET}")],
        [("Authorization", f"Bearer {SECRET} trailing")],
        [("Cookie", f"not_harness_ui_session={SECRET}")],
    ],
)
def test_http_rejects_missing_or_malformed_credentials_without_disclosure(
    auth: LaunchAuth, headers: list[tuple[str, str]]
):
    with pytest.raises(HTTPException) as raised:
        auth.require_http(request(headers=headers))

    assert raised.value.status_code == 401
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


@pytest.mark.parametrize(
    "headers",
    [
        [("Authorization", "Bearer é")],
        [("Cookie", "harness_ui_session=é")],
    ],
)
def test_non_ascii_http_credentials_fail_closed(
    auth: LaunchAuth, headers: list[tuple[str, str]]
):
    with pytest.raises(HTTPException) as raised:
        auth.require_http(request(headers=headers))

    assert raised.value.status_code == 401
    assert raised.value.detail == "Unauthorized"
    assert "é" not in repr(raised.value)
    assert SECRET not in repr(raised.value)


@pytest.mark.parametrize(
    "authorization_headers",
    [
        [
            ("Authorization", f"Bearer {SECRET}"),
            ("Authorization", f"Bearer {SECRET}"),
        ],
        [
            ("Authorization", f"Bearer {SECRET}"),
            ("Authorization", "Bearer wrong"),
            ("Cookie", f"harness_ui_session={SECRET}"),
        ],
    ],
)
def test_http_rejects_repeated_authorization_fields_without_cookie_fallback(
    auth: LaunchAuth, authorization_headers: list[tuple[str, str]]
):
    with pytest.raises(HTTPException) as raised:
        auth.require_http(request(headers=authorization_headers))

    assert raised.value.status_code == 401


@pytest.mark.parametrize(
    "cookie_headers",
    [
        [
            (
                "Cookie",
                f"harness_ui_session={SECRET}; harness_ui_session={SECRET}",
            )
        ],
        [
            ("Cookie", f"harness_ui_session={SECRET}"),
            ("Cookie", f"harness_ui_session={SECRET}"),
        ],
    ],
)
def test_http_rejects_duplicate_session_cookie_names(
    auth: LaunchAuth, cookie_headers: list[tuple[str, str]]
):
    with pytest.raises(HTTPException) as raised:
        auth.require_http(request(headers=cookie_headers))

    assert raised.value.status_code == 401


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT"]
)
@pytest.mark.parametrize(
    "origin_headers",
    [
        [],
        [("Origin", "null")],
        [("Origin", "http://127.0.0.1:8000.evil.example")],
        [("Origin", "http://127.0.0.1:8000/")],
        [("Origin", LOOPBACK_ORIGIN), ("Origin", TAURI_ORIGIN)],
    ],
)
def test_unsafe_http_requires_one_exact_allowed_origin(
    auth: LaunchAuth, method: str, origin_headers: list[tuple[str, str]]
):
    with pytest.raises(HTTPException) as raised:
        auth.require_http(
            request(
                method,
                headers=[("Authorization", f"Bearer {SECRET}"), *origin_headers],
            )
        )

    assert raised.value.status_code == 403
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


def test_websocket_rejects_browser_cookie_credentials(auth: LaunchAuth):
    with pytest.raises(WebSocketException) as raised:
        auth.require_websocket(
            websocket(
                headers=[
                    ("Origin", LOOPBACK_ORIGIN),
                    ("Cookie", f"harness_ui_session={SECRET}"),
                ]
            )
        )

    assert raised.value.code == 1008


def test_websocket_accepts_tauri_secret_only_after_public_protocol(auth: LaunchAuth):
    selected = auth.require_websocket(
        websocket(
            headers=[
                ("Origin", TAURI_ORIGIN),
                ("Sec-WebSocket-Protocol", f"harness-ui, {SECRET}"),
            ]
        )
    )

    assert selected == "harness-ui"
    assert SECRET not in selected


def test_websocket_combines_repeated_protocol_headers_in_wire_order(auth: LaunchAuth):
    selected = auth.require_websocket(
        websocket(
            headers=[
                ("Origin", TAURI_ORIGIN),
                ("Sec-WebSocket-Protocol", "harness-ui"),
                ("Sec-WebSocket-Protocol", SECRET),
            ]
        )
    )

    assert selected == "harness-ui"


@pytest.mark.parametrize(
    "protocol_headers",
    [
        [("Sec-WebSocket-Protocol", f"  harness-ui\t,\t{SECRET}  ")],
        [
            ("Sec-WebSocket-Protocol", " harness-ui "),
            ("Sec-WebSocket-Protocol", f"\t{SECRET}\t"),
        ],
    ],
)
def test_websocket_accepts_optional_whitespace_around_protocol_values(
    auth: LaunchAuth, protocol_headers: list[tuple[str, str]]
):
    selected = auth.require_websocket(
        websocket(headers=[("Origin", TAURI_ORIGIN), *protocol_headers])
    )

    assert selected == "harness-ui"


@pytest.mark.parametrize(
    "protocol_headers",
    [
        [("Sec-WebSocket-Protocol", f"harness-ui, {SECRET}, {SECRET}")],
        [
            ("Sec-WebSocket-Protocol", "harness-ui"),
            ("Sec-WebSocket-Protocol", SECRET),
            ("Sec-WebSocket-Protocol", SECRET),
        ],
    ],
)
def test_websocket_rejects_duplicate_secret_protocol_values(
    auth: LaunchAuth, protocol_headers: list[tuple[str, str]]
):
    with pytest.raises(WebSocketException) as raised:
        auth.require_websocket(
            websocket(headers=[("Origin", TAURI_ORIGIN), *protocol_headers])
        )

    assert raised.value.code == 1008


@pytest.mark.parametrize(
    "origin_headers",
    [
        [],
        [("Origin", "null")],
        [("Origin", "https://example.com")],
        [("Origin", LOOPBACK_ORIGIN), ("Origin", TAURI_ORIGIN)],
    ],
)
def test_every_websocket_requires_one_exact_allowed_origin(
    auth: LaunchAuth, origin_headers: list[tuple[str, str]]
):
    with pytest.raises(WebSocketException) as raised:
        auth.require_websocket(
            websocket(
                headers=[
                    *origin_headers,
                    ("Cookie", f"harness_ui_session={SECRET}"),
                ]
            )
        )

    assert raised.value.code == 1008
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


@pytest.mark.parametrize(
    "protocol",
    [
        None,
        SECRET,
        f"{SECRET}, harness-ui",
        "harness-ui",
        "other, url-safe_secret",
        "harness-ui, wrong",
        f"harness-ui, {SECRET}, extra",
        f"harness-ui, , {SECRET}",
    ],
)
def test_websocket_rejects_missing_malformed_or_reordered_secret_protocols(
    auth: LaunchAuth, protocol: str | None
):
    headers = [("Origin", TAURI_ORIGIN)]
    if protocol is not None:
        headers.append(("Sec-WebSocket-Protocol", protocol))

    with pytest.raises(WebSocketException) as raised:
        auth.require_websocket(websocket(headers=headers))

    assert raised.value.code == 1008
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


def test_non_ascii_websocket_protocol_credential_fails_closed(auth: LaunchAuth):
    with pytest.raises(WebSocketException) as raised:
        auth.require_websocket(
            websocket(
                headers=[
                    ("Origin", TAURI_ORIGIN),
                    ("Sec-WebSocket-Protocol", "harness-ui, é"),
                ]
            )
        )

    assert raised.value.code == 1008
    assert raised.value.reason == "Forbidden"
    assert "é" not in repr(raised.value)
    assert SECRET not in repr(raised.value)


def test_websocket_does_not_accept_bearer_auth(auth: LaunchAuth):
    with pytest.raises(WebSocketException):
        auth.require_websocket(
            websocket(
                headers=[
                    ("Origin", TAURI_ORIGIN),
                    ("Authorization", f"Bearer {SECRET}"),
                ]
            )
        )


def test_malformed_protocol_is_not_bypassed_by_a_valid_cookie(auth: LaunchAuth):
    with pytest.raises(WebSocketException):
        auth.require_websocket(
            websocket(
                headers=[
                    ("Origin", LOOPBACK_ORIGIN),
                    ("Cookie", f"harness_ui_session={SECRET}"),
                    ("Sec-WebSocket-Protocol", f"{SECRET}, harness-ui"),
                ]
            )
        )


def test_configured_secret_is_absent_from_auth_and_exception_reprs(auth: LaunchAuth):
    assert SECRET not in repr(auth)

    with pytest.raises(HTTPException) as http_error:
        auth.require_http(request(headers=[("Authorization", "Bearer wrong")]))
    assert SECRET not in repr(http_error.value)

    with pytest.raises(WebSocketException) as websocket_error:
        auth.require_websocket(
            websocket(
                headers=[
                    ("Origin", TAURI_ORIGIN),
                    ("Sec-WebSocket-Protocol", "harness-ui, wrong"),
                ]
            )
        )
    assert SECRET not in repr(websocket_error.value)
