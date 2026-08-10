"""Packaged-sidecar smoke test mirroring the Tauri host contract.

Run from ui/ after packaging/build-sidecar.sh:

    uv run python packaging/smoke_sidecar.py

Spawns the staged binary with no arguments, performs the one-shot stdin
bootstrap, waits for the readiness record on stdout, exercises authenticated
REST from the Tauri origin, then terminates it and verifies a clean exit.
"""

from __future__ import annotations

import json
import secrets
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NoReturn

import httpx

TAURI_ORIGIN = "tauri://localhost"
READINESS_TIMEOUT = 15.0
EXIT_TIMEOUT = 5.0


def fail(message: str) -> NoReturn:
    raise SystemExit(f"smoke failed: {message}")


def staged_binary(ui_root: Path) -> Path:
    binary = (
        ui_root / "packaging" / "dist" / "agent-harness-sidecar" / "agent-harness-sidecar"
    )
    if not binary.is_file():
        fail("no built sidecar; run packaging/build-sidecar.sh")
    return binary


def main() -> int:
    ui_root = Path(__file__).resolve().parents[1]
    binary = staged_binary(ui_root)
    launch_secret = secrets.token_urlsafe(32)

    with tempfile.TemporaryDirectory() as scratch:
        workspace = Path(scratch) / "workspace"
        workspace.mkdir()
        codex_dir = Path(scratch) / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text(json.dumps({
            "tokens": {
                "access_token": "smoke-fixture-token",
                "account_id": "smoke-fixture-account",
            },
        }))
        bootstrap = json.dumps(
            {"type": "bootstrap", "secret": launch_secret, "workspace": str(workspace)},
            separators=(",", ":"),
        )
        process = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"HOME": scratch, "TMPDIR": scratch, "PATH": "/usr/bin:/bin"},
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(bootstrap.encode() + b"\n")
            process.stdin.flush()
            process.stdin.close()

            started = time.monotonic()
            line = process.stdout.readline()
            elapsed = time.monotonic() - started
            if elapsed > READINESS_TIMEOUT:
                fail(f"readiness took {elapsed:.1f}s (host budget is 15s)")
            record = json.loads(line)
            if record.get("type") != "server-ready" or record.get("host") != "127.0.0.1":
                fail(f"unexpected readiness record: {record!r}")
            port = record["port"]
            if not isinstance(port, int) or port <= 0:
                fail(f"invalid port: {port!r}")
            if launch_secret in line.decode(errors="replace"):
                fail("launch secret leaked to stdout")

            base = f"http://127.0.0.1:{port}"
            headers = {
                "Authorization": f"Bearer {launch_secret}",
                "Origin": TAURI_ORIGIN,
            }
            bare = httpx.get(
                f"{base}/api/health", headers={"Origin": TAURI_ORIGIN}, timeout=10.0
            )
            if bare.status_code != 401:
                fail("unauthenticated health did not return 401")
            with httpx.Client(base_url=base, headers=headers, timeout=10.0) as client:
                health = client.get("/api/health")
                if health.status_code != 200:
                    fail(f"health returned {health.status_code}")
                config = client.get("/api/config")
                if config.json().get("base_workspace") != str(workspace.resolve()):
                    fail(f"config workspace mismatch: {config.json()!r}")
                created = client.post(
                    "/api/sessions",
                    json={
                        "workspace": str(workspace),
                        "mode": "default",
                        "context_mode": "compaction",
                        "title": "Smoke",
                    },
                )
                if created.status_code != 201:
                    fail(f"session create returned {created.status_code}: {created.text}")
                session_id = created.json()["session_id"]
                listed = client.get("/api/sessions").json()
                if [row["session_id"] for row in listed] != [session_id]:
                    fail(f"session list mismatch: {listed!r}")
                transcript = client.get(f"/api/sessions/{session_id}/transcript")
                if transcript.status_code != 200 or transcript.json()["messages"] != []:
                    fail(f"transcript read failed: {transcript.status_code}")
                safety = client.get(f"/api/sessions/{session_id}/safety")
                if safety.status_code != 200 or "sandbox" not in safety.text:
                    fail(f"safety read failed: {safety.status_code}")

            process.send_signal(signal.SIGTERM)
            try:
                code = process.wait(timeout=EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                fail("sidecar did not exit within 5s of SIGTERM")
            if not (workspace / ".agent" / "sessions").is_dir():
                fail("session artifacts were not written to the workspace")
            print(
                f"smoke ok: {binary.name} ready in {elapsed:.1f}s on port {port}, "
                f"exit code {code}"
            )
            return 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
