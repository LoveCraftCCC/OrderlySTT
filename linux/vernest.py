#!/usr/bin/env python3
"""Vernest Linux 原生语音输入 (守护进程架构).

子命令:
  daemon   常驻: 预载模型, 监听 unix socket
  toggle   触发: F9 = 开始录音; 录音中再按 = 立即结束并提交
  once     一次性完整流水线 (测试用)
  status   探活 (真实 ping/pong 往返)
"""
import array
import json
import os
import select
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


def _drain_srv(srv):
    """录音开始前排空积压连接 (旧按键不排队重放)."""
    while True:
        r, _, _ = select.select([srv], [], [], 0)
        if not r:
            return
        try:
            conn, _ = srv.accept()
            conn.recv(64)
            conn.close()
        except Exception:
            return


def record(cfg, srv):
    """多路复用: 同时监听麦克风与控制 socket. 再按 F9 = 立即停止并提交."""
    sr = cfg["sample_rate"]
    proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", str(sr), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE)
    frames = bytearray()
    spoke = False
    silent_since = None
    t0 = time.monotonic()
    chunk_len = sr * 2 // 10  # 100ms
    afd = proc.stdout.fileno()
    sfd = srv.fileno()
    mic_no_data_reported = False
    while True:
        r, _, _ = select.select([afd, sfd], [], [], 0.2)
        # 录音中再按 F9: 停止并提交
        if sfd in r:
            try:
                conn, _ = srv.accept()
                msg = conn.recv(64)
                if msg == b"ping":
                    try:
                        conn.sendall(b"pong")
                    except Exception:
                        pass
                conn.close()
                if msg != b"ping":
                    break
            except Exception:
                pass
        # 看门狗: 麦克风无数据绝不永久阻塞
        if afd not in r:
            now = time.monotonic()
            if not frames and now - t0 > 3.0 and not mic_no_data_reported:
                notify("Vernest", "麦克风 3 秒无数据 (音频访问异常)", ms=4000)
                mic_no_data_reported = True
            if now - t0 > cfg["max_seconds"]:
                break
            continue
        chunk = os.read(afd, chunk_len)
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
            break
    try:
        proc.send_signal(signal.SIGINT)
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
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
                     "Authorization": "***" + cfg["deepseek_key"]})
        with op.open(req, timeout=8) as r:
            p = (json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or "").strip()
            return p or text
    except Exception:
        return text  # 铁律: 润色失败绝不阻塞


def polish(text, cfg):
    if cfg.get("deepseek_key"):
        return polish_deepseek(text, cfg)
    return polish_local(text, cfg)


def set_clipboard(text):
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
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


def pipeline(cfg, engine, srv):
    _drain_srv(srv)
    notify("Vernest", "正在听… (再按 F9 立即提交)")
    samples = record(cfg, srv)
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
    srv.listen(4)
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
    notify("Vernest", "守护进程就绪: F9 开始, 再按提交")
    while True:
        conn, _ = srv.accept()
        try:
            msg = conn.recv(64)
            if msg == b"ping":
                try:
                    conn.sendall(b"pong")
                except Exception:
                    pass
                continue
            pipeline(cfg, engine, srv)
        except Exception:
            pass  # 单次流水线异常不拖垮守护
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
        c.settimeout(3)
        c.connect(SOCK)
        c.send(b"ping")
        reply = c.recv(16)
        c.close()
        print("daemon: ALIVE" if reply == b"pong" else "daemon: NO-REPLY(busy?)")
    except OSError:
        print("daemon: DOWN")


def once():
    cfg = load_cfg()
    engine = Engine(cfg)
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    pipeline(cfg, engine, dummy)


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

