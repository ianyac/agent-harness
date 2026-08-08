"""Loopback-only browser and sidecar entrypoint for the local UI service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from typing import Callable
from urllib.parse import urlencode

from harness.llm import CodexAdapter
from platformdirs import user_data_path
import uvicorn

from server.app import AppSettings, create_app
from server.sessions import CodexCredentialFactory, InvalidWorkspace
from server.static import frontend_dist


LOOPBACK_HOST = "127.0.0.1"
TAURI_ORIGIN = "tauri://localhost"
MAX_BOOTSTRAP_BYTES = 8 * 1024


class BootstrapInputError(ValueError):
    """A deliberately non-disclosing bootstrap protocol failure."""


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 0 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Agent Harness UI service")
    parser.add_argument("--workspace")
    parser.add_argument("--host", choices=[LOOPBACK_HOST], default=LOOPBACK_HOST)
    parser.add_argument("--port", type=_port, default=0)
    parser.add_argument(
        "--metadata-db",
        type=Path,
        default=user_data_path("agent-harness-ui") / "metadata.sqlite3",
    )
    parser.add_argument("--secret-stdin", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.secret_stdin and arguments.workspace is None:
        parser.error("--workspace is required unless --secret-stdin is used")
    return arguments


def _validated_workspace(value: object) -> Path:
    if not isinstance(value, str):
        raise InvalidWorkspace("workspace must be a string")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise InvalidWorkspace("workspace must be an absolute directory")
    if candidate.is_symlink():
        raise InvalidWorkspace("workspace must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InvalidWorkspace("workspace must be an existing directory") from error
    if not resolved.is_dir():
        raise InvalidWorkspace("workspace must be an existing directory")
    return resolved


def _read_one_bounded_line() -> bytes:
    descriptor = sys.stdin.buffer.fileno()
    payload = bytearray()
    while True:
        remaining = MAX_BOOTSTRAP_BYTES + 1 - len(payload)
        if remaining <= 0:
            raise BootstrapInputError
        chunk = os.read(descriptor, min(4096, remaining))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > MAX_BOOTSTRAP_BYTES:
            raise BootstrapInputError
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise BootstrapInputError
    return bytes(payload[:-1])


def _read_sidecar_bootstrap(
    expected_workspace: Path | None,
) -> tuple[str, Path]:
    try:
        raw = _read_one_bounded_line()
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {
            "type",
            "secret",
            "workspace",
        }:
            raise BootstrapInputError
        if document["type"] != "bootstrap":
            raise BootstrapInputError
        secret = document["secret"]
        if not isinstance(secret, str) or not secret or len(secret) > 1024:
            raise BootstrapInputError
        payload_workspace = _validated_workspace(document["workspace"])
        if expected_workspace is not None and payload_workspace != expected_workspace:
            raise BootstrapInputError
        return secret, payload_workspace
    except BootstrapInputError:
        raise
    except (KeyError, OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise BootstrapInputError from None


def _listener(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK_HOST, port))
    except BaseException:
        listener.close()
        raise
    return listener


class _ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, on_ready: Callable[[], None]):
        super().__init__(config)
        self._on_ready = on_ready

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            try:
                self._on_ready()
            except BrokenPipeError:
                self.should_exit = True


def _emit_line(value: str) -> None:
    sys.stdout.write(value + "\n")
    sys.stdout.flush()


def _llm_factory() -> CodexCredentialFactory:
    return CodexCredentialFactory(
        CodexAdapter,
        credential_path=Path.home() / ".codex" / "auth.json",
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    workspace = None
    if arguments.workspace is not None:
        try:
            workspace = _validated_workspace(arguments.workspace)
        except InvalidWorkspace:
            sys.stderr.write("server startup failed: invalid workspace\n")
            return 2

    if arguments.secret_stdin:
        try:
            launch_secret, workspace = _read_sidecar_bootstrap(workspace)
        except BootstrapInputError:
            sys.stderr.write("server startup failed: invalid bootstrap input\n")
            return 2
    else:
        launch_secret = secrets.token_urlsafe(32)
    assert workspace is not None

    try:
        listener = _listener(arguments.port)
    except OSError:
        sys.stderr.write("server startup failed: unable to bind loopback port\n")
        return 2

    try:
        actual_port = listener.getsockname()[1]
        origin = f"http://{LOOPBACK_HOST}:{actual_port}"
        settings = AppSettings(
            metadata_path=arguments.metadata_db,
            base_workspace=workspace,
            launch_secret=launch_secret,
            allowed_origins=frozenset({origin, TAURI_ORIGIN}),
        )
        app = create_app(
            settings,
            _llm_factory(),
            static_root=frontend_dist(),
        )

        def announce() -> None:
            if arguments.secret_stdin:
                _emit_line(
                    json.dumps(
                        {
                            "type": "server-ready",
                            "host": LOOPBACK_HOST,
                            "port": actual_port,
                        },
                        separators=(",", ":"),
                    )
                )
            else:
                query = urlencode({"token": launch_secret})
                _emit_line(f"{origin}/bootstrap?{query}")

        config = uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=actual_port,
            access_log=False,
            log_level="warning",
        )
        server = _ReadyServer(config, announce)
        server.run(sockets=[listener])
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
