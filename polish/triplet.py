"""三元组采集: (raw, polished, final) + 字符级 diff, 全本地 JSONL.

数据格式 (每行一条):
{"ts": 169..., "app": "...", "raw": "...", "polished": "...", "final": "...",
 "user_edited": true, "diff": [["replace", 3, 5, "盘古"], ...]}
diff 基于 (polished -> final) 的 difflib.SequenceMatcher opcodes,
区间为相对 polished 的字符下标 [i1,i2), 替换文本来自 final[j1:j2].
"""

import json
import threading
import time
from difflib import SequenceMatcher

from .paths import TRIPLET_FILE
from . import wordlist

_lock = threading.Lock()


def diff_ops(a: str, b: str):
    ops = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        ops.append([tag, i1, i2, b[j1:j2]])
    return ops


def record(raw: str, polished: str, final: str, app: str = ""):
    """落盘三元组; 若用户改过, 顺带喂词表学习. 任何异常静默吞掉."""
    try:
        user_edited = (final != polished)
        ops = diff_ops(polished, final) if user_edited else []
        entry = {
            "ts": int(time.time()), "app": app or "",
            "raw": raw, "polished": polished, "final": final,
            "user_edited": user_edited, "diff": ops,
        }
        with _lock:
            TRIPLET_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(TRIPLET_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if user_edited:
            # diff 的 replace/delete 片段作为 (src, dst) 候选喂给词表
            pairs = [(polished[i1:i2], dst) for tag, i1, i2, dst in ops if tag in ("replace", "delete")]
            if pairs:
                wordlist.learn_from_pairs(pairs)
    except Exception:
        pass
