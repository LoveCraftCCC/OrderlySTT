"""润色流水线入口 (voice_core/runtime.py 只 import 这里).

铁律: 任何异常/超时 -> 返回原文, 绝不阻塞输入主路径.
"""

import json
import time
import urllib.request
import urllib.error

from . import wordlist, triplet

POLISH_DEFAULTS = {
    "enabled": False,
    "endpoint": "http://127.0.0.1:47640",
    "budget_ms": 1500,      # 润色总预算 (含网络); 云端往返实测 ~0.6-0.8s, 800 压线
    "dwell_enabled": True,  # 驻留浮窗
    "dwell_ms": 1500,       # 无操作自动上屏延时
    "learn_enabled": True,  # 三元组采集 + 词表学习
}


def get_cfg(runtime_config: dict) -> dict:
    cfg = dict(POLISH_DEFAULTS)
    user = runtime_config.get("polish") or {}
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in POLISH_DEFAULTS})
    return cfg


def _call_server(text: str, endpoint: str, budget_ms: int) -> str:
    payload = json.dumps({"text": text, "budget_ms": budget_ms}).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/polish", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    timeout = max(0.3, budget_ms / 1000.0 + 0.5)  # 含排队余量; 客户端侧仍受预算约束
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if time.monotonic() - t0 > budget_ms / 1000.0:
        return text  # 服务端返回了但已超预算, 丢弃, 保证体感
    out = (data.get("polished") or "").strip()
    return out if out else text


def polish_with_timeout(raw: str, runtime_config: dict) -> str:
    """raw -> 润色文本. 失败/超时一律返回 raw."""
    cfg = get_cfg(runtime_config)
    if not cfg["enabled"]:
        return raw
    # ① 词表快路径 (本地, 即学即用)
    try:
        txt = wordlist.apply(raw)
    except Exception:
        txt = raw
    # ② 小模型润色 (服务不可用则静默跳过)
    try:
        txt = _call_server(txt, cfg["endpoint"], cfg["budget_ms"])
    except Exception:
        pass
    return txt if txt and txt.strip() else raw


def dwell_and_capture(raw: str, polished: str, runtime_config: dict,
                      log=print) -> str:
    """驻留浮窗 (可选) + 三元组采集. 返回最终要上屏的文本.

    浮窗关闭/不可用时直接返回 polished, 主路径不受影响.
    """
    cfg = get_cfg(runtime_config)
    final = polished
    if cfg["dwell_enabled"]:
        try:
            from .overlay import dwell_overlay
            final = dwell_overlay(polished, dwell_ms=cfg["dwell_ms"]) or polished
        except Exception as e:
            log(f"润色浮窗不可用, 直接上屏: {e}")
            final = polished
    if cfg["learn_enabled"]:
        # 未修改也记录 (负样本, 用于统计修正率), 离线再聚合
        triplet.record(raw, polished, final)
    return final
