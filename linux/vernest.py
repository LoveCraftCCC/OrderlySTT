#!/usr/bin/env python3
"""Vernest Linux 原生语音输入 (守护进程架构).

子命令:
  daemon   常驻: 预载模型, 监听 unix socket, 收到命令即录音->识别->润色->剪贴板
  toggle   触发: 连 socket 发录音命令 (绑定 GNOME 快捷键)
  once     一次性完整流水线 (测试用)
  status   探活
"""
import array
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import wave

PREFIX = "/data/vernest"
SOCK = "/tmp/vernest.sock"
CONFIG = os.path.join(PREFIX, "config.json")
PIDFILE = "/tmp/vernest-daemon.pid"

DEFAULTS = {
    "model_dir": "/data/projects/STT-YanQi/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
    "deepseek_key": "",
    "polish_server": "http://127.0.0.1:47640/polish",
    "sample_rate": 16000,
    "max_seconds": 25,
    "silence_seconds": 1.2,
    "silence_rms": 350,
}

sys.path.insert(0, "/data/pyuser/lib/python3.12/site-packages")

SYSTEM_PROMPT = ("你是语音输入的润色助手。只做: 标点整理、口语词清理(嗯/呃/那个)、"
                 "明显错别字纠正。禁止: 增删内容、改写句式、翻译、回答或评论。"
                 "只输出润色后的句子本身。")


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def notify(title, body="", ms=2500):
    try:
        subprocess.run(["notify-send", "-t", str(ms), title, body], timeout=3)
    except Exception:
        pass


def record(cfg):
    sr = cfg["sample_rate"]
    proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", str(sr), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE)
    frames = bytearray()
    spoke = False
    silent_since = None
    t0 = time.monotonic()
    chunk_len = sr * 2 // 10  # 100ms
    while True:
        chunk = proc.stdout.read(chunk_len)
        if not chunk:
            break
        frames += chunk
        arr = array.array("h")
        arr.frombytes(chunk[: len(chunk) // 2 * 2])
        rms = (sum(x * x for x in arr) / max(1, len(arr))) ** 0.5
        now = time.monotonic()
        if rms > cfg["silence_rms"]:
            spoke = True
            silent_since = None
        elif spoke and silent_since is None:
            silent_since = now
        stop = False
        if spoke and silent_since and now - silent_since > cfg["silence_seconds"]:
            stop = True
        elif now - t0 > cfg["max_seconds"]:
            stop = True
        if stop:
            proc.send_signal(signal.SIGINT)
            try:
                rest = proc.stdout.read()
                if rest:
                    frames += rest
            except Exception:
                pass
            break
    proc.wait(timeout=5)
    return bytes(frames)


class Engine:
    def __init__(self, cfg):
        import sherpa_onnx
        md = cfg["model_dir"]
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            tokens=os.path.join(md, "tokens.txt"),
            model=os.path.join(md, "model.int8.onnx"),
            use_itn=True, language="auto")
        self.sr = cfg["sample_rate"]

    def stt(self, samples):
        import numpy as np
        data = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
        s = self.rec.create_stream()
        s.accept_waveform(self.sr, data)
        ret = None
        if hasattr(self.rec, "decode_stream"):
            ret = self.rec.decode_stream(s)
        else:
            self.rec.decode(s)
        res = ret if ret is not None and hasattr(ret, "text") else s.result
        text = getattr(res, "text", "")
        return (text or "").strip()


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def polish_local(text, cfg):
    try:
        op = _no_proxy_opener()
        body = json.dumps({"text": text, "budget_ms": 1500}).encode("utf-8")
        req = urllib.request.Request(cfg["polish_server"], data=body,
                                     headers={"Content-Type": "application/json"})
        with op.open(req, timeout=2.5) as r:
            p = (json.loads(r.read().decode("utf-8")).get("polished") or "").strip()
            return p or text
    except Exception:
        return text


def polish_deepseek(text, cfg):
    if not cfg.get("deepseek_key"):
        return text
    try:
        op = _no_proxy_opener()
        body = json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": text}],
            "temperature": 0.1, "max_tokens": max(64, len(text) * 2),
            "thinking": {"type": "disabled"},
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cfg["deepseek_key"]})
        with op.open(req, timeout=8) as r:
            p = (json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or "").strip()
            return p or text
    except Exception:
        return text


def polish(text, cfg):
    if cfg.get("deepseek_key"):
        return polish_deepseek(text, cfg)
    return polish_local(text, cfg)


def set_clipboard(text):
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(text, -1)
        cb.store()
        return
    except Exception:
        pass
    try:
        subprocess.run(["wl-copy", text], timeout=3)
    except Exception:
        pass


def pipeline(cfg, engine):
    notify("Vernest", "正在听…")
    samples = record(cfg)
    if len(samples) < cfg["sample_rate"] * 2 // 5:
        notify("Vernest", "太短了, 没听清")
        return
    raw = engine.stt(samples)
    if not raw:
        notify("Vernest", "识别结果为空")
        return
    final = polish(raw, cfg)
    set_clipboard(final)
    notify("已复制, Ctrl+V 粘贴", final[:90])


def daemon():
    cfg = load_cfg()
    try:
        os.remove(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(1)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))

    def bye(*_):
        try:
            os.remove(SOCK)
            os.remove(PIDFILE)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    print("daemon: loading model...", flush=True)
    engine = Engine(cfg)
    print("daemon: ready", flush=True)
    notify("Vernest", "守护进程就绪, 按快捷键语音输入")
    while True:
        conn, _ = srv.accept()
        try:
            conn.recv(64)
            pipeline(cfg, engine)
        finally:
            conn.close()


def toggle():
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2)
        c.connect(SOCK)
        c.send(b"record")
        c.close()
    except OSError:
        notify("Vernest", "守护进程未运行")


def status():
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2)
        c.connect(SOCK)
        c.send(b"ping")
        c.close()
        print("daemon: ALIVE")
    except OSError:
        print("daemon: DOWN")


def once():
    cfg = load_cfg()
    engine = Engine(cfg)
    pipeline(cfg, engine)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    if cmd == "daemon":
        daemon()
    elif cmd == "toggle":
        toggle()
    elif cmd == "status":
        status()
    elif cmd == "once":
        once()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

