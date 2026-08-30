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
_cache_mtime = None      # wordlist.json 的 mtime, 跨进程写入后自动重载


def _read_mtime():
    try:
        return WORDLIST_FILE.stat().st_mtime_ns
    except OSError:
        return None


def _load(force=False):
    """读词表; 文件 mtime 变化 (另一进程写过) 则重载, 避免脏写覆盖."""
    global _cache, _cache_mtime
    mtime = _read_mtime()
    if force or _cache is None or mtime != _cache_mtime:
        try:
            with open(WORDLIST_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _cache = {"pairs": {}}
        if "pairs" not in _cache:
            _cache = {"pairs": {}}
        _cache_mtime = mtime
    return _cache


def _save():
    POLISH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WORDLIST_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, WORDLIST_FILE)
    global _cache_mtime
    _cache_mtime = _read_mtime()


_LOCKFILE = WORDLIST_FILE.with_suffix(".lock")


def _pfile_lock(timeout_s=5.0):
    """跨进程文件锁 (O_CREAT|O_EXCL 自旋, Windows/Linux 通用)."""
    t0 = time.monotonic()
    POLISH_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(_LOCKFILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return fd
        except FileExistsError:
            if time.monotonic() - t0 > timeout_s:
                return None  # 拿不到锁就放弃本次持久化, 计数下次再补
            try:
                if _LOCKFILE.stat().st_mtime < time.time() - timeout_s:
                    os.unlink(_LOCKFILE)  # 清理残留死锁
            except OSError:
                pass
            time.sleep(0.02)


def _pfile_unlock(fd):
    if fd is not None:
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(_LOCKFILE)
            except OSError:
                pass


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
    pfd = _pfile_lock()
    try:
        with _lock:
            data = _load(force=True)  # 拿到锁后重读, 融合其他进程的写入
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
    finally:
        _pfile_unlock(pfd)
    return learned
