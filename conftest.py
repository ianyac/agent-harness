import os
from pathlib import Path

# keep the default suite offline: tiktoken loads its BPE file from the
# vendored copy instead of downloading it on a cold-cache machine
os.environ.setdefault(
    "TIKTOKEN_CACHE_DIR", str(Path(__file__).parent / "vendor" / "tiktoken")
)
