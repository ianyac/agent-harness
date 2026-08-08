import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
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
from server.sessions import SessionManager


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
    assert not session_path.with_suffix(".lock").exists()
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
    assert first.headers["location"] == "/"
    assert "httponly" in first.headers["set-cookie"].lower()
    assert SECRET not in first.headers["location"]
    assert second.status_code == 401
    assert SECRET not in second.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_framework_documentation_does_not_add_unauthenticated_routes(app, path: str):
    with TestClient(app, base_url=ORIGIN) as anonymous:
        response = anonymous.get(path)

    assert response.status_code == 404


def test_missing_credentials_return_typed_non_disclosing_prerequisite(
    settings: AppSettings, workspace: Path
):
    token_material = "access-token-must-not-escape"

    def missing_credentials():
        raise RuntimeError(
            f"no codex credentials ({token_material}); run `codex login` first"
        )

    app = create_app(settings, missing_credentials)
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
    def invalid_credentials():
        raise failure

    with TestClient(
        create_app(settings, invalid_credentials),
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
    with TestClient(
        create_app(settings, FakeLLM),
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
    assert not session_path.with_suffix(".lock").exists()


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
    assert not session_path.with_suffix(".lock").exists()


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
    def __init__(self, calls: list[str]):
        self.calls = calls

    def close(self) -> None:
        self.calls.append("metadata")


def test_manager_close_attempts_every_runtime_and_metadata_after_an_error(
    tmp_path: Path,
):
    calls: list[str] = []
    manager = SessionManager(ClosingMetadata(calls), tmp_path, FakeLLM)
    manager._runtimes.update(
        {
            "one": ClosingRuntime("one", calls, RuntimeError("close failed")),
            "two": ClosingRuntime("two", calls),
        }
    )

    with pytest.raises(ExceptionGroup, match="session manager close failed"):
        asyncio.run(manager.close())

    assert calls == ["one", "two", "metadata"]
    assert manager._runtimes == {}


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

    assert not lock_path.exists()
    with pytest.raises(Exception):
        manager.metadata.get_session(session_id)
