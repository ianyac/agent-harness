"""Injectable FastAPI application for the authenticated local service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from harness.llm import LLMClient
from harness.permissions import STARTUP_MODES
from server.auth import LaunchAuth
from server.context_mode import CONTEXT_MODES
from server.metadata import MetadataStore, SessionRecord
from server.sessions import (
    CredentialPrerequisite,
    InvalidSessionId,
    InvalidTitle,
    InvalidWorkspace,
    SessionManager,
    SessionNotFound,
    SessionResumeError,
)


@dataclass(frozen=True)
class AppSettings:
    metadata_path: Path
    base_workspace: Path
    launch_secret: str
    allowed_origins: frozenset[str]
    compact_threshold: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata_path", Path(self.metadata_path))
        object.__setattr__(self, "base_workspace", Path(self.base_workspace))
        object.__setattr__(self, "allowed_origins", frozenset(self.allowed_origins))


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str
    mode: Literal["default", "acceptAll", "readOnly"] = "default"
    context_mode: Literal["compaction", "folding"] = "compaction"
    title: str = Field(default="New session", min_length=1, max_length=512)


class RenameSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)


def _session_json(record: SessionRecord) -> dict:
    payload = asdict(record)
    payload["workspace"] = str(record.workspace)
    return payload


def _error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message}},
    )


def create_app(
    settings: AppSettings,
    llm_factory: Callable[[], LLMClient],
) -> FastAPI:
    auth = LaunchAuth(settings.launch_secret, set(settings.allowed_origins))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        metadata = MetadataStore(settings.metadata_path)
        manager = SessionManager(
            metadata,
            settings.base_workspace,
            llm_factory,
            compact_threshold=settings.compact_threshold,
        )
        app.state.session_manager = manager
        try:
            await manager.discover()
            yield
        finally:
            await manager.close()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.auth = auth

    @app.exception_handler(InvalidSessionId)
    async def invalid_session_id_handler(_request: Request, _error: InvalidSessionId):
        return _error_response(404, "session_not_found", "Session not found.")

    @app.exception_handler(SessionNotFound)
    async def session_not_found_handler(_request: Request, _error: SessionNotFound):
        return _error_response(404, "session_not_found", "Session not found.")

    @app.exception_handler(InvalidWorkspace)
    async def invalid_workspace_handler(_request: Request, error: InvalidWorkspace):
        return _error_response(422, "invalid_workspace", str(error))

    @app.exception_handler(InvalidTitle)
    async def invalid_title_handler(_request: Request, error: InvalidTitle):
        return _error_response(422, "invalid_title", str(error))

    @app.exception_handler(CredentialPrerequisite)
    async def credential_handler(_request: Request, _error: CredentialPrerequisite):
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "type": "credential_prerequisite",
                    "message": "Codex credentials are required. Run `codex login` and retry.",
                    "command": "codex login",
                }
            },
        )

    @app.exception_handler(SessionResumeError)
    async def resume_handler(_request: Request, error: SessionResumeError):
        return _error_response(409, "session_resume_error", str(error))

    @app.get("/bootstrap", include_in_schema=False)
    async def bootstrap(token: str | None = None):
        return auth.bootstrap_response(token)

    router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_http)])

    def manager(request: Request) -> SessionManager:
        return request.app.state.session_manager

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @router.get("/config")
    async def config() -> dict:
        return {
            "base_workspace": str(settings.base_workspace.resolve()),
            "default_mode": "default",
            "default_context_mode": "compaction",
            "modes": list(STARTUP_MODES),
            "context_modes": list(CONTEXT_MODES),
        }

    @router.get("/sessions")
    async def list_sessions(request: Request) -> list[dict]:
        return [_session_json(row) for row in manager(request).list_sessions()]

    @router.post("/sessions", status_code=201)
    async def create_session(request: Request, body: CreateSessionRequest) -> dict:
        record = await manager(request).create_session(
            workspace=body.workspace,
            mode=body.mode,
            context_mode=body.context_mode,
            title=body.title,
        )
        return _session_json(record)

    @router.get("/sessions/{session_id}")
    async def get_session(request: Request, session_id: str) -> dict:
        return _session_json(manager(request).get_session(session_id))

    @router.patch("/sessions/{session_id}")
    async def rename_session(
        request: Request, session_id: str, body: RenameSessionRequest
    ) -> dict:
        return _session_json(manager(request).rename_session(session_id, body.title))

    @router.delete("/sessions/{session_id}", status_code=204)
    async def archive_session(request: Request, session_id: str) -> Response:
        await manager(request).archive_session(session_id)
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/transcript")
    async def transcript(request: Request, session_id: str) -> dict:
        messages = await manager(request).transcript(session_id)
        return {"session_id": session_id, "messages": messages}

    @router.get("/sessions/{session_id}/safety")
    async def safety(request: Request, session_id: str) -> dict:
        return await manager(request).safety(session_id)

    app.include_router(router)
    return app
