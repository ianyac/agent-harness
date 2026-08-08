"""Authenticated static frontend and SPA fallback serving."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import sys

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

_STATIC_METHODS = ("GET", "HEAD")
_MISSING_BUILD = (
    "Frontend build is unavailable. Run `cd frontend && npm run build`, "
    "then restart the service."
)


def resource_root() -> Path:
    """Return the stable root used for bundled runtime resources."""
    if getattr(sys, "frozen", False):
        bundled_root = getattr(sys, "_MEIPASS", None)
        if not isinstance(bundled_root, (str, bytes)):
            raise RuntimeError("frozen resource root is unavailable")
        return Path(bundled_root)
    return Path(__file__).resolve().parents[1]


def frontend_dist() -> Path:
    """Locate the shared frontend build in development or PyInstaller."""
    return resource_root() / "frontend" / "dist"


def _not_found() -> Response:
    return Response(status_code=404)


class _UnsafeStaticPath(ValueError):
    """A path that must never be treated as an ordinary SPA miss."""


def _validate_static_path(path: str) -> None:
    if "\\" in path or "\x00" in path or path.startswith("/"):
        raise _UnsafeStaticPath
    if path and any(part in {"", ".", ".."} for part in path.split("/")):
        raise _UnsafeStaticPath


def _safe_file(root: Path, path: str) -> Path | None:
    _validate_static_path(path)
    relative = PurePosixPath(path)
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        raise _UnsafeStaticPath
    try:
        candidate = root.joinpath(*relative.parts).resolve(strict=False)
    except (OSError, RuntimeError):
        raise _UnsafeStaticPath from None
    if not candidate.is_relative_to(root):
        raise _UnsafeStaticPath
    if not candidate.is_file():
        return None
    return candidate


class _StaticFallback:
    """Serve only requests already authorized by the outer path boundary."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        request = Request(scope, receive=receive)
        response = self._response(request)
        await response(scope, receive, send)

    def _response(self, request: Request) -> Response:
        path = getattr(request.state, "browser_static_path", None)
        if not isinstance(path, str):
            return _not_found()
        if request.method not in _STATIC_METHODS:
            return Response(
                status_code=405,
                headers={"Allow": ", ".join(_STATIC_METHODS)},
            )
        try:
            _validate_static_path(path)
        except _UnsafeStaticPath:
            return _not_found()
        try:
            resolved_root = self._root.resolve(strict=True)
        except (OSError, RuntimeError):
            return PlainTextResponse(_MISSING_BUILD, status_code=503)
        if not resolved_root.is_dir():
            return PlainTextResponse(_MISSING_BUILD, status_code=503)

        try:
            requested = _safe_file(resolved_root, path) if path else None
        except _UnsafeStaticPath:
            return _not_found()
        if requested is not None:
            return FileResponse(requested)

        if path == "assets" or path.startswith("assets/"):
            return _not_found()

        try:
            index = _safe_file(resolved_root, "index.html")
        except _UnsafeStaticPath:
            index = None
        if index is None:
            return PlainTextResponse(_MISSING_BUILD, status_code=503)
        return FileResponse(index, media_type="text/html")


def install_static_routes(
    app: FastAPI,
    root: Path,
) -> None:
    """Install a path-capability frontend route after API and WS routes."""
    app.router.routes.append(
        Route(
            "/{path:path}",
            endpoint=_StaticFallback(root),
            methods=None,
            name="authenticated-static-fallback",
            include_in_schema=False,
        )
    )
