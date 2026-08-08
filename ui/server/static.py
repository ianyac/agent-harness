"""Authenticated static frontend and SPA fallback serving."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import sys

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from server.auth import LaunchAuth


_SERVICE_NAMESPACES = frozenset({"api", "ws"})
_STATIC_METHODS = ("GET", "HEAD")
_FALLBACK_METHODS = (*_STATIC_METHODS, "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
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


def _is_service_path(path: str) -> bool:
    first, _separator, _rest = path.partition("/")
    return first in _SERVICE_NAMESPACES


def _safe_file(root: Path, path: str) -> Path | None:
    if "\\" in path or "\x00" in path:
        return None
    relative = PurePosixPath(path)
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        return None
    try:
        candidate = root.joinpath(*relative.parts).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def install_static_routes(
    app: FastAPI,
    root: Path,
    auth: LaunchAuth,
) -> None:
    """Install an authenticated frontend route after API and WS routes."""
    configured_root = Path(root)

    @app.api_route(
        "/{path:path}",
        methods=list(_FALLBACK_METHODS),
        dependencies=[Depends(auth.require_http)],
        include_in_schema=False,
    )
    async def serve_frontend(request: Request, path: str) -> Response:
        if _is_service_path(path):
            return _not_found()
        if request.method not in _STATIC_METHODS:
            return Response(
                status_code=405,
                headers={"Allow": ", ".join(_STATIC_METHODS)},
            )
        try:
            resolved_root = configured_root.resolve(strict=True)
        except (OSError, RuntimeError):
            return PlainTextResponse(_MISSING_BUILD, status_code=503)
        if not resolved_root.is_dir():
            return PlainTextResponse(_MISSING_BUILD, status_code=503)

        requested = _safe_file(resolved_root, path) if path else None
        if requested is not None:
            return FileResponse(requested)

        if path == "assets" or path.startswith("assets/"):
            return _not_found()

        index = _safe_file(resolved_root, "index.html")
        if index is None:
            return PlainTextResponse(_MISSING_BUILD, status_code=503)
        return FileResponse(index, media_type="text/html")
