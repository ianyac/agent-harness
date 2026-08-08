import os
import sys
from pathlib import Path

UI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = UI_ROOT.parent

if (REPO_ROOT / "harness").is_dir() and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR", str(REPO_ROOT / "vendor" / "tiktoken")
)
