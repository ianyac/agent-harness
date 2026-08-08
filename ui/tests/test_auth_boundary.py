from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import warnings

import pytest
from starlette.exceptions import (
    HTTPException,
    StarletteDeprecationWarning,
    WebSocketException,
)
from starlette.requests import Request
from starlette.websockets import WebSocket

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from server.app import AppSettings, create_app
from server.auth import LaunchAuth


LOOPBACK_ORIGIN = "http://testserver"
TAURI_ORIGIN = "tauri://localhost"
LAUNCH_SECRET = "launch-secret-for-boundary-tests"


class OfflineLLM:
    context_window = 128_000

    def complete(self, *_args, **_kwargs):
        raise AssertionError("authentication-boundary tests must remain offline")


def _request(
    method: str = "GET", *, headers: list[tuple[str, str]] | None = None
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": "/api/health",
            "raw_path": b"/api/health",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers or []
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }
    )


def _websocket(*, token: str, origin: str = LOOPBACK_ORIGIN) -> WebSocket:
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
                (b"origin", origin.encode("latin-1")),
                (b"sec-websocket-protocol", f"harness-ui, {token}".encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
            "subprotocols": ["harness-ui", token],
        },
        receive,
        send,
    )


def _decode_capability(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _browser_credentials(response) -> tuple[str, str, str]:
    location = urlsplit(response.headers["location"])
    segments = location.path.strip("/").split("/")
    assert segments[:1] == ["_app"]
    assert len(segments) == 2
    assert location.path.endswith("/")
    fragment = parse_qs(location.fragment, strict_parsing=True)
    assert set(fragment) == {"token"}
    static_token = segments[1]
    api_token = fragment["token"][0]
    return location.path.rstrip("/"), static_token, api_token


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=LAUNCH_SECRET,
        allowed_origins=frozenset({LOOPBACK_ORIGIN, TAURI_ORIGIN}),
    )


def _write_dist(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>boundary frontend</main>")
    (root / "assets" / "app.js").write_text("window.boundary = true;")


def test_browser_bootstrap_mints_distinct_path_and_api_capabilities_without_cookie():
    auth = LaunchAuth(LAUNCH_SECRET, {LOOPBACK_ORIGIN})

    response = auth.bootstrap_response(LAUNCH_SECRET)
    _base_path, static_token, api_token = _browser_credentials(response)

    assert response.status_code == 303
    assert len(_decode_capability(static_token)) == 32
    assert len(_decode_capability(api_token)) == 32
    assert static_token != api_token
    assert LAUNCH_SECRET not in response.headers["location"]
    assert "set-cookie" not in response.headers
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_bootstrap_disables_launch_secret_and_enables_browser_bearer_and_socket():
    auth = LaunchAuth(LAUNCH_SECRET, {LOOPBACK_ORIGIN})
    response = auth.bootstrap_response(LAUNCH_SECRET)
    _base_path, _static_token, api_token = _browser_credentials(response)

    with pytest.raises(HTTPException) as stale_http:
        auth.require_http(
            _request(headers=[("Authorization", f"Bearer {LAUNCH_SECRET}")])
        )
    assert stale_http.value.status_code == 401

    assert (
        auth.require_http(
            _request(headers=[("Authorization", f"Bearer {api_token}")])
        )
        is None
    )
    assert auth.require_websocket(_websocket(token=api_token)) == "harness-ui"
    with pytest.raises(WebSocketException) as stale_socket:
        auth.require_websocket(_websocket(token=LAUNCH_SECRET))
    assert stale_socket.value.code == 1008


def test_bootstrap_never_enables_cookie_credentials():
    auth = LaunchAuth(LAUNCH_SECRET, {LOOPBACK_ORIGIN})
    auth.bootstrap_response(LAUNCH_SECRET)

    with pytest.raises(HTTPException) as rejected:
        auth.require_http(
            _request(headers=[("Cookie", f"harness_ui_session={LAUNCH_SECRET}")])
        )

    assert rejected.value.status_code == 401


def test_tauri_keeps_launch_secret_when_browser_bootstrap_is_not_used():
    auth = LaunchAuth(LAUNCH_SECRET, {LOOPBACK_ORIGIN, TAURI_ORIGIN})

    assert (
        auth.require_http(
            _request(
                method="POST",
                headers=[
                    ("Authorization", f"Bearer {LAUNCH_SECRET}"),
                    ("Origin", TAURI_ORIGIN),
                ],
            )
        )
        is None
    )
    assert (
        auth.require_websocket(
            _websocket(token=LAUNCH_SECRET, origin=TAURI_ORIGIN)
        )
        == "harness-ui"
    )


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"Origin": LOOPBACK_ORIGIN}, 401),
        (
            {
                "Authorization": f"Bearer {LAUNCH_SECRET}",
                "Origin": "https://attacker.example",
            },
            403,
        ),
    ],
)
def test_outer_boundary_rejects_before_request_body_parsing_without_static_routes(
    settings: AppSettings,
    headers: dict[str, str],
    expected_status: int,
):
    app = create_app(settings, OfflineLLM, static_root=None)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        response = client.post(
            "/api/sessions",
            content=b'{"workspace":"\xff',
            headers={"Content-Type": "application/json", **headers},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"] in {"Unauthorized", "Forbidden"}
    assert "json" not in response.text.casefold()
    assert "utf" not in response.text.casefold()


@pytest.mark.parametrize("method", ["TRACE", "PROPFIND", "X-BOUNDARY"])
def test_outer_boundary_authenticates_arbitrary_methods_without_static_routes(
    settings: AppSettings,
    method: str,
):
    app = create_app(settings, OfflineLLM, static_root=None)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        anonymous = client.request(
            method,
            "/not-a-route",
            headers={"Origin": LOOPBACK_ORIGIN},
        )
        wrong_origin = client.request(
            method,
            "/not-a-route",
            headers={
                "Authorization": f"Bearer {LAUNCH_SECRET}",
                "Origin": "https://attacker.example",
            },
        )

    assert anonymous.status_code == 401
    assert wrong_origin.status_code == 403


def test_tauri_cors_preflight_and_actual_response_use_one_exact_origin(
    settings: AppSettings,
):
    app = create_app(settings, OfflineLLM, static_root=None)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        preflight = client.options(
            "/api/sessions",
            headers={
                "Origin": TAURI_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        actual = client.get(
            "/api/health",
            headers={
                "Authorization": f"Bearer {LAUNCH_SECRET}",
                "Origin": TAURI_ORIGIN,
            },
        )

    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == TAURI_ORIGIN
    assert "*" not in " ".join(
        value
        for name, value in preflight.headers.items()
        if name.casefold().startswith("access-control-")
    )
    assert "POST" in preflight.headers["access-control-allow-methods"]
    assert "Authorization" in preflight.headers["access-control-allow-headers"]
    assert "Content-Type" in preflight.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in preflight.headers
    assert actual.status_code == 200
    assert actual.headers["access-control-allow-origin"] == TAURI_ORIGIN


def test_cors_preflight_rejects_non_allowlisted_origin_without_cors_headers(
    settings: AppSettings,
):
    app = create_app(settings, OfflineLLM, static_root=None)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        response = client.options(
            "/api/sessions",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_static_capability_scopes_spa_routes_and_assets_and_api_uses_fragment_token(
    settings: AppSettings,
    tmp_path: Path,
):
    dist = tmp_path / "dist"
    _write_dist(dist)
    app = create_app(settings, OfflineLLM, static_root=dist)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        bootstrap = client.get(
            "/bootstrap",
            params={"token": LAUNCH_SECRET},
            follow_redirects=False,
        )
        static_base, static_token, api_token = _browser_credentials(bootstrap)
        shell = client.get(f"{static_base}/sessions/active")
        asset = client.get(f"{static_base}/assets/app.js")
        api = client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {api_token}"},
        )
        stale_launch = client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {LAUNCH_SECRET}"},
        )
        static_as_api = client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {static_token}"},
        )
        unscoped_asset = client.get(
            "/assets/app.js",
            headers={"Authorization": f"Bearer {api_token}"},
        )

    assert shell.status_code == 200
    assert "boundary frontend" in shell.text
    assert shell.headers["referrer-policy"] == "no-referrer"
    assert asset.status_code == 200
    assert asset.text == "window.boundary = true;"
    assert api.status_code == 200
    assert stale_launch.status_code == 401
    assert static_as_api.status_code == 401
    assert unscoped_asset.status_code == 404


@pytest.mark.parametrize(
    "unsafe_suffix",
    [
        "%2Fapi/not-a-route",
        "%2Fws/not-a-route",
        "route/%2e%2e/outside.txt",
        "route%5C..%5Coutside.txt",
    ],
)
def test_unsafe_static_paths_are_rejected_before_spa_fallback(
    settings: AppSettings,
    tmp_path: Path,
    unsafe_suffix: str,
):
    dist = tmp_path / "dist"
    _write_dist(dist)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside material")
    app = create_app(settings, OfflineLLM, static_root=dist)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        bootstrap = client.get(
            "/bootstrap",
            params={"token": LAUNCH_SECRET},
            follow_redirects=False,
        )
        static_base, _static_token, _api_token = _browser_credentials(bootstrap)
        response = client.get(f"{static_base}/{unsafe_suffix}")

    assert response.status_code == 404
    assert "boundary frontend" not in response.text
    assert "outside material" not in response.text


def test_static_path_resolving_through_root_symlink_is_rejected_before_spa(
    settings: AppSettings,
    tmp_path: Path,
):
    dist = tmp_path / "dist"
    _write_dist(dist)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside material")
    (dist / "linked-route").symlink_to(outside)
    app = create_app(settings, OfflineLLM, static_root=dist)

    with TestClient(app, base_url=LOOPBACK_ORIGIN) as client:
        bootstrap = client.get(
            "/bootstrap",
            params={"token": LAUNCH_SECRET},
            follow_redirects=False,
        )
        static_base, _static_token, _api_token = _browser_credentials(bootstrap)
        response = client.get(f"{static_base}/linked-route")

    assert response.status_code == 404
    assert "boundary frontend" not in response.text
    assert "outside material" not in response.text
