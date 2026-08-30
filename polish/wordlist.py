"""个人词表: 即学即用的本地替换表 (全本地, 无网络)."""

import json
import os
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

from .paths import POLISH_DIR

WORDLIST_FILE = POLISH_DIR / "wordlist.json"
MAX_KEY_LEN = 24          # 超长片段不进词表 (多半是整句重写, 不是习惯词)
MIN_OCCURRENCE = 1        # 出现即生效; 达到 CONFIRM_COUNT 后标记确认
CONFIRM_COUNT = 2

_lock = threading.Lock()
_cache = None            # {"pairs": {src: {"dst":.., "n":.., "confirmed":..}}}


def _load():
    global _cache
    if _cache is None:
        try:
            with open(WORDLIST_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _cache = {"pairs": {}}
        if "pairs" not in _cache:
            _cache = {"pairs": {}}
    return _cache


def _save():
    POLISH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WORDLIST_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, WORDLIST_FILE)


def apply(text: str) -> str:
    """词表快路径: 按已确认映射替换. <1ms."""
    with _lock:
        pairs = {k: v for k, v in _load()["pairs"].items() if v.get("confirmed", False)}
    if not pairs:
        return text
    # 长词优先, 避免短词嵌在长词内被抢先替换
    for src in sorted(pairs, key=len, reverse=True):
        if src and src in text:
            text = text.replace(src, pairs[src]["dst"])
    return text


def learn_from_pairs(pairs):
    """[(src, dst), ...] -> 计数并持久化. 出现 CONFIRM_COUNT 次即确认生效."""
    learned = []
    with _lock:
        data = _load()
        for src, dst in pairs:
            src, dst = (src or "").strip(), (dst or "").strip()
            if not src or not dst or src == dst:
                continue
            if len(src) > MAX_KEY_LEN or len(dst) > MAX_KEY_LEN:
                continue
            # 同音/近形才更像习惯词而非随意改写; 放宽为相似度>=0.4
            if SequenceMatcher(None, src, dst).ratio() < 0.4:
                continue
            ent = data["pairs"].setdefault(src, {"dst": dst, "n": 0, "confirmed": False, "ts": 0})
            if ent.get("dst") == dst:
                ent["n"] += 1
            else:  # 同一 src 改成不同 dst, 以最新为准重新计数
                ent.update(dst=dst, n=1, confirmed=False)
            ent["ts"] = int(time.time())
            if ent["n"] >= CONFIRM_COUNT:
                ent["confirmed"] = True
            learned.append((src, dst, ent["n"], ent["confirmed"]))
        if learned:
            _save()
    return learned
