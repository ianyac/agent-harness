"""PyInstaller entrypoint: run the UI service in sidecar (secret-stdin) mode.

The Tauri host spawns the sidecar with no arguments and no environment
overrides, so this entry hard-wires `--secret-stdin` and points the tokenizer
cache at the bundled copy before any server module can capture the default.
"""

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    bundle_root = Path(getattr(sys, "_MEIPASS"))
    os.environ["TIKTOKEN_CACHE_DIR"] = str(bundle_root / "vendor" / "tiktoken")

from server.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["--secret-stdin"]))
