import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import textwrap
import warnings

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
from server.metadata import MetadataStore, NewSession
from server.protocol import UserMessage
from server.sessions import (
    InvalidWorkspace,
    SessionManager,
    SessionResumeError,
)


ORIGIN = "http://testserver"
SECRET = "rest-test-secret"
AUTH_HEADERS = {
    "Authorization": f"Bearer {SECRET}",
    "Origin": ORIGIN,
}


class FakeLLM:
    context_window = 128_000

    def complete(self, *_args, **_kwargs):
        raise AssertionError("REST tests must not call the model")


class ArchiveBlockingLLM:
    context_window = 128_000

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, *_args, on_text_delta=None, **_kwargs):
        if on_text_delta is not None:
            on_text_delta("still running")
        self.started.set()
        assert self.release.wait(timeout=5)
        return {"role": "assistant", "content": "finished"}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def settings(tmp_path: Path, workspace: Path) -> AppSettings:
    return AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )


@pytest.fixture
def app(settings: AppSettings):
    return create_app(settings, FakeLLM)


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app, base_url=ORIGIN, headers=AUTH_HEADERS) as test_client:
        yield test_client


def create_session(client: TestClient, workspace: Path, **changes) -> dict:
    payload = {
        "workspace": str(workspace),
        "mode": "default",
        "context_mode": "compaction",
        "title": "New chat",
    }
    payload.update(changes)
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def write_completed_session(path: Path, content: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": content},
            }
        )
        + "\n"
    )


def assert_session_unlocked(session_path: Path) -> None:
    lock_path = session_path.with_suffix(".lock")
    assert lock_path.is_file()
    assert lock_path.read_text() == ""


def valid_credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "test-access-token",
                    "account_id": "test-account",
                }
            }
        )
    )
    return path


def codex_credential_factory(
    llm_factory,
    credential_path: Path,
    *,
    read_text=None,
):
    from server import sessions as sessions_module

    return sessions_module.CodexCredentialFactory(
        llm_factory,
        credential_path=credential_path,
        read_text=read_text,
    )


def test_create_list_load_rename_and_archive_session(
    client: TestClient, workspace: Path
):
    created = create_session(client, workspace)
    session_id = created["session_id"]

    assert session_id
    assert created["workspace"] == str(workspace.resolve())
    assert client.get("/api/sessions").json()[0]["session_id"] == session_id
    assert client.get(f"/api/sessions/{session_id}").json()["title"] == "New chat"

    renamed = client.patch(
        f"/api/sessions/{session_id}", json={"title": "Retry work"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Retry work"

    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    assert session_path.with_suffix(".lock").exists()
    archived = client.delete(f"/api/sessions/{session_id}")
    assert archived.status_code == 204
    assert archived.content == b""
    assert_session_unlocked(session_path)
    assert client.get("/api/sessions").json() == []


def test_health_and_config_are_authenticated_and_describe_public_choices(
    client: TestClient, workspace: Path
):
    health = client.get("/api/health")
    config = client.get("/api/config")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert config.status_code == 200
    assert config.json() == {
        "base_workspace": str(workspace.resolve()),
        "default_mode": "default",
        "default_context_mode": "compaction",
        "modes": ["default", "acceptAll", "readOnly"],
        "context_modes": ["compaction", "folding"],
    }


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/api/health", None),
        ("GET", "/api/config", None),
        ("GET", "/api/sessions", None),
        ("POST", "/api/sessions", {}),
        ("GET", "/api/sessions/unknown", None),
        ("PATCH", "/api/sessions/unknown", {"title": "x"}),
        ("DELETE", "/api/sessions/unknown", None),
        ("GET", "/api/sessions/unknown/transcript", None),
        ("GET", "/api/sessions/unknown/safety", None),
    ],
)
def test_every_api_route_rejects_unauthenticated_requests(
    app, method: str, path: str, json_body: dict | None
):
    with TestClient(app, base_url=ORIGIN) as anonymous:
        response = anonymous.request(
            method,
            path,
            json=json_body,
            headers={"Origin": ORIGIN},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_bootstrap_is_the_only_auth_exemption_and_consumes_token_once(app):
    with TestClient(app, base_url=ORIGIN) as anonymous:
        first = anonymous.get(
            "/bootstrap", params={"token": SECRET}, follow_redirects=False
        )
        second = anonymous.get(
            "/bootstrap", params={"token": SECRET}, follow_redirects=False
        )

    assert first.status_code == 303
    assert first.headers["location"].startswith("/_app/")
    assert "#token=" in first.headers["location"]
    assert "set-cookie" not in first.headers
    assert first.headers["referrer-policy"] == "no-referrer"
    assert SECRET not in first.headers["location"]
    assert second.status_code == 401
    assert SECRET not in second.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_framework_documentation_does_not_add_unauthenticated_routes(app, path: str):
    with TestClient(app, base_url=ORIGIN) as anonymous:
        response = anonymous.get(path)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("path", ["/api/not-a-route", "/ws/not-a-route"])
def test_static_fallback_does_not_mask_service_namespace_404s(
    settings: AppSettings, tmp_path: Path, path: str
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend fallback</main>")
    app = create_app(settings, FakeLLM, static_root=dist)

    with TestClient(app, base_url=ORIGIN, headers=AUTH_HEADERS) as test_client:
        response = test_client.get(path)

    assert response.status_code == 404
    assert "frontend fallback" not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_static_fallback_authenticates_unknown_unsafe_http_methods(
    settings: AppSettings, tmp_path: Path, method: str
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend fallback</main>")
    app = create_app(settings, FakeLLM, static_root=dist)

    with TestClient(app, base_url=ORIGIN) as anonymous:
        response = anonymous.request(
            method,
            "/unknown-frontend-route",
            headers={"Origin": ORIGIN},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    ("path", "status_code", "allow"),
    [
        ("/unknown-frontend-route", 404, None),
        ("/api/not-a-route", 404, None),
        ("/ws/not-a-route", 404, None),
    ],
)
def test_authenticated_unsafe_static_fallback_never_serves_the_spa(
    settings: AppSettings,
    tmp_path: Path,
    path: str,
    status_code: int,
    allow: str | None,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend fallback</main>")
    app = create_app(settings, FakeLLM, static_root=dist)

    with TestClient(app, base_url=ORIGIN, headers=AUTH_HEADERS) as test_client:
        response = test_client.post(path)

    assert response.status_code == status_code
    assert response.headers.get("allow") == allow
    assert "frontend fallback" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("TRACE", "/unknown"),
        ("PROPFIND", "/api/not-a-route"),
    ],
)
def test_arbitrary_http_methods_authenticate_before_framework_method_handling(
    settings: AppSettings,
    tmp_path: Path,
    method: str,
    path: str,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend fallback</main>")
    app = create_app(settings, FakeLLM, static_root=dist)

    with TestClient(app, base_url=ORIGIN) as anonymous:
        response = anonymous.request(method, path, headers={"Origin": ORIGIN})

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("method", ["TRACE", "PROPFIND", "X-ARBITRARY"])
def test_credentialed_arbitrary_unsafe_methods_still_require_allowed_origin(
    settings: AppSettings,
    tmp_path: Path,
    method: str,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend fallback</main>")
    app = create_app(settings, FakeLLM, static_root=dist)

    with TestClient(app, base_url=ORIGIN) as test_client:
        response = test_client.request(
            method,
            "/unknown",
            headers={"Authorization": f"Bearer {SECRET}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


@pytest.mark.parametrize(
    ("method", "path", "status_code", "allow"),
    [
        ("TRACE", "/unknown", 404, None),
        ("X-GET-HEAD", "/unknown", 404, None),
        ("PROPFIND", "/api/not-a-route", 404, None),
        ("MKCOL", "/ws/not-a-route", 404, None),
    ],
)
def test_authenticated_arbitrary_methods_reach_non_spa_fallback(
    settings: AppSettings,
    tmp_path: Path,
    method: str,
    path: str,
    status_code: int,
    allow: str | None,
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend fallback</main>")
    app = create_app(settings, FakeLLM, static_root=dist)

    with TestClient(app, base_url=ORIGIN, headers=AUTH_HEADERS) as test_client:
        response = test_client.request(method, path)

    assert response.status_code == status_code
    assert response.headers.get("allow") == allow
    assert "frontend fallback" not in response.text


def test_missing_credentials_return_typed_non_disclosing_prerequisite(
    settings: AppSettings, workspace: Path
):
    token_material = "access-token-must-not-escape"

    def missing_credentials():
        raise RuntimeError(
            f"no codex credentials ({token_material}); run `codex login` first"
        )

    credential_path = valid_credential_file(settings.metadata_path.parent)
    app = create_app(
        settings,
        codex_credential_factory(missing_credentials, credential_path),
    )
    with TestClient(app, base_url=ORIGIN, headers=AUTH_HEADERS) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Blocked chat",
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "type": "credential_prerequisite",
            "message": "Codex credentials are required. Run `codex login` and retry.",
            "command": "codex login",
        }
    }
    assert token_material not in response.text
    assert SECRET not in response.text
    assert not (workspace / ".agent" / "sessions").exists()


@pytest.mark.parametrize("failure", [FileNotFoundError(), KeyError("tokens")])
def test_missing_or_invalid_credential_files_share_the_typed_prerequisite(
    settings: AppSettings, workspace: Path, failure: Exception
):
    def fail_read(_path: Path):
        raise failure

    with TestClient(
        create_app(
            settings,
            codex_credential_factory(
                FakeLLM,
                settings.metadata_path.parent / "auth.json",
                read_text=fail_read,
            ),
        ),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Blocked chat",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "credential_prerequisite"
    assert response.json()["error"]["command"] == "codex login"


@pytest.mark.parametrize("workspace_value", ["relative", "/definitely/not/here"])
def test_create_rejects_non_absolute_or_missing_workspace(
    client: TestClient, workspace_value: str
):
    response = client.post(
        "/api/sessions",
        json={
            "workspace": workspace_value,
            "mode": "default",
            "context_mode": "compaction",
            "title": "Invalid workspace",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_workspace"


def test_create_rejects_a_file_as_workspace(client: TestClient, tmp_path: Path):
    file_path = tmp_path / "not-a-workspace"
    file_path.write_text("content")

    response = client.post(
        "/api/sessions",
        json={
            "workspace": str(file_path),
            "mode": "default",
            "context_mode": "compaction",
            "title": "Invalid workspace",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_workspace"


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/missing",
        "/api/sessions/missing/transcript",
        "/api/sessions/missing/safety",
    ],
)
def test_unknown_session_ids_return_not_found(client: TestClient, path: str):
    response = client.get(path)

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "session_not_found"


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/%2E%2E%2Foutside",
        "/api/sessions/%2E%2E%2Foutside/transcript",
        "/api/sessions/%2E%2E%2Foutside/safety",
        "/api/sessions/space%20id",
    ],
)
def test_path_traversal_and_invalid_ids_are_rejected_without_external_access(
    client: TestClient, tmp_path: Path, path: str
):
    sentinel = tmp_path / "outside.jsonl"
    sentinel.write_text("do not read or alter")

    response = client.get(path)

    assert response.status_code == 404
    assert sentinel.read_text() == "do not read or alter"


def test_discovery_scans_only_base_and_metadata_known_workspaces_and_ignores_fold_logs(
    tmp_path: Path,
):
    base = tmp_path / "base"
    known = tmp_path / "known"
    unknown = tmp_path / "unknown"
    for path in (base, known, unknown):
        path.mkdir()
    write_completed_session(base / ".agent" / "sessions" / "base-session.jsonl")
    write_completed_session(known / ".agent" / "sessions" / "known-session.jsonl")
    write_completed_session(unknown / ".agent" / "sessions" / "secret-session.jsonl")
    write_completed_session(
        base / ".agent" / "sessions" / "base-session.fold-decisions.jsonl"
    )

    metadata_path = tmp_path / "metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        metadata.create_session(NewSession.defaults("indexed", known))

    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=base,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(settings, FakeLLM)
    with TestClient(
        app,
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        rows = test_client.get("/api/sessions").json()

    assert {row["session_id"] for row in rows} == {
        "indexed",
        "base-session",
        "known-session",
    }
    assert "secret-session" not in {row["session_id"] for row in rows}
    assert all("fold-decisions" not in row["session_id"] for row in rows)


def test_discovery_does_not_follow_context_companion_swapped_after_path_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "discover-swap.jsonl"
    write_completed_session(session_path, "local")
    mode_path = session_path.with_suffix(".context-mode")
    mode_path.write_text("compaction\n")
    external_mode = tmp_path / "external.context-mode"
    external_mode.write_text("folding\n")
    external_before = external_mode.read_bytes()
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    from server import sessions as sessions_module

    original_safe_path = sessions_module.SessionManager._safe_session_path

    def safe_then_swap(cls, *args, **kwargs):
        safe_path = original_safe_path(*args, **kwargs)
        if safe_path == session_path and not mode_path.is_symlink():
            mode_path.unlink()
            mode_path.symlink_to(external_mode)
        return safe_path

    monkeypatch.setattr(
        sessions_module.SessionManager,
        "_safe_session_path",
        classmethod(safe_then_swap),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        rows = test_client.get("/api/sessions").json()

    assert rows[0]["session_id"] == "discover-swap"
    assert rows[0]["context_mode"] == "compaction"
    assert external_mode.read_bytes() == external_before


def test_transcript_uses_session_log_while_the_session_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "resume-1.jsonl"
    write_completed_session(session_path, "authoritative")
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    from server import sessions as sessions_module

    original_load = sessions_module.SessionLog.load
    observations: list[bool] = []

    def load_while_locked(log):
        observations.append(session_path.with_suffix(".lock").exists())
        return original_load(log)

    monkeypatch.setattr(sessions_module.SessionLog, "load", load_while_locked)
    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/resume-1/transcript")

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "resume-1",
        "messages": [{"role": "assistant", "content": "authoritative"}],
    }
    assert observations == [True]
    assert_session_unlocked(session_path)


def test_active_turn_transcript_stops_at_last_durable_boundary(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    llm = ArchiveBlockingLLM()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, lambda: llm)

    async def scenario() -> None:
        record = await manager.create_session(
            workspace=workspace,
            mode="default",
            context_mode="compaction",
            title="Durable boundary",
        )
        connection = await manager.connect(record.session_id)
        connection.dispatch(UserMessage(text="not durable yet", mode="base"))
        assert await asyncio.to_thread(llm.started.wait, 5)
        session_path = (
            workspace / ".agent" / "sessions" / f"{record.session_id}.jsonl"
        )

        assert await manager.transcript(record.session_id) == []
        assert session_path.read_bytes() == b""

        llm.release.set()
        await manager._channels[record.session_id].wait_for_worker()
        assert await manager.transcript(record.session_id) == [
            {"role": "user", "content": "not durable yet"},
            {"role": "assistant", "content": "finished"},
        ]
        await manager.close()

    try:
        asyncio.run(scenario())
    finally:
        llm.release.set()


def test_prepoisoned_unicode_log_returns_typed_resume_error_without_mutation(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "poisoned.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": "bad\ud800log"},
            }
        )
        + "\n"
    )
    before = session_path.read_bytes()
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/poisoned/transcript")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert "malformed Unicode" in response.json()["error"]["message"]
    response.content.decode("utf-8")
    assert session_path.read_bytes() == before


def test_safety_opens_the_reviewed_runtime_and_returns_public_snapshot(
    client: TestClient, workspace: Path
):
    session_id = create_session(
        client, workspace, mode="readOnly", title="Safety"
    )["session_id"]

    response = client.get(f"/api/sessions/{session_id}/safety")

    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["mode"] == "readOnly"
    assert snapshot["sandbox_backend"]
    assert snapshot["workspace_write_boundary"]
    assert snapshot["network_policy"] in {"allow", "deny"}
    assert snapshot["tools"]
    assert set(next(iter(snapshot["tools"].values()))) == {
        "decision",
        "read_only",
        "network_egress",
    }


def test_folding_resume_failure_is_typed_and_does_not_leave_a_lock(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "folding-1.jsonl"
    write_completed_session(session_path, "resume me")
    session_path.with_suffix(".context-mode").write_text("folding\n")
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/folding-1/safety")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert "ledger is missing" in response.json()["error"]["message"]
    assert_session_unlocked(session_path)


def test_invalid_modes_are_rejected_by_request_validation(
    client: TestClient, workspace: Path
):
    invalid_base = client.post(
        "/api/sessions",
        json={
            "workspace": str(workspace),
            "mode": "plan",
            "context_mode": "compaction",
            "title": "Plan is per turn",
        },
    )
    invalid_context = client.post(
        "/api/sessions",
        json={
            "workspace": str(workspace),
            "mode": "default",
            "context_mode": "automatic",
            "title": "Unknown context",
        },
    )

    assert invalid_base.status_code == 422
    assert invalid_context.status_code == 422


@dataclass
class ClosingRuntime:
    name: str
    calls: list[str]
    failure: Exception | None = None

    def close(self) -> None:
        self.calls.append(self.name)
        if self.failure is not None:
            raise self.failure


class ClosingMetadata:
    def __init__(self, calls: list[str], failure: Exception | None = None):
        self.calls = calls
        self.failure = failure

    def close(self) -> None:
        self.calls.append("metadata")
        if self.failure is not None:
            raise self.failure


def test_manager_close_retries_only_failed_resources_and_stays_closed(
    tmp_path: Path,
):
    calls: list[str] = []
    runtime_one = ClosingRuntime("one", calls, RuntimeError("runtime close failed"))
    runtime_two = ClosingRuntime("two", calls)
    metadata = ClosingMetadata(calls, RuntimeError("metadata close failed"))
    manager = SessionManager(metadata, tmp_path, FakeLLM)
    manager._runtimes.update(
        {
            "one": runtime_one,
            "two": runtime_two,
        }
    )

    async def scenario() -> None:
        with pytest.raises(ExceptionGroup, match="session manager close failed") as raised:
            await manager.close()
        assert {str(error) for error in raised.value.exceptions} == {
            "runtime close failed",
            "metadata close failed",
        }
        assert manager._runtimes == {"one": runtime_one}

        with pytest.raises(RuntimeError) as sync_rejected:
            manager.list_sessions()
        assert type(sync_rejected.value).__name__ == "SessionManagerClosed"
        with pytest.raises(RuntimeError) as async_rejected:
            await manager.discover()
        assert type(async_rejected.value).__name__ == "SessionManagerClosed"

        runtime_one.failure = None
        metadata.failure = None
        await manager.close()
        await manager.close()

    asyncio.run(scenario())

    assert calls == ["one", "two", "metadata", "one", "metadata"]
    assert manager._runtimes == {}


def test_persistent_durability_failure_quarantines_non_durable_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class CompletingLLM:
        context_window = 128_000

        def complete(self, *_args, on_text_delta=None, **_kwargs):
            if on_text_delta is not None:
                on_text_delta("uncommitted answer")
            return {"role": "assistant", "content": "uncommitted answer"}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, CompletingLLM)

    async def scenario() -> None:
        try:
            record = await manager.create_session(
                workspace=workspace,
                mode="default",
                context_mode="compaction",
                title="Durability quarantine",
            )
            connection = await manager.connect(record.session_id)
            runtime = manager._runtimes[record.session_id]
            original_append = runtime.session_log._append

            def append_partial(payload: str) -> None:
                original_append(payload.splitlines(keepends=True)[0])
                raise OSError("persistent append failure")

            def fail_authoritative_reload() -> list[dict]:
                raise OSError("authoritative reload unavailable")

            monkeypatch.setattr(runtime.session_log, "_append", append_partial)
            monkeypatch.setattr(
                runtime.session_log,
                "load",
                fail_authoritative_reload,
            )

            connection.dispatch(UserMessage(text="must not surface", mode="base"))
            while True:
                event = await asyncio.wait_for(connection.next_event(), timeout=2)
                if event.type in {
                    "turn_completed",
                    "turn_cancelled",
                    "turn_failed",
                }:
                    terminal = event
                    break

            channel = manager._channels[record.session_id]
            assert terminal.type == "turn_failed"
            assert terminal.error_category == "filesystem"
            assert runtime.messages == []
            assert channel.messages == []
            assert getattr(runtime, "_ui_durability_failed", False)
            assert channel.lifecycle == "durability_failed"
            assert channel.shutting_down

            for operation in (
                lambda: manager.open_runtime(record.session_id),
                lambda: manager.connect(record.session_id),
                lambda: manager.safety(record.session_id),
            ):
                with pytest.raises(SessionResumeError, match="authority"):
                    await operation()
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_manager_close_retries_process_claim_release_and_allows_reacquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "shutdown-claim.jsonl"
    write_completed_session(session_path, "local")
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.create_session(NewSession.defaults("shutdown-claim", workspace))
    manager = SessionManager(metadata, workspace, FakeLLM)
    replacement_metadata = MetadataStore(tmp_path / "replacement.sqlite3")
    replacement_metadata.create_session(
        NewSession.defaults("shutdown-claim", workspace)
    )
    replacement = SessionManager(replacement_metadata, workspace, FakeLLM)
    from server import sessions as sessions_module

    runtime = None

    async def scenario() -> None:
        nonlocal runtime
        runtime = await manager.open_runtime("shutdown-claim")
        claim = runtime._session_lease._process_claim
        original_release = sessions_module._ProcessLeaseClaim.release
        failed = False

        def fail_release_once(current_claim):
            nonlocal failed
            if current_claim is claim and not failed:
                failed = True
                raise OSError("shutdown process claim release interrupted")
            return original_release(current_claim)

        monkeypatch.setattr(
            sessions_module._ProcessLeaseClaim,
            "release",
            fail_release_once,
        )

        try:
            with pytest.raises(ExceptionGroup, match="session manager close failed"):
                await manager.close()

            await manager.close()
            reopened = await replacement.open_runtime("shutdown-claim")
            assert reopened is not None
        finally:
            if runtime is not None:
                runtime.close()
            await manager.close()
            await replacement.close()

    asyncio.run(scenario())


def test_manager_close_retries_final_stage_removal_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "shutdown-stage.jsonl"
    write_completed_session(session_path, "local")
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.create_session(NewSession.defaults("shutdown-stage", workspace))
    manager = SessionManager(metadata, workspace, FakeLLM)
    from server import sessions as sessions_module

    runtime = None

    async def scenario() -> None:
        nonlocal runtime
        runtime = await manager.open_runtime("shutdown-stage")
        stage_path = runtime._session_lease._stage_path
        original_rmdir = sessions_module.os.rmdir
        failed = False

        def fail_stage_removal_once(path, *args, **kwargs):
            nonlocal failed
            if path == stage_path.name and not failed:
                failed = True
                raise OSError("shutdown stage removal interrupted")
            return original_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(sessions_module.os, "rmdir", fail_stage_removal_once)

        try:
            with pytest.raises(ExceptionGroup, match="session manager close failed"):
                await manager.close()
            assert stage_path.is_dir()

            await manager.close()

            assert not stage_path.exists()
            assert not any(
                path.name.startswith(".runtime-shutdown-stage-")
                for path in session_path.parent.iterdir()
            )
        finally:
            if runtime is not None:
                runtime.close()
            await manager.close()

    asyncio.run(scenario())


def test_archive_cancellation_keeps_worker_owned_until_thread_finishes(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    llm = ArchiveBlockingLLM()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, lambda: llm)

    async def scenario() -> None:
        record = await manager.create_session(
            workspace=workspace,
            mode="default",
            context_mode="compaction",
            title="Cancellation",
        )
        connection = await manager.connect(record.session_id)
        connection.dispatch(UserMessage(text="block", mode="base"))
        assert await asyncio.to_thread(llm.started.wait, 5)
        channel = manager._channels[record.session_id]
        worker = channel.worker
        assert worker is not None

        archive = asyncio.create_task(manager.archive_session(record.session_id))
        await asyncio.sleep(0)
        assert channel.shutting_down
        archive.cancel()
        with pytest.raises(asyncio.CancelledError):
            await archive

        assert channel.worker is worker
        assert not worker.done()
        assert channel.running
        assert record.session_id in manager._runtimes
        assert record.session_id in manager._channels

        retry = asyncio.create_task(manager.archive_session(record.session_id))
        await asyncio.sleep(0)
        assert not retry.done()
        llm.release.set()
        await retry

        assert record.session_id not in manager._runtimes
        assert record.session_id not in manager._channels
        assert metadata.get_session(record.session_id).archived_at is not None
        await manager.close()

    try:
        asyncio.run(scenario())
    finally:
        llm.release.set()


def test_archive_close_failure_is_non_reusable_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)
    from server import sessions as sessions_module

    async def scenario() -> None:
        record = await manager.create_session(
            workspace=workspace,
            mode="default",
            context_mode="compaction",
            title="Close retry",
        )
        await manager.connect(record.session_id)
        runtime = manager._runtimes[record.session_id]
        channel = manager._channels[record.session_id]
        stage_path = runtime._session_lease._stage_path
        original_rmdir = sessions_module.os.rmdir
        failed = False

        def fail_final_stage_once(path, *args, **kwargs):
            nonlocal failed
            if path == stage_path.name and not failed:
                failed = True
                raise OSError("archive stage removal interrupted")
            return original_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(sessions_module.os, "rmdir", fail_final_stage_once)

        with pytest.raises(OSError, match="archive stage removal interrupted"):
            await manager.archive_session(record.session_id)

        assert manager._runtimes[record.session_id] is runtime
        assert manager._channels[record.session_id] is channel
        assert channel.shutting_down
        assert metadata.get_session(record.session_id).archived_at is None
        for operation in (
            lambda: manager.open_runtime(record.session_id),
            lambda: manager.connect(record.session_id),
            lambda: manager.transcript(record.session_id),
            lambda: manager.safety(record.session_id),
        ):
            with pytest.raises(SessionResumeError, match="cleanup"):
                await operation()

        await manager.archive_session(record.session_id)
        assert not stage_path.exists()
        assert record.session_id not in manager._runtimes
        assert record.session_id not in manager._channels
        assert metadata.get_session(record.session_id).archived_at is not None
        await manager.close()

    asyncio.run(scenario())


def test_lifespan_closes_open_runtimes_and_metadata(
    settings: AppSettings, workspace: Path
):
    app = create_app(settings, FakeLLM)
    with TestClient(app, base_url=ORIGIN, headers=AUTH_HEADERS) as test_client:
        session_id = create_session(test_client, workspace)["session_id"]
        lock_path = (
            workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
        ).with_suffix(".lock")
        assert lock_path.exists()
        manager = app.state.session_manager

    assert lock_path.is_file()
    assert lock_path.read_text() == ""
    with pytest.raises(Exception):
        manager.metadata.get_session(session_id)


@pytest.mark.parametrize("linked_parent", ["agent", "sessions"])
def test_discovery_rejects_symlinked_session_parents_without_external_reads(
    tmp_path: Path, linked_parent: str
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_agent = tmp_path / "external-agent"
    external_sessions = external_agent / "sessions"
    external_sessions.mkdir(parents=True)
    external_session = external_sessions / "outside.jsonl"
    write_completed_session(external_session, "external-secret")
    if linked_parent == "agent":
        (workspace / ".agent").symlink_to(external_agent, target_is_directory=True)
    else:
        (workspace / ".agent").mkdir()
        (workspace / ".agent" / "sessions").symlink_to(
            external_sessions, target_is_directory=True
        )
    original = external_session.read_bytes()
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions")

    assert response.status_code == 200
    assert response.json() == []
    assert external_session.read_bytes() == original


@pytest.mark.parametrize(
    "external_payload",
    [
        "NOT SESSION DATA",
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "content": "external-secret"},
            }
        )
        + "\n",
    ],
)
def test_transcript_rejects_symlinked_session_file_without_read_or_truncation(
    tmp_path: Path, external_payload: str
):
    workspace = tmp_path / "workspace"
    sessions_dir = workspace / ".agent" / "sessions"
    sessions_dir.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text(external_payload)
    (sessions_dir / "linked.jsonl").symlink_to(external)
    metadata_path = tmp_path / "metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        metadata.create_session(NewSession.defaults("linked", workspace))
    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/linked/transcript")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert "external-secret" not in response.text
    assert external.read_text() == external_payload


def test_transcript_rejects_symlinked_lock_without_overwriting_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "locked.jsonl"
    write_completed_session(session_path, "local")
    external = tmp_path / "external-lock-target"
    external.write_text("not-a-pid")
    session_path.with_suffix(".lock").symlink_to(external)
    metadata_path = tmp_path / "metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        metadata.create_session(NewSession.defaults("locked", workspace))
    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/locked/transcript")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert external.read_text() == "not-a-pid"


def test_runtime_rejects_symlinked_context_artifact_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "context-link.jsonl"
    write_completed_session(session_path, "local")
    external = tmp_path / "external-context-mode"
    external.write_text("compaction\n")
    session_path.with_suffix(".context-mode").symlink_to(external)
    metadata_path = tmp_path / "metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        metadata.create_session(NewSession.defaults("context-link", workspace))
    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/context-link/safety")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert external.read_text() == "compaction\n"


def test_lifespan_closes_metadata_and_preserves_startup_error_when_manager_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from server import app as app_module

    calls: list[str] = []

    class FailingCloseMetadata:
        def __init__(self, _path: Path):
            calls.append("open")

        def close(self) -> None:
            calls.append("close")
            raise RuntimeError("metadata cleanup failed")

    missing_workspace = tmp_path / "missing-workspace"
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=missing_workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    monkeypatch.setattr(app_module, "MetadataStore", FailingCloseMetadata)

    with pytest.raises(InvalidWorkspace) as raised:
        with TestClient(create_app(settings, FakeLLM), base_url=ORIGIN):
            pass

    assert calls == ["open", "close"]
    assert "metadata cleanup failed" in "\n".join(raised.value.__notes__)


@pytest.mark.parametrize("missing", ["workspace", "session"])
def test_transcript_revalidates_missing_artifacts_without_recreating_them(
    tmp_path: Path, missing: str
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "vanished.jsonl"
    write_completed_session(session_path, "local")
    metadata_path = tmp_path / "metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        metadata.create_session(NewSession.defaults("vanished", workspace))
    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        if missing == "workspace":
            shutil.rmtree(workspace)
        else:
            session_path.unlink()
        response = test_client.get("/api/sessions/vanished/transcript")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert "missing" in response.json()["error"]["message"].lower()
    assert not session_path.with_suffix(".lock").exists()
    if missing == "workspace":
        assert not workspace.exists()
    else:
        assert not session_path.exists()


@pytest.mark.parametrize("missing", ["workspace", "session"])
def test_transcript_revalidates_missing_artifacts_for_an_open_runtime(
    client: TestClient, workspace: Path, missing: str
):
    session_id = create_session(client, workspace)["session_id"]
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    session_path.touch()
    if missing == "workspace":
        shutil.rmtree(workspace)
    else:
        session_path.unlink()

    response = client.get(f"/api/sessions/{session_id}/transcript")

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    assert "missing" in response.json()["error"]["message"].lower()
    if missing == "workspace":
        assert not workspace.exists()
    else:
        assert not session_path.exists()


def test_transcript_pins_validated_inode_across_load_boundary_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "swap.jsonl"
    write_completed_session(session_path, "local-authoritative")
    external = tmp_path / "external-target.jsonl"
    external.write_text("NOT SESSION DATA")
    external_before = external.read_bytes()
    metadata_path = tmp_path / "metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        metadata.create_session(NewSession.defaults("swap", workspace))
    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    from server import sessions as sessions_module

    original_load = sessions_module.SessionLog.load

    def swap_then_load(log):
        session_path.unlink()
        session_path.symlink_to(external)
        return original_load(log)

    monkeypatch.setattr(sessions_module.SessionLog, "load", swap_then_load)
    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.get("/api/sessions/swap/transcript")

    assert response.status_code == 200
    assert response.json()["messages"] == [
        {"role": "assistant", "content": "local-authoritative"}
    ]
    assert external.read_bytes() == external_before


def test_runtime_creation_uses_pinned_session_and_lock_across_boundary_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_session = tmp_path / "external-session.jsonl"
    external_session.write_text("NOT SESSION DATA")
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("not-a-pid")
    session_before = external_session.read_bytes()
    lock_before = external_lock.read_bytes()
    metadata_path = tmp_path / "metadata.sqlite3"
    settings = AppSettings(
        metadata_path=metadata_path,
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    session_id = "runtime-swap"
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    lock_path = session_path.with_suffix(".lock")
    from server import sessions as sessions_module

    monkeypatch.setattr(
        sessions_module.SessionManager,
        "_new_session_id",
        staticmethod(lambda: session_id),
    )
    original_init = sessions_module.HarnessRuntime.__init__
    observed_leases: list[object | None] = []

    def swap_then_initialize(runtime, *args, **kwargs):
        observed_leases.append(kwargs.get("session_lease"))
        session_path.unlink()
        session_path.symlink_to(external_session)
        if lock_path.exists() or lock_path.is_symlink():
            lock_path.unlink()
        lock_path.symlink_to(external_lock)
        return original_init(runtime, *args, **kwargs)

    monkeypatch.setattr(
        sessions_module.HarnessRuntime,
        "__init__",
        swap_then_initialize,
    )
    app = create_app(settings, FakeLLM)
    with TestClient(
        app,
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Swap",
            },
        )
        runtime = app.state.session_manager._runtimes[session_id]
        runtime.messages = [{"role": "assistant", "content": "new local turn"}]
        runtime.session_log.record_turn(runtime.messages)

    assert response.status_code == 201
    assert observed_leases and observed_leases[0] is not None
    assert external_session.read_bytes() == session_before
    assert external_lock.read_bytes() == lock_before


def test_runtime_context_mode_uses_private_stage_across_boundary_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_mode = tmp_path / "external.context-mode"
    external_mode.write_text("compaction")
    external_before = external_mode.read_bytes()
    session_id = "context-swap"
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    mode_path = session_path.with_suffix(".context-mode")
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    from server import sessions as sessions_module

    monkeypatch.setattr(
        sessions_module.SessionManager,
        "_new_session_id",
        staticmethod(lambda: session_id),
    )
    original_init = sessions_module.HarnessRuntime.__init__

    def swap_then_initialize(runtime, *args, **kwargs):
        mode_path.symlink_to(external_mode)
        return original_init(runtime, *args, **kwargs)

    monkeypatch.setattr(
        sessions_module.HarnessRuntime,
        "__init__",
        swap_then_initialize,
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Context swap",
            },
        )

    assert response.status_code == 201, response.text
    assert external_mode.read_bytes() == external_before
    assert not mode_path.is_symlink()
    assert mode_path.read_text() == "compaction\n"


def test_runtime_folding_companions_use_private_stages_across_boundary_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = "folding-swap"
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    external_mode = tmp_path / "external.context-mode"
    external_ledger = tmp_path / "external.folds.sqlite3"
    external_decisions = tmp_path / "external.fold-decisions.jsonl"
    external_mode.write_text("folding")
    external_ledger.write_bytes(b"")
    external_decisions.write_text("external decisions\n")
    external_before = {
        external_mode: external_mode.read_bytes(),
        external_ledger: external_ledger.read_bytes(),
        external_decisions: external_decisions.read_bytes(),
    }
    public_artifacts = {
        session_path.with_suffix(".context-mode"): external_mode,
        session_path.with_suffix(".folds.sqlite3"): external_ledger,
        session_path.with_suffix(".fold-decisions.jsonl"): external_decisions,
    }
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    from server import sessions as sessions_module

    monkeypatch.setattr(
        sessions_module.SessionManager,
        "_new_session_id",
        staticmethod(lambda: session_id),
    )
    original_init = sessions_module.HarnessRuntime.__init__

    def swap_then_initialize(runtime, *args, **kwargs):
        for public, external in public_artifacts.items():
            public.symlink_to(external)
        return original_init(runtime, *args, **kwargs)

    monkeypatch.setattr(
        sessions_module.HarnessRuntime,
        "__init__",
        swap_then_initialize,
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "folding",
                "title": "Folding swap",
            },
        )

    assert response.status_code == 201, response.text
    for external, before in external_before.items():
        assert external.read_bytes() == before
    for public in public_artifacts:
        assert public.is_file()
        assert not public.is_symlink()


def test_folding_purge_keeps_public_and_live_session_log_on_one_inode(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)
    secret = "delete this mounted payload"
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "a.py"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": secret},
        {"role": "assistant", "content": "done"},
    ]

    async def scenario() -> None:
        created = await manager.create_session(
            workspace=workspace,
            mode="default",
            context_mode="folding",
            title="Folding purge",
        )
        session_id = created.session_id
        runtime = manager._runtimes[session_id]
        session_path = (
            workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
        )
        runtime.messages = messages
        runtime.session_log.record_turn(messages)
        runtime.folding.sync(messages, runtime.tools)
        before_purge_inode = session_path.stat().st_ino

        runtime.folding.delete("m2.r0")

        assert secret not in session_path.read_text()
        assert session_path.stat().st_ino != before_purge_inode
        assert session_path.stat().st_ino == os.fstat(
            runtime.session_log._descriptor
        ).st_ino
        runtime.messages.append({"role": "assistant", "content": "after purge"})
        runtime.session_log.record_turn(runtime.messages)
        persisted = session_path.read_text()
        assert secret not in persisted
        assert "after purge" in persisted

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(manager.close())


def test_committed_runtime_republishes_live_log_when_public_name_is_replaced(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)
    external = tmp_path / "external.jsonl"
    external.write_text("external bytes stay exact\n")

    async def scenario() -> None:
        created = await manager.create_session(
            workspace=workspace,
            mode="default",
            context_mode="compaction",
            title="Close reconciliation",
        )
        runtime = manager._runtimes[created.session_id]
        session_path = (
            workspace
            / ".agent"
            / "sessions"
            / f"{created.session_id}.jsonl"
        )
        session_path.unlink()
        session_path.symlink_to(external)
        runtime.messages = [
            {"role": "user", "content": "persist me"},
            {"role": "assistant", "content": "persisted"},
        ]
        runtime.session_log.record_turn(runtime.messages)

        await manager.close()

        assert session_path.is_file()
        assert not session_path.is_symlink()
        assert "persist me" in session_path.read_text()
        assert external.read_text() == "external bytes stay exact\n"

    asyncio.run(scenario())


def test_secure_runtime_lock_has_one_owner_and_keeps_a_stable_inode(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "shared.jsonl"
    write_completed_session(session_path, "local")
    first_metadata = MetadataStore(tmp_path / "first.sqlite3")
    second_metadata = MetadataStore(tmp_path / "second.sqlite3")
    first_metadata.create_session(NewSession.defaults("shared", workspace))
    second_metadata.create_session(NewSession.defaults("shared", workspace))
    first = SessionManager(first_metadata, workspace, FakeLLM)
    second = SessionManager(second_metadata, workspace, FakeLLM)
    lock_path = session_path.with_suffix(".lock")

    async def scenario() -> None:
        try:
            first_runtime = await first.open_runtime("shared")
            assert first_runtime is not None
            owned_inode = lock_path.stat().st_ino
            with pytest.raises(RuntimeError, match="in use"):
                await second.open_runtime("shared")

            await first.close()
            assert lock_path.exists()
            assert lock_path.read_text() == ""

            second_runtime = await second.open_runtime("shared")
            assert second_runtime is not None
            assert lock_path.stat().st_ino == owned_inode
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())


def test_process_lease_serializes_concurrent_managers_after_lock_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "concurrent.jsonl"
    write_completed_session(session_path, "local")
    first = SessionManager(
        MetadataStore(tmp_path / "first.sqlite3"),
        workspace,
        FakeLLM,
    )
    second = SessionManager(
        MetadataStore(tmp_path / "second.sqlite3"),
        workspace,
        FakeLLM,
    )
    record = NewSession.defaults("concurrent", workspace)
    lock_path = session_path.with_suffix(".lock")
    first_lease_ready = threading.Event()
    resume_first = threading.Event()
    call_guard = threading.Lock()
    instantiate_calls = 0
    from server import sessions as sessions_module

    original_instantiate = sessions_module.SessionManager._instantiate_runtime

    def pause_first_instantiation(config, llm, lease, *, resuming):
        nonlocal instantiate_calls
        with call_guard:
            instantiate_calls += 1
            call_number = instantiate_calls
        if call_number == 1:
            first_lease_ready.set()
            if not resume_first.wait(5):
                raise TimeoutError("first runtime acquisition was not resumed")
        return original_instantiate(
            config,
            llm,
            lease,
            resuming=resuming,
        )

    monkeypatch.setattr(
        sessions_module.SessionManager,
        "_instantiate_runtime",
        staticmethod(pause_first_instantiation),
    )

    first_runtime = None
    second_runtime = None
    replacement_runtime = None
    second_error: BaseException | None = None
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(first._construct_runtime, record)
            assert first_lease_ready.wait(5)
            owned_inode = lock_path.stat().st_ino
            lock_path.unlink()
            lock_path.write_text("")
            assert lock_path.stat().st_ino != owned_inode

            second_future = executor.submit(second._construct_runtime, record)
            try:
                second_runtime = second_future.result(timeout=5)
            except BaseException as error:
                second_error = error
            finally:
                resume_first.set()
            first_runtime = first_future.result(timeout=5)

        assert isinstance(second_error, RuntimeError), second_runtime
        assert "in use" in str(second_error)

        first_runtime.close()
        first_runtime = None
        replacement_runtime = second._construct_runtime(record)
    finally:
        resume_first.set()
        for runtime in (second_runtime, first_runtime, replacement_runtime):
            if runtime is not None:
                runtime.close()
        asyncio.run(first.close())
        asyncio.run(second.close())


def test_cross_process_lease_survives_every_lock_path_replacement(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "cross-process.jsonl"
    write_completed_session(session_path, "local")
    lock_path = session_path.with_suffix(".lock")
    lease = SessionManager._acquire_session_lease(
        workspace,
        "cross-process",
        create_session=False,
    )
    from server import sessions as sessions_module

    coordination_descriptor = lease._coordination_claim.descriptor
    assert coordination_descriptor is not None
    coordination_path = sessions_module._SecureSessionLease._descriptor_path(
        coordination_descriptor
    )
    for authoritative_path in (lock_path, coordination_path):
        owned_identity = (
            authoritative_path.stat().st_dev,
            authoritative_path.stat().st_ino,
        )
        authoritative_path.unlink()
        authoritative_path.write_text("")
        assert (
            authoritative_path.stat().st_dev,
            authoritative_path.stat().st_ino,
        ) != owned_identity
    unrelated_device = os.open("/dev/null", os.O_RDWR | os.O_CLOEXEC)
    os.close(unrelated_device)

    probe = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from server.sessions import SessionManager, SessionResumeError

        try:
            lease = SessionManager._acquire_session_lease(
                Path(sys.argv[1]), "cross-process", create_session=False
            )
        except SessionResumeError as error:
            print(f"BLOCKED:{error}")
            raise SystemExit(3)
        else:
            print("ACQUIRED")
            lease.close()
        """
    )

    def run_probe() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", probe, str(workspace)],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    try:
        blocked = run_probe()
        assert blocked.returncode == 3, (blocked.stdout, blocked.stderr)
        assert "BLOCKED:session is already in use" in blocked.stdout
    finally:
        lease.close()

    acquired = run_probe()
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)
    assert acquired.stdout.strip() == "ACQUIRED"


def test_coordination_lock_allows_distinct_sessions_concurrently(tmp_path: Path):
    workspace = tmp_path / "workspace"
    first_path = workspace / ".agent" / "sessions" / "first.jsonl"
    second_path = workspace / ".agent" / "sessions" / "second.jsonl"
    write_completed_session(first_path, "one")
    write_completed_session(second_path, "two")

    first = SessionManager._acquire_session_lease(
        workspace, "first", create_session=False
    )
    second = None
    try:
        second = SessionManager._acquire_session_lease(
            workspace, "second", create_session=False
        )
    finally:
        first.close()
        if second is not None:
            second.close()


@pytest.mark.parametrize(
    "unsafe_entry",
    ["root_symlink", "root_file", "lock_symlink", "lock_directory"],
)
def test_coordination_domain_rejects_symlinks_and_nonregular_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_entry: str,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    coordination_root = tmp_path / "runtime" / "agent-harness-ui"
    coordination_root.parent.mkdir()
    from server import sessions as sessions_module

    monkeypatch.setattr(
        sessions_module,
        "_coordination_root_path",
        lambda: coordination_root,
    )
    if unsafe_entry == "root_symlink":
        outside = tmp_path / "outside-root"
        outside.mkdir()
        coordination_root.symlink_to(outside, target_is_directory=True)
    elif unsafe_entry == "root_file":
        coordination_root.write_text("not a directory")
    else:
        workspace_descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            claim = sessions_module._CoordinationLeaseClaim.acquire(
                workspace_descriptor, "unsafe"
            )
            claim.release()
        finally:
            os.close(workspace_descriptor)
        lock_path = next(coordination_root.glob("*.lock"))
        lock_path.unlink()
        if unsafe_entry == "lock_symlink":
            outside = tmp_path / "outside-lock"
            outside.write_text("external")
            lock_path.symlink_to(outside)
        else:
            lock_path.mkdir()

    workspace_descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(SessionResumeError, match="coordination"):
            sessions_module._CoordinationLeaseClaim.acquire(
                workspace_descriptor, "unsafe"
            )
    finally:
        os.close(workspace_descriptor)


def test_coordination_acquisition_failure_is_resource_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    from server import sessions as sessions_module

    before_descriptors = len(os.listdir("/dev/fd"))
    primary = OSError("injected hashed-lock acquisition failure")
    cleanup_error = OSError("injected coordination descriptor close failure")
    failed_descriptor: int | None = None
    close_interrupted = False
    original_close = sessions_module.os.close

    def fail_hashed_lock(descriptor: int, _operation: int) -> None:
        nonlocal failed_descriptor
        failed_descriptor = descriptor
        raise primary

    def interrupt_hashed_close_once(descriptor: int) -> None:
        nonlocal close_interrupted
        if descriptor == failed_descriptor and not close_interrupted:
            close_interrupted = True
            raise cleanup_error
        original_close(descriptor)

    replacement = None
    retry_error: BaseException | None = None
    try:
        monkeypatch.setattr(sessions_module.fcntl, "flock", fail_hashed_lock)
        monkeypatch.setattr(sessions_module.os, "close", interrupt_hashed_close_once)

        with pytest.raises(OSError) as raised:
            sessions_module._CoordinationLeaseClaim.acquire(
                workspace_descriptor,
                "acquisition-cleanup",
            )

        monkeypatch.undo()
        after_failure = len(os.listdir("/dev/fd"))
        try:
            replacement = sessions_module._CoordinationLeaseClaim.acquire(
                workspace_descriptor,
                "acquisition-cleanup",
            )
        except BaseException as error:
            retry_error = error
        finally:
            if replacement is not None:
                replacement.release()
        after_retry = len(os.listdir("/dev/fd"))

        assert raised.value is primary, (
            raised.value,
            f"fd_growth={after_failure - before_descriptors}",
            retry_error,
        )
        assert getattr(raised.value, "cleanup_errors", ()) == (cleanup_error,)
        assert after_failure == before_descriptors
        assert retry_error is None
        assert after_retry == before_descriptors
    finally:
        monkeypatch.undo()
        os.close(workspace_descriptor)


def test_authority_transfer_close_interruption_is_resource_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    from server import sessions as sessions_module

    before_descriptors = len(os.listdir("/dev/fd"))
    primary = OSError("injected temporary authority directory close failure")
    device_descriptor: int | None = None
    authority_descriptor: int | None = None
    close_interrupted = False
    original_open = sessions_module.os.open
    original_close = sessions_module.os.close

    def record_authority_open(path, flags, *args, **kwargs):
        nonlocal device_descriptor, authority_descriptor
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "/dev":
            device_descriptor = descriptor
        elif path == "null" and kwargs.get("dir_fd") == device_descriptor:
            authority_descriptor = descriptor
        return descriptor

    def interrupt_device_close_once(descriptor: int) -> None:
        nonlocal close_interrupted
        if descriptor == device_descriptor and not close_interrupted:
            close_interrupted = True
            raise primary
        original_close(descriptor)

    replacement = None
    retry_error: BaseException | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sessions_module.os, "open", record_authority_open)
            patch.setattr(sessions_module.os, "close", interrupt_device_close_once)

            with pytest.raises(BaseException) as raised:
                sessions_module._CoordinationLeaseClaim._acquire_authority(
                    os.fstat(workspace_descriptor),
                    "authority-transfer-cleanup",
                )

        after_failure = len(os.listdir("/dev/fd"))
        try:
            replacement = sessions_module._CoordinationLeaseClaim._acquire_authority(
                os.fstat(workspace_descriptor),
                "authority-transfer-cleanup",
            )
        except BaseException as error:
            retry_error = error
        finally:
            if replacement is not None:
                os.close(replacement)
        after_retry = len(os.listdir("/dev/fd"))

        assert device_descriptor is not None
        assert authority_descriptor is not None
        assert raised.value is primary, (
            raised.value,
            f"fd_growth={after_failure - before_descriptors}",
            retry_error,
        )
        assert getattr(raised.value, "cleanup_errors", ()) == ()
        assert after_failure == before_descriptors
        assert retry_error is None
        assert after_retry == before_descriptors
    finally:
        for descriptor in (authority_descriptor, device_descriptor):
            if descriptor is not None:
                try:
                    original_close(descriptor)
                except OSError:
                    pass
        os.close(workspace_descriptor)


def test_authority_acquisition_failure_cleanup_is_resource_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    from server import sessions as sessions_module

    before_descriptors = len(os.listdir("/dev/fd"))
    primary = SessionResumeError("injected authority acquisition failure")
    cleanup_error = OSError("injected authority descriptor close failure")
    device_descriptor: int | None = None
    authority_descriptor: int | None = None
    close_interrupted = False
    original_open = sessions_module.os.open
    original_close = sessions_module.os.close

    def record_authority_open(path, flags, *args, **kwargs):
        nonlocal device_descriptor, authority_descriptor
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "/dev":
            device_descriptor = descriptor
        elif path == "null" and kwargs.get("dir_fd") == device_descriptor:
            authority_descriptor = descriptor
        return descriptor

    def fail_authority_acquisition(
        _descriptor: int,
        _command: int,
        _lock: bytes,
    ) -> None:
        raise primary

    def interrupt_authority_close_once(descriptor: int) -> None:
        nonlocal close_interrupted
        if descriptor == authority_descriptor and not close_interrupted:
            close_interrupted = True
            raise cleanup_error
        original_close(descriptor)

    replacement = None
    retry_error: BaseException | None = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sessions_module.os, "open", record_authority_open)
            patch.setattr(sessions_module.fcntl, "fcntl", fail_authority_acquisition)
            patch.setattr(
                sessions_module.os,
                "close",
                interrupt_authority_close_once,
            )

            with pytest.raises(BaseException) as raised:
                sessions_module._CoordinationLeaseClaim._acquire_authority(
                    os.fstat(workspace_descriptor),
                    "authority-acquisition-cleanup",
                )

        after_failure = len(os.listdir("/dev/fd"))
        try:
            replacement = sessions_module._CoordinationLeaseClaim._acquire_authority(
                os.fstat(workspace_descriptor),
                "authority-acquisition-cleanup",
            )
        except BaseException as error:
            retry_error = error
        finally:
            if replacement is not None:
                os.close(replacement)
        after_retry = len(os.listdir("/dev/fd"))

        assert device_descriptor is not None
        assert authority_descriptor is not None
        assert raised.value is primary, (
            raised.value,
            f"fd_growth={after_failure - before_descriptors}",
            retry_error,
        )
        assert getattr(raised.value, "cleanup_errors", ()) == (cleanup_error,)
        assert after_failure == before_descriptors
        assert retry_error is None
        assert after_retry == before_descriptors
    finally:
        for descriptor in (authority_descriptor, device_descriptor):
            if descriptor is not None:
                try:
                    original_close(descriptor)
                except OSError:
                    pass
        os.close(workspace_descriptor)


@pytest.mark.parametrize(
    "failure_point",
    ["session_descriptor", "lock_clear", "process_registry", "coordination"],
)
def test_secure_session_lease_release_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "retry.jsonl"
    write_completed_session(session_path, "local")
    from server import sessions as sessions_module

    lease = SessionManager._acquire_session_lease(
        workspace,
        "retry",
        create_session=False,
    )
    lock_path = session_path.with_suffix(".lock")
    if failure_point == "session_descriptor":
        target = lease._session_descriptor
        original = sessions_module.os.close
        failed = False

        def fail_once(descriptor: int):
            nonlocal failed
            if descriptor == target and not failed:
                failed = True
                raise OSError("session descriptor close interrupted")
            return original(descriptor)

        monkeypatch.setattr(sessions_module.os, "close", fail_once)
    elif failure_point == "lock_clear":
        target = lease._lock_descriptor
        original = sessions_module.os.ftruncate
        failed = False

        def fail_once(descriptor: int, length: int):
            nonlocal failed
            if descriptor == target and not failed:
                failed = True
                raise OSError("lock clear interrupted")
            return original(descriptor, length)

        monkeypatch.setattr(sessions_module.os, "ftruncate", fail_once)
    elif failure_point == "process_registry":
        target = lease._process_claim
        original = sessions_module._ProcessLeaseClaim.release
        failed = False

        def fail_once(claim):
            nonlocal failed
            if claim is target and not failed:
                failed = True
                raise OSError("process registry release interrupted")
            return original(claim)

        monkeypatch.setattr(
            sessions_module._ProcessLeaseClaim,
            "release",
            fail_once,
        )
    else:
        target = lease._coordination_claim
        original = sessions_module._CoordinationLeaseClaim.release
        failed = False

        def fail_once(claim):
            nonlocal failed
            if claim is target and not failed:
                failed = True
                raise OSError("coordination release interrupted")
            return original(claim)

        monkeypatch.setattr(
            sessions_module._CoordinationLeaseClaim,
            "release",
            fail_once,
        )

    with pytest.raises(OSError, match="interrupted"):
        lease.close()
    with pytest.raises(RuntimeError, match="in use"):
        SessionManager._acquire_session_lease(
            workspace,
            "retry",
            create_session=False,
        )

    lease.close()
    replacement = SessionManager._acquire_session_lease(
        workspace,
        "retry",
        create_session=False,
    )
    replacement.close()

    assert lock_path.exists()
    assert lock_path.read_text() == ""


def test_session_lease_commit_defers_private_backup_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "commit.jsonl"
    write_completed_session(session_path, "local")
    from server import sessions as sessions_module

    lease = SessionManager._acquire_session_lease(
        workspace,
        "commit",
        create_session=False,
    )
    lease.publish()
    backup_name = lease._publications["commit.jsonl"].backup_name
    assert backup_name is not None
    original_unlink = sessions_module.os.unlink
    failed = False

    def fail_backup_cleanup_once(path, *args, **kwargs):
        nonlocal failed
        if path == backup_name and not failed:
            failed = True
            raise OSError("backup cleanup interrupted")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "unlink", fail_backup_cleanup_once)

    lease.commit()
    lease.close()

    assert session_path.exists()
    assert "local" in session_path.read_text()


def test_repeated_session_publication_keeps_original_abort_backup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "repeat.jsonl"
    original = json.dumps(
        {"type": "message", "message": {"role": "user", "content": "original"}}
    ) + "\n"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(original)

    lease = SessionManager._acquire_session_lease(
        workspace,
        "repeat",
        create_session=False,
    )
    lease.publish()
    lease.publish()
    lease.abort()

    assert session_path.read_text() == original


def test_session_lease_abort_retries_post_capture_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "capture.jsonl"
    write_completed_session(session_path, "before lease")
    from server import sessions as sessions_module

    lease = SessionManager._acquire_session_lease(
        workspace,
        "capture",
        create_session=False,
    )
    lease.publish()
    session_path.unlink()
    session_path.write_text("replacement installed during lease\n")
    original_stat = sessions_module.os.stat
    failed = False

    def fail_captured_inspection_once(path, *args, **kwargs):
        nonlocal failed
        if (
            isinstance(path, str)
            and path.startswith(".quarantine-capture.jsonl-")
            and kwargs.get("dir_fd") == lease._stage_descriptor
            and not failed
        ):
            failed = True
            raise OSError("captured replacement inspection interrupted")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "stat", fail_captured_inspection_once)

    with pytest.raises(ExceptionGroup, match="publication rollback") as raised:
        lease.abort()
    assert any(
        "captured replacement inspection interrupted" in str(error)
        for error in raised.value.exceptions
    )

    lease.abort()

    assert session_path.read_text() == "replacement installed during lease\n"


def test_session_lease_recovers_private_sqlite_journal_from_stale_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    session_id = "crashed"
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    write_completed_session(session_path, "local")
    sessions_dir = session_path.parent
    lock_path = session_path.with_suffix(".lock")
    lock_path.touch()
    ledger_path = session_path.with_suffix(".folds.sqlite3")

    import sqlite3

    connection = sqlite3.connect(ledger_path)
    connection.execute("CREATE TABLE durable (value TEXT NOT NULL)")
    connection.execute("INSERT INTO durable VALUES ('before crash')")
    connection.commit()
    connection.close()

    stale_stage = sessions_dir / f".runtime-{session_id}-stale"
    stale_stage.mkdir(mode=0o700)
    (stale_stage / lock_path.name).hardlink_to(lock_path)
    (stale_stage / ledger_path.name).hardlink_to(ledger_path)
    (stale_stage / f"{ledger_path.name}-journal").write_bytes(b"")

    from server import sessions as sessions_module

    original_connect = sessions_module.sqlite3.connect
    recovered_paths: list[Path] = []

    def record_connect(path, *args, **kwargs):
        recovered_paths.append(Path(path))
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sessions_module.sqlite3, "connect", record_connect)

    lease = SessionManager._acquire_session_lease(
        workspace,
        session_id,
        create_session=False,
    )
    lease.abort()

    assert recovered_paths == [stale_stage / ledger_path.name]
    assert not stale_stage.exists()
    verification = sqlite3.connect(ledger_path)
    try:
        assert verification.execute(
            "SELECT value FROM durable"
        ).fetchone() == ("before crash",)
    finally:
        verification.close()


def test_session_lease_recovers_atomic_purge_from_stale_stage(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_id = "purge-crash"
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"
    write_completed_session(session_path, "secret before purge")
    original_inode = session_path.stat().st_ino
    sessions_dir = session_path.parent
    lock_path = session_path.with_suffix(".lock")
    lock_path.touch()

    stale_stage = sessions_dir / f".runtime-{session_id}-stale"
    stale_stage.mkdir(mode=0o700)
    (stale_stage / lock_path.name).hardlink_to(lock_path)
    (stale_stage / ".session-anchor").hardlink_to(session_path)
    sanitized = json.dumps(
        {
            "type": "message",
            "message": {"role": "assistant", "content": "purged"},
        }
    ) + "\n"
    (stale_stage / session_path.name).write_text(sanitized)

    lease = SessionManager._acquire_session_lease(
        workspace,
        session_id,
        create_session=False,
    )
    lease.abort()

    assert session_path.read_text() == sanitized
    assert session_path.stat().st_ino != original_inode
    assert not stale_stage.exists()


def test_session_lease_abort_retries_transient_created_artifact_capture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    sessions_dir = workspace / ".agent" / "sessions"
    sessions_dir.mkdir(parents=True)
    session_id = "abort-retry"
    session_name = f"{session_id}.jsonl"
    session_path = sessions_dir / session_name
    from server import sessions as sessions_module

    lease = SessionManager._acquire_session_lease(
        workspace,
        session_id,
        create_session=True,
    )
    original_rename = sessions_module.os.rename
    failed = False

    def fail_owned_capture_once(source, destination, *args, **kwargs):
        nonlocal failed
        if source == session_name and not failed:
            failed = True
            raise OSError("transient created artifact capture failure")
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "rename", fail_owned_capture_once)

    with pytest.raises(ExceptionGroup, match="publication rollback") as raised:
        lease.abort()
    assert any(
        "transient created artifact capture failure" in str(error)
        for error in raised.value.exceptions
    )
    assert session_path.exists()

    lease.abort()

    assert not session_path.exists()
    replacement = SessionManager._acquire_session_lease(
        workspace,
        session_id,
        create_session=True,
    )
    replacement.abort()


def test_partial_lease_acquisition_preserves_primary_and_closes_all_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    sessions_dir = workspace / ".agent" / "sessions"
    sessions_dir.mkdir(parents=True)
    from server import sessions as sessions_module

    original_open = sessions_module.os.open
    original_ftruncate = sessions_module.os.ftruncate
    original_listdir = sessions_module.os.listdir
    opened_directories: list[int] = []
    lock_descriptor: int | None = None
    lock_truncations = 0
    stage_descriptor: int | None = None
    stage_listings = 0

    def record_open(path, flags, *args, **kwargs):
        nonlocal lock_descriptor, stage_descriptor
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & sessions_module.os.O_DIRECTORY:
            opened_directories.append(descriptor)
            if str(path).startswith(".runtime-missing-"):
                stage_descriptor = descriptor
        if path == "missing.lock":
            lock_descriptor = descriptor
        return descriptor

    def fail_lock_cleanup(descriptor: int, length: int):
        nonlocal lock_truncations
        if descriptor == lock_descriptor:
            lock_truncations += 1
            if lock_truncations == 2:
                raise OSError("lock cleanup interrupted")
        return original_ftruncate(descriptor, length)

    def fail_stage_listing_once(descriptor):
        nonlocal stage_listings
        if descriptor == stage_descriptor:
            stage_listings += 1
            if stage_listings == 1:
                raise OSError("stage listing interrupted")
        return original_listdir(descriptor)

    monkeypatch.setattr(sessions_module.os, "open", record_open)
    monkeypatch.setattr(sessions_module.os, "ftruncate", fail_lock_cleanup)
    monkeypatch.setattr(sessions_module.os, "listdir", fail_stage_listing_once)

    with pytest.raises(RuntimeError, match="session file is missing or unsafe") as raised:
        SessionManager._acquire_session_lease(
            workspace,
            "missing",
            create_session=False,
        )

    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    assert "lock cleanup interrupted" in notes
    assert "stage listing interrupted" in notes
    assert any(
        "lock cleanup interrupted" in str(error)
        for error in getattr(raised.value, "cleanup_errors", ())
    )
    for descriptor in opened_directories:
        with pytest.raises(OSError):
            sessions_module.os.fstat(descriptor)
    assert not any(path.name.startswith(".runtime-missing-") for path in sessions_dir.iterdir())
    replacement = SessionManager._acquire_session_lease(
        workspace,
        "missing",
        create_session=True,
    )
    replacement.abort()


def test_transcript_preserves_load_error_when_abort_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FailingLog:
        def load(self):
            raise ValueError("invalid transcript payload")

    class FailingLease:
        session_log = FailingLog()

        def abort(self):
            raise OSError("transcript cleanup interrupted")

    monkeypatch.setattr(
        SessionManager,
        "_acquire_session_lease",
        classmethod(lambda cls, *args, **kwargs: FailingLease()),
    )

    with pytest.raises(ValueError, match="invalid transcript payload") as raised:
        SessionManager._load_verified_transcript(workspace, "broken")

    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    assert "transcript cleanup interrupted" in notes


def test_session_lease_close_retries_stage_removal_without_reclosing_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "stage-retry.jsonl"
    write_completed_session(session_path, "local")
    from server import sessions as sessions_module

    lease = SessionManager._acquire_session_lease(
        workspace,
        "stage-retry",
        create_session=False,
    )
    lease.publish()
    lease.commit()
    stage_path = lease._stage_path
    stage_descriptor = lease._stage_descriptor
    original_close = sessions_module.os.close
    original_rmdir = sessions_module.os.rmdir
    stage_close_calls = 0
    failed = False

    def record_close(descriptor: int):
        nonlocal stage_close_calls
        if descriptor == stage_descriptor:
            stage_close_calls += 1
        return original_close(descriptor)

    def fail_stage_removal_once(path, *args, **kwargs):
        nonlocal failed
        if path == stage_path.name and not failed:
            failed = True
            raise OSError("stage removal interrupted")
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "close", record_close)
    monkeypatch.setattr(sessions_module.os, "rmdir", fail_stage_removal_once)

    with pytest.raises(OSError, match="stage removal interrupted"):
        lease.close()
    assert stage_path.is_dir()
    assert stage_close_calls == 1
    assert lease._stage_descriptor is None
    with pytest.raises(SessionResumeError, match="in use"):
        SessionManager._acquire_session_lease(
            workspace,
            "stage-retry",
            create_session=False,
        )

    lease.close()

    assert not stage_path.exists()
    assert lease._stage_descriptor is None


def test_new_empty_transcript_is_authoritative_then_deletion_is_a_conflict(
    client: TestClient, workspace: Path
):
    session_id = create_session(client, workspace)["session_id"]
    session_path = workspace / ".agent" / "sessions" / f"{session_id}.jsonl"

    assert session_path.is_file()
    assert session_path.read_bytes() == b""
    first = client.get(f"/api/sessions/{session_id}/transcript")
    assert first.status_code == 200
    assert first.json() == {"session_id": session_id, "messages": []}

    session_path.unlink()
    missing = client.get(f"/api/sessions/{session_id}/transcript")
    assert missing.status_code == 409
    assert missing.json()["error"]["type"] == "session_resume_error"
    assert "missing" in missing.json()["error"]["message"].lower()
    assert not session_path.exists()


def test_new_empty_session_artifacts_roll_back_if_metadata_insert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)

    def fail_create(_session: NewSession):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(metadata, "create_session", fail_create)

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="metadata insert failed"):
                await manager.create_session(
                    workspace=workspace,
                    mode="default",
                    context_mode="compaction",
                    title="Rollback",
                )
            sessions_dir = workspace / ".agent" / "sessions"
            artifacts = list(sessions_dir.iterdir())
            assert len(artifacts) == 1
            assert artifacts[0].suffix == ".lock"
            assert artifacts[0].read_text() == ""
            assert manager._runtimes == {}
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_session_record_construction_failure_leaves_no_ghost_row_or_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)
    session_id = "no-ghost"
    monkeypatch.setattr(manager, "_new_session_id", lambda: session_id)

    def fail_record(_row):
        raise sqlite3.OperationalError("record construction failed")

    monkeypatch.setattr(metadata, "_session_record", fail_record)

    async def scenario() -> None:
        try:
            with pytest.raises(sqlite3.OperationalError, match="record construction"):
                await manager.create_session(
                    workspace=workspace,
                    mode="default",
                    context_mode="compaction",
                    title="No ghost",
                )
            assert (
                metadata._connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                == 0
            )
            sessions_dir = workspace / ".agent" / "sessions"
            assert not (sessions_dir / f"{session_id}.jsonl").exists()
            assert not any(
                path.name.startswith(f".runtime-{session_id}-")
                for path in sessions_dir.iterdir()
            )
            assert manager._runtimes == {}
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_new_session_abort_preserves_preexisting_companion_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    sessions_dir = workspace / ".agent" / "sessions"
    sessions_dir.mkdir(parents=True)
    session_id = "collision"
    context_mode = sessions_dir / f"{session_id}.context-mode"
    context_mode.write_text("compaction\n")
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)
    monkeypatch.setattr(
        manager,
        "_new_session_id",
        lambda: session_id,
    )

    def fail_create(_session: NewSession):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(metadata, "create_session", fail_create)
    from server import runtime as runtime_module

    original_abort = getattr(runtime_module.HarnessRuntime, "abort", None)
    abort_calls = 0

    def interrupt_first_abort(runtime):
        nonlocal abort_calls
        abort_calls += 1
        if abort_calls == 1:
            raise OSError("abort interrupted")
        assert original_abort is not None
        return original_abort(runtime)

    monkeypatch.setattr(
        runtime_module.HarnessRuntime,
        "abort",
        interrupt_first_abort,
        raising=False,
    )

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="metadata insert failed"):
                await manager.create_session(
                    workspace=workspace,
                    mode="default",
                    context_mode="compaction",
                    title="Collision",
                )
            assert context_mode.read_text() == "compaction\n"
            assert not (sessions_dir / f"{session_id}.jsonl").exists()
            assert abort_calls == 2
            assert manager._runtimes == {}
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_new_session_abort_cleans_owned_artifacts_after_lock_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    manager = SessionManager(metadata, workspace, FakeLLM)
    session_id = "cleanup-retry"
    monkeypatch.setattr(manager, "_new_session_id", lambda: session_id)

    def fail_create(_session: NewSession):
        raise RuntimeError("metadata insert failed")

    monkeypatch.setattr(metadata, "create_session", fail_create)
    from server import sessions as sessions_module

    original_release = sessions_module._SecureSessionLease._release_lock
    release_calls = 0

    def fail_first_release(lease):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise OSError("lock release interrupted")
        return original_release(lease)

    monkeypatch.setattr(
        sessions_module._SecureSessionLease,
        "_release_lock",
        fail_first_release,
    )

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="metadata insert failed"):
                await manager.create_session(
                    workspace=workspace,
                    mode="default",
                    context_mode="compaction",
                    title="Cleanup retry",
                )
            sessions_dir = workspace / ".agent" / "sessions"
            assert sorted(path.suffix for path in sessions_dir.iterdir()) == [
                ".lock"
            ]
            assert not (sessions_dir / f"{session_id}.jsonl").exists()
            assert not (sessions_dir / f"{session_id}.context-mode").exists()
            assert release_calls == 2
            assert (sessions_dir / f"{session_id}.lock").read_text() == ""
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_constructor_cleanup_retries_runtime_owned_context_after_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = AppSettings(
        metadata_path=tmp_path / "metadata.sqlite3",
        base_workspace=workspace,
        launch_secret=SECRET,
        allowed_origins=frozenset({ORIGIN}),
    )
    session_id = "constructor-cleanup"
    from server import runtime as runtime_module
    from server import sessions as sessions_module

    monkeypatch.setattr(
        sessions_module.SessionManager,
        "_new_session_id",
        staticmethod(lambda: session_id),
    )

    def fail_registry(**_kwargs):
        raise ValueError("registry construction failed")

    monkeypatch.setattr(runtime_module, "build_registry", fail_registry)
    original_rollback = runtime_module.PreparedContext.rollback
    rollback_calls = 0

    def fail_first_rollback(context):
        nonlocal rollback_calls
        rollback_calls += 1
        if rollback_calls == 1:
            raise OSError("context rollback interrupted")
        return original_rollback(context)

    monkeypatch.setattr(
        runtime_module.PreparedContext,
        "rollback",
        fail_first_rollback,
    )

    with TestClient(
        create_app(settings, FakeLLM),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Constructor cleanup",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "session_resume_error"
    sessions_dir = workspace / ".agent" / "sessions"
    assert rollback_calls == 2
    assert not (sessions_dir / f"{session_id}.jsonl").exists()
    assert not (sessions_dir / f"{session_id}.context-mode").exists()
    assert (sessions_dir / f"{session_id}.lock").read_text() == ""


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("unreadable auth access-token-leak"),
        IsADirectoryError("auth path access-token-leak"),
        json.JSONDecodeError("access-token-leak", "secret-document", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "access-token-leak"),
        KeyError("access_token"),
        TypeError("'NoneType' object is not subscriptable: access-token-leak"),
        TypeError(
            "list indices must be integers or slices, not str: access-token-leak"
        ),
        RuntimeError(
            "no codex credentials at access-token-leak — run `codex login` first"
        ),
    ],
)
def test_every_local_credential_file_failure_is_a_fixed_non_disclosing_409(
    settings: AppSettings, workspace: Path, failure: Exception
):
    def fail_read(_path: Path):
        raise failure

    with TestClient(
        create_app(
            settings,
            codex_credential_factory(
                FakeLLM,
                settings.metadata_path.parent / "auth.json",
                read_text=fail_read,
            ),
        ),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        response = test_client.post(
            "/api/sessions",
            json={
                "workspace": str(workspace),
                "mode": "default",
                "context_mode": "compaction",
                "title": "Blocked",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "type": "credential_prerequisite",
        "message": "Codex credentials are required. Run `codex login` and retry.",
        "command": "codex login",
    }
    assert "access-token-leak" not in response.text
    assert SECRET not in response.text
    assert not (workspace / ".agent").exists()


@pytest.mark.parametrize("bug", ["type", "key", "os", "runtime"])
def test_natural_unrelated_factory_bugs_are_not_mislabeled_as_credentials(
    settings: AppSettings, workspace: Path, tmp_path: Path, bug: str
):
    def broken_factory():
        if bug == "type":
            value = None
            return value["tokens"]
        if bug == "key":
            return {}["tokens"]
        if bug == "os":
            return (tmp_path / "missing-model-cache").read_text()
        raise RuntimeError("no codex credentials; run `codex login` first")

    expected = {
        "type": TypeError,
        "key": KeyError,
        "os": FileNotFoundError,
        "runtime": RuntimeError,
    }[bug]

    with TestClient(
        create_app(settings, broken_factory),
        base_url=ORIGIN,
        headers=AUTH_HEADERS,
    ) as test_client:
        with pytest.raises(expected):
            test_client.post(
                "/api/sessions",
                json={
                    "workspace": str(workspace),
                    "mode": "default",
                    "context_mode": "compaction",
                    "title": "Bug",
                },
            )

    assert not (workspace / ".agent").exists()


def test_open_runtime_rolls_back_cache_and_lock_when_metadata_touch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "touch-fails.jsonl"
    write_completed_session(session_path, "local")
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.create_session(NewSession.defaults("touch-fails", workspace))
    manager = SessionManager(metadata, workspace, FakeLLM)

    def fail_touch(_session_id: str):
        raise RuntimeError("metadata touch failed")

    monkeypatch.setattr(metadata, "touch_session", fail_touch)

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="metadata touch failed"):
                await manager.open_runtime("touch-fails")
            assert manager._runtimes == {}
            assert_session_unlocked(session_path)
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_open_runtime_preserves_touch_error_when_rollback_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.create_session(NewSession.defaults("touch-cleanup", workspace))
    manager = SessionManager(metadata, workspace, FakeLLM)
    runtime = ClosingRuntime(
        "runtime", [], RuntimeError("runtime cleanup failed")
    )

    def fail_touch(_session_id: str):
        raise RuntimeError("metadata touch failed")

    monkeypatch.setattr(manager, "_construct_runtime", lambda _record: runtime)
    monkeypatch.setattr(metadata, "touch_session", fail_touch)

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="metadata touch failed") as raised:
                await manager.open_runtime("touch-cleanup")
            assert runtime.calls == ["runtime", "runtime"]
            assert manager._runtimes == {}
            assert "runtime cleanup failed" in "\n".join(raised.value.__notes__)
        finally:
            runtime.failure = None
            await manager.close()

    asyncio.run(scenario())


def test_manager_operations_reject_with_typed_error_after_close(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.create_session(NewSession.defaults("closed", workspace))
    manager = SessionManager(metadata, workspace, FakeLLM)

    async def scenario() -> None:
        await manager.close()
        for operation in (
            manager.list_sessions,
            lambda: manager.get_session("closed"),
            lambda: manager.rename_session("closed", "renamed"),
        ):
            with pytest.raises(RuntimeError) as raised:
                operation()
            assert type(raised.value).__name__ == "SessionManagerClosed"
        for operation in (
            manager.discover,
            lambda: manager.create_session(
                workspace=workspace,
                mode="default",
                context_mode="compaction",
                title="",
            ),
            lambda: manager.open_runtime("closed"),
            lambda: manager.transcript("closed"),
            lambda: manager.archive_session("closed"),
        ):
            with pytest.raises(RuntimeError) as raised:
                await operation()
            assert type(raised.value).__name__ == "SessionManagerClosed"

    asyncio.run(scenario())


def test_close_queued_before_open_and_archive_prevents_post_close_work(tmp_path: Path):
    workspace = tmp_path / "workspace"
    session_path = workspace / ".agent" / "sessions" / "queued.jsonl"
    write_completed_session(session_path, "local")
    metadata = MetadataStore(tmp_path / "metadata.sqlite3")
    metadata.create_session(NewSession.defaults("queued", workspace))
    manager = SessionManager(metadata, workspace, FakeLLM)

    async def scenario() -> None:
        await manager._runtime_lock.acquire()
        close_task = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        open_task = asyncio.create_task(manager.open_runtime("queued"))
        await asyncio.sleep(0)
        archive_task = asyncio.create_task(manager.archive_session("queued"))
        await asyncio.sleep(0)
        manager._runtime_lock.release()

        await close_task
        for task in (open_task, archive_task):
            with pytest.raises(RuntimeError) as raised:
                await task
            assert type(raised.value).__name__ == "SessionManagerClosed"
        assert manager._runtimes == {}
        assert not session_path.with_suffix(".lock").exists()

    asyncio.run(scenario())
