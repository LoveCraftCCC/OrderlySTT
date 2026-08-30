"""polish 层本地数据目录: %APPDATA%\\Vernest\\polish (与主程序同一数据根)."""

import os
import sys
from pathlib import Path


def _app_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return Path(base) / "Vernest"
    return Path.home() / ".vernest"


POLISH_DIR = _app_data_root() / "polish"
TRIPLET_FILE = POLISH_DIR / "triplets.jsonl"
WORDLIST_FILE = POLISH_DIR / "wordlist.json"
