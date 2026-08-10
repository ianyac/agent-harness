# PyInstaller spec for the agent-harness-sidecar bundle.
# Build via packaging/build-sidecar.sh (run from ui/).
#
# One-directory mode is deliberate: the Tauri bundle ships the directory as a
# resource, so the installed Mach-O files keep stable inodes and macOS code
# assessment is paid once per install instead of on every launch (one-file
# extraction re-validates fresh inodes on each start and can stall for
# seconds to minutes per launch).

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

UI_ROOT = Path(SPECPATH).resolve().parent
REPO_ROOT = UI_ROOT.parent

frontend_dist = UI_ROOT / "frontend" / "dist"
if not (frontend_dist / "index.html").is_file():
    raise SystemExit("frontend/dist is missing; run `cd frontend && npm run build` first")

analysis = Analysis(
    [str(UI_ROOT / "packaging" / "sidecar_entry.py")],
    pathex=[str(UI_ROOT), str(REPO_ROOT)],
    datas=[
        (str(frontend_dist), "frontend/dist"),
        (str(REPO_ROOT / "vendor" / "tiktoken"), "vendor/tiktoken"),
    ],
    hiddenimports=[
        *collect_submodules("harness"),
        *collect_submodules("uvicorn"),
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="agent-harness-sidecar",
    console=True,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    name="agent-harness-sidecar",
    strip=False,
    upx=False,
)
