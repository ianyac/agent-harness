"""Injectable FastAPI application for the authenticated local service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from harness.llm import LLMClient
from harness.permissions import STARTUP_MODES
from server.auth import AuthBoundary, LaunchAuth
from server.context_mode import CONTEXT_MODES
from server.metadata import MetadataStore, SessionRecord
from server.protocol import dump_server_event, parse_client_event
from server.sessions import (
    ClientStateViolation,
    CredentialPrerequisite,
    InvalidSessionId,
    InvalidTitle,
    InvalidWorkspace,
    SessionManager,
    SessionManagerClosed,
    SessionNotFound,
    SessionResumeError,
)
from server.static import install_static_routes


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


def _workspace_branch(workspace: Path) -> str | None:
    """Read the checked-out branch from git metadata without running git."""
    git_path = workspace / ".git"
    try:
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8", errors="replace")
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = Path(pointer.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = workspace / git_dir
            head = git_dir / "HEAD"
        else:
            head = git_path / "HEAD"
        content = head.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if not content.startswith(prefix):
        return None
    return content[len(prefix):] or None


def _session_json(record: SessionRecord) -> dict:
    payload = asdict(record)
    payload["workspace"] = str(record.workspace)
    payload["branch"] = _workspace_branch(record.workspace)
    return payload


def _error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message}},
    )


def _record_cleanup_failure(original: BaseException, cleanup: BaseException) -> None:
    original.add_note(
        f"startup cleanup failed: {type(cleanup).__name__}: {cleanup}"
    )
    existing = tuple(getattr(original, "cleanup_errors", ()))
    original.cleanup_errors = (*existing, cleanup)  # type: ignore[attr-defined]


async def _send_connection_events(websocket: WebSocket, connection) -> None:
    while True:
        event_waiter = asyncio.create_task(connection.next_event())
        stop_waiter = asyncio.create_task(connection.stop.wait())
        waiters = {event_waiter, stop_waiter}
        try:
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        if stop_waiter in done:
            if connection.superseded:
                try:
                    await websocket.close(code=1000, reason="Superseded")
                except (OSError, RuntimeError, WebSocketDisconnect):
                    pass
            return
        try:
            await websocket.send_text(dump_server_event(event_waiter.result()))
        except (OSError, RuntimeError, WebSocketDisconnect):
            connection.stop.set()
            return


async def _receive_connection_events(websocket: WebSocket, connection) -> None:
    try:
        while True:
            receive_waiter = asyncio.create_task(websocket.receive())
            stop_waiter = asyncio.create_task(connection.stop.wait())
            waiters = {receive_waiter, stop_waiter}
            try:
                done, _ = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
            if stop_waiter in done:
                return
            message = receive_waiter.result()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if not isinstance(raw, str):
                await websocket.close(code=1008, reason="Invalid client frame")
                return
            try:
                event = parse_client_event(raw)
                connection.dispatch(event)
            except (ValidationError, ClientStateViolation):
                await websocket.close(code=1008, reason="Invalid client frame")
                return
    except WebSocketDisconnect:
        return
    finally:
        connection.stop.set()


def create_app(
    settings: AppSettings,
    llm_factory: Callable[[], LLMClient],
    *,
    static_root: Path | None = None,
) -> FastAPI:
    auth = LaunchAuth(settings.launch_secret, set(settings.allowed_origins))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        metadata = MetadataStore(settings.metadata_path)
        try:
            manager = SessionManager(
                metadata,
                settings.base_workspace,
                llm_factory,
                compact_threshold=settings.compact_threshold,
            )
        except BaseException as startup_error:
            try:
                metadata.close()
            except BaseException as cleanup_error:
                _record_cleanup_failure(startup_error, cleanup_error)
            raise
        app.state.session_manager = manager
        try:
            await manager.discover()
        except BaseException as startup_error:
            try:
                await manager.close()
            except BaseException as cleanup_error:
                _record_cleanup_failure(startup_error, cleanup_error)
            raise
        try:
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

    router = APIRouter(prefix="/api")

    def manager(request: Request) -> SessionManager:
        return request.app.state.session_manager

    @router.get("/health")
    async def health(request: Request) -> dict:
        return {
            "status": "ok",
            "service_id": manager(request).metadata.service_id,
        }

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

    @app.websocket("/ws/sessions/{session_id}")
    async def session_websocket(websocket: WebSocket, session_id: str) -> None:
        selected_protocol = websocket.scope["state"][
            "auth_websocket_subprotocol"
        ]
        await websocket.accept(subprotocol=selected_protocol)
        connection = None
        try:
            try:
                connection = await websocket.app.state.session_manager.connect(
                    session_id
                )
            except (
                InvalidSessionId,
                SessionNotFound,
                SessionResumeError,
                SessionManagerClosed,
            ):
                await websocket.close(code=1008, reason="Session unavailable")
                return
            snapshot = await connection.next_event()
            await websocket.send_text(dump_server_event(snapshot))
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(_send_connection_events(websocket, connection))
                tasks.create_task(_receive_connection_events(websocket, connection))
        finally:
            if connection is not None:
                connection.disconnected()

    app.include_router(router)

    @app.websocket("/{path:path}")
    async def unknown_websocket(websocket: WebSocket, _path: str) -> None:
        selected_protocol = websocket.scope["state"][
            "auth_websocket_subprotocol"
        ]
        await websocket.accept(subprotocol=selected_protocol)
        await websocket.close(code=1008, reason="Unknown WebSocket route")

    if static_root is not None:
        install_static_routes(app, static_root)
    app.add_middleware(
        AuthBoundary,
        auth=auth,
        static_enabled=static_root is not None,
    )
    return app
