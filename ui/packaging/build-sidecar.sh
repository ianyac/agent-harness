#!/usr/bin/env bash
# Build the PyInstaller sidecar bundle consumed by the Tauri app as a
# resource directory (see desktop/src-tauri/tauri.conf.json "resources").
# Run from ui/:  packaging/build-sidecar.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f frontend/dist/index.html ]]; then
  echo "frontend/dist missing; run: cd frontend && npm run build" >&2
  exit 1
fi

uv sync --group packaging
uv run --group packaging pyinstaller \
  --distpath packaging/dist \
  --workpath packaging/build \
  --noconfirm \
  packaging/sidecar.spec

# Warm run: triggers macOS code assessment of the fresh binaries once, so the
# first real launch is not spent inside Gatekeeper.
packaging/dist/agent-harness-sidecar/agent-harness-sidecar --help >/dev/null 2>&1 || true

echo "built packaging/dist/agent-harness-sidecar/"
