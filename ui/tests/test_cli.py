from __future__ import annotations

import base64
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit
import warnings

import httpx
import pytest
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from server.app import AppSettings, create_app
from server.__main__ import _listener
from server.static import frontend_dist, resource_root


UI_ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = "127.0.0.1"
TAURI_ORIGIN = "tauri://localhost"


class OfflineLLM:
    context_window = 128_000

    def complete(self, *_args, **_kwargs):
        raise AssertionError("static tests must not call the model")


@dataclass
class RunningServer:
    process: subprocess.Popen[str]
    stdout_lines: list[str] = field(default_factory=list)
    secret: str | None = None

    @property
    def stdout_text(self) -> str:
        return "".join(self.stdout_lines)

    def finish(
        self,
        *,
        expected_returncodes: frozenset[int] = frozenset({0, -signal.SIGTERM}),
    ) -> tuple[str, str]:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            remainder, stderr = self.process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            remainder, stderr = self.process.communicate(timeout=3)
            pytest.fail("server did not exit after SIGTERM")
        self.stdout_lines.append(remainder)
        assert self.process.returncode in expected_returncodes, self._scrub(stderr)
        return self.stdout_text, self._scrub(stderr)

    def _scrub(self, text: str) -> str:
        if self.secret:
            return text.replace(self.secret, "[redacted]")
        return text


def _readline(process: subprocess.Popen[str], secret: str | None) -> str:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    payload = bytearray()
    deadline = time.monotonic() + 8
    try:
        while not payload.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(timeout=remaining):
                process.kill()
                _, stderr = process.communicate(timeout=3)
                if secret:
                    stderr = stderr.replace(secret, "[redacted]")
                pytest.fail(f"server did not emit complete startup output: {stderr}")
            chunk = os.read(process.stdout.fileno(), 1)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        selector.close()
    if not payload.endswith(b"\n"):
        _, stderr = process.communicate(timeout=3)
        if secret:
            stderr = stderr.replace(secret, "[redacted]")
        pytest.fail(f"server exited before startup output: {stderr}")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        process.kill()
        process.communicate(timeout=3)
        pytest.fail("server emitted non-UTF-8 startup output")


@pytest.fixture
def run_server(tmp_path: Path):
    processes: list[RunningServer] = []

    def run(
        workspace: Path,
        *,
        secret: str = "sidecar-test-secret",
        env: dict[str, str] | None = None,
        workspace_argument: bool = True,
    ) -> tuple[RunningServer, dict]:
        metadata = tmp_path / f"metadata-{len(processes)}.sqlite3"
        command = [
            sys.executable,
            "-m",
            "server",
            "--port",
            "0",
            "--metadata-db",
            str(metadata),
            "--secret-stdin",
        ]
        if workspace_argument:
            command.extend(["--workspace", str(workspace)])
        process = subprocess.Popen(
            command,
            cwd=UI_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        running = RunningServer(process, secret=secret)
        processes.append(running)
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "type": "bootstrap",
                    "secret": secret,
                    "workspace": str(workspace.resolve()),
                }
            )
            + "\n"
        )
        process.stdin.close()
        process.stdin = None
        line = _readline(process, secret)
        running.stdout_lines.append(line)
        try:
            ready = json.loads(line)
        except json.JSONDecodeError:
            running.finish(
                expected_returncodes=frozenset({process.returncode or 1})
            )
            pytest.fail("sidecar did not emit a JSON readiness record")
        return running, ready

    yield run

    for running in processes:
        if running.process.poll() is None:
            running.finish()


def _static_app(tmp_path: Path, static_root: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return create_app(
        AppSettings(
            metadata_path=tmp_path / "metadata.sqlite3",
            base_workspace=workspace,
            launch_secret="static-test-secret",
            allowed_origins=frozenset(
                {"http://testserver", TAURI_ORIGIN}
            ),
        ),
        OfflineLLM,
        static_root=static_root,
    )


def _write_dist(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><title>Harness UI</title><main>frontend shell</main>"
    )
    (root / "assets" / "app.js").write_text("window.HARNESS_UI = true;")


def _run_invalid_sidecar(
    workspace: Path,
    stdin_text: str,
    *,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "server",
        "--workspace",
        str(workspace),
        "--port",
        "0",
        "--metadata-db",
        str(workspace / "metadata.sqlite3"),
        "--secret-stdin",
        *(extra_args or []),
    ]
    return subprocess.run(
        command,
        cwd=UI_ROOT,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_ready_record_never_contains_launch_secret(tmp_path: Path, run_server):
    process, ready = run_server(tmp_path, secret="not-for-stdout")

    assert ready["type"] == "server-ready"
    assert ready["host"] == LOOPBACK
    assert ready["port"] > 0
    assert set(ready) == {"type", "host", "port"}
    assert "not-for-stdout" not in process.stdout_text


def test_sidecar_derives_workspace_from_bootstrap_without_cli_argument(
    tmp_path: Path, run_server
):
    process, ready = run_server(
        tmp_path,
        secret="bootstrap-workspace-secret",
        workspace_argument=False,
    )

    assert ready["type"] == "server-ready"
    assert ready["host"] == LOOPBACK
    assert ready["port"] > 0
    assert "bootstrap-workspace-secret" not in process.stdout_text


def test_sidecar_stdout_contains_only_one_readiness_record(
    tmp_path: Path, run_server
):
    process, _ready = run_server(tmp_path, secret="single-record-secret")

    stdout, _stderr = process.finish()

    assert stdout.count("\n") == 1
    assert "single-record-secret" not in stdout


@pytest.mark.parametrize(
    "stdin_factory",
    [
        lambda workspace, secret: "not-json\n",
        lambda workspace, secret: json.dumps(
            {"type": "bootstrap", "secret": secret}
        )
        + "\n",
        lambda workspace, secret: json.dumps(
            {
                "type": "bootstrap",
                "secret": secret,
                "workspace": str(workspace),
                "extra": True,
            }
        )
        + "\n",
        lambda workspace, secret: json.dumps(
            {
                "type": "wrong",
                "secret": secret,
                "workspace": str(workspace),
            }
        )
        + "\n",
        lambda workspace, secret: json.dumps(
            {
                "type": "bootstrap",
                "secret": 123,
                "workspace": str(workspace),
            }
        )
        + "\n",
        lambda workspace, secret: json.dumps(
            {
                "type": "bootstrap",
                "secret": secret,
                "workspace": str(workspace.parent),
            }
        )
        + "\n",
        lambda workspace, secret: json.dumps(
            {
                "type": "bootstrap",
                "secret": secret + ("x" * 9_000),
                "workspace": str(workspace),
            }
        )
        + "\n",
        lambda workspace, secret: (
            json.dumps(
                {
                    "type": "bootstrap",
                    "secret": secret,
                    "workspace": str(workspace),
                }
            )
            + "\n"
        )
        * 2,
    ],
    ids=[
        "malformed",
        "missing-field",
        "extra-field",
        "wrong-type",
        "non-string-secret",
        "workspace-mismatch",
        "oversized",
        "duplicate-record",
    ],
)
def test_sidecar_rejects_invalid_bootstrap_without_disclosing_input(
    tmp_path: Path,
    stdin_factory: Callable[[Path, str], str],
):
    secret = "invalid-bootstrap-secret"
    result = _run_invalid_sidecar(tmp_path, stdin_factory(tmp_path, secret))

    assert result.returncode != 0
    assert result.stdout == ""
    assert secret not in result.stderr
    assert str(tmp_path) not in result.stderr


def test_sidecar_rejects_a_delayed_fragmented_second_bootstrap_record(
    tmp_path: Path,
):
    secret = "delayed-duplicate-secret"
    record = (
        json.dumps(
            {
                "type": "bootstrap",
                "secret": secret,
                "workspace": str(tmp_path),
            }
        )
        + "\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "server",
            "--port",
            "0",
            "--metadata-db",
            str(tmp_path / "metadata.sqlite3"),
            "--secret-stdin",
        ],
        cwd=UI_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(record)
        process.stdin.flush()
        time.sleep(1.0)
        process.stdin.write(record[:7])
        process.stdin.flush()
        process.stdin.write(record[7:])
        process.stdin.close()
        process.stdin = None
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=3)
            pytest.fail("sidecar accepted a delayed duplicate bootstrap record")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)

    assert process.returncode != 0
    assert stdout == ""
    assert secret not in stderr


def test_cli_refuses_non_loopback_host(tmp_path: Path):
    secret = "host-escape-secret"
    result = _run_invalid_sidecar(
        tmp_path,
        json.dumps(
            {
                "type": "bootstrap",
                "secret": secret,
                "workspace": str(tmp_path),
            }
        )
        + "\n",
        extra_args=["--host", "0.0.0.0"],
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert secret not in result.stderr


def test_prebound_loopback_listener_is_not_inheritable():
    listener = _listener(0)
    try:
        assert listener.getsockname()[0] == LOOPBACK
        assert listener.getsockname()[1] > 0
        assert not os.get_inheritable(listener.fileno())
    finally:
        listener.close()


def test_browser_mode_prints_complete_one_time_loopback_url(tmp_path: Path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "server",
            "--workspace",
            str(tmp_path),
            "--port",
            "0",
            "--metadata-db",
            str(tmp_path / "metadata.sqlite3"),
        ],
        cwd=UI_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    running = RunningServer(process)
    try:
        url = _readline(process, None).strip()
        running.stdout_lines.append(url + "\n")
        parsed = urlsplit(url)
        token = parse_qs(parsed.query, strict_parsing=True)["token"][0]
        running.secret = token
        padded = token + "=" * (-len(token) % 4)

        assert parsed.scheme == "http"
        assert parsed.hostname == LOOPBACK
        assert parsed.port is not None and parsed.port > 0
        assert parsed.path == "/bootstrap"
        assert set(parse_qs(parsed.query, strict_parsing=True)) == {"token"}
        assert len(base64.urlsafe_b64decode(padded)) == 32

        with httpx.Client(follow_redirects=False, timeout=3) as client:
            first = client.get(url)
            second = client.get(url)
        assert first.status_code == 303
        assert first.headers["location"] == "/"
        assert "httponly" in first.headers["set-cookie"].lower()
        assert second.status_code == 401
    finally:
        stdout, stderr = running.finish()

    assert token not in stderr
    assert stdout == url + "\n"


def test_static_bootstrap_cookie_serves_spa_routes_and_assets(tmp_path: Path):
    dist = tmp_path / "dist"
    _write_dist(dist)
    app = _static_app(tmp_path, dist)

    with TestClient(app, base_url="http://testserver") as client:
        bootstrap = client.get(
            "/bootstrap",
            params={"token": "static-test-secret"},
            follow_redirects=False,
        )
        route = client.get("/sessions/active/transcript-view")
        asset = client.get("/assets/app.js")
        second = client.get(
            "/bootstrap",
            params={"token": "static-test-secret"},
            follow_redirects=False,
        )

    assert bootstrap.status_code == 303
    assert bootstrap.headers["location"] == "/"
    assert "httponly" in bootstrap.headers["set-cookie"].lower()
    assert route.status_code == 200
    assert "frontend shell" in route.text
    assert route.headers["content-type"].startswith("text/html")
    assert asset.status_code == 200
    assert asset.text == "window.HARNESS_UI = true;"
    assert second.status_code == 401


def test_static_files_require_launch_auth(tmp_path: Path):
    dist = tmp_path / "dist"
    _write_dist(dist)
    app = _static_app(tmp_path, dist)

    with TestClient(app, base_url="http://testserver") as client:
        root = client.get("/")
        asset = client.get("/assets/app.js")

    assert root.status_code == 401
    assert asset.status_code == 401


def test_static_serving_rejects_missing_assets_and_traversal(tmp_path: Path):
    dist = tmp_path / "dist"
    _write_dist(dist)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must-not-be-served")
    (dist / "assets" / "linked.js").symlink_to(outside)
    app = _static_app(tmp_path, dist)
    headers = {"Authorization": "Bearer static-test-secret"}

    with TestClient(app, base_url="http://testserver", headers=headers) as client:
        missing = client.get("/assets/missing.js")
        traversal = client.get("/assets/%2e%2e/%2e%2e/outside-secret.txt")
        linked = client.get("/assets/linked.js")

    assert missing.status_code == 404
    assert traversal.status_code == 404
    assert linked.status_code == 404
    assert "must-not-be-served" not in missing.text + traversal.text + linked.text
    assert "frontend shell" not in missing.text + traversal.text + linked.text


def test_missing_development_dist_returns_actionable_response(tmp_path: Path):
    missing = tmp_path / "frontend" / "dist"
    app = _static_app(tmp_path, missing)

    with TestClient(
        app,
        base_url="http://testserver",
        headers={"Authorization": "Bearer static-test-secret"},
    ) as client:
        response = client.get("/")

    assert response.status_code == 503
    assert "frontend build" in response.text.lower()
    assert "npm run build" in response.text
    assert str(missing) not in response.text


def test_static_root_resolves_development_and_frozen_layouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert resource_root() == UI_ROOT
    assert frontend_dist() == UI_ROOT / "frontend" / "dist"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resource_root() == tmp_path
    assert frontend_dist() == tmp_path / "frontend" / "dist"


def test_sigterm_releases_real_open_session_lock(
    tmp_path: Path, run_server
):
    home = tmp_path / "home"
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "offline-access-token",
                    "account_id": "offline-account",
                }
            }
        )
    )
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    secret = "sigterm-test-secret"
    running, ready = run_server(tmp_path, secret=secret, env=environment)
    base_url = f"http://{ready['host']}:{ready['port']}"

    response = httpx.post(
        f"{base_url}/api/sessions",
        headers={
            "Authorization": f"Bearer {secret}",
            "Origin": base_url,
        },
        json={
            "workspace": str(tmp_path),
            "mode": "default",
            "context_mode": "compaction",
            "title": "Shutdown lock",
        },
        timeout=3,
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["session_id"]
    lock_path = tmp_path / ".agent" / "sessions" / f"{session_id}.lock"
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        running.finish()

        assert running.process.poll() is not None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
