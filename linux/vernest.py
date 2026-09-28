#!/usr/bin/env python3
"""Vernest Linux 原生语音输入 — 最终形态 (push-to-talk).

按住 F9 说话 -> 松开 -> 识别 -> 润色 -> 直接粘贴到光标处.
守护进程直读键盘设备 (evdev), 绕开 GNOME 快捷键只给按下事件的平台限制.

子命令: daemon / toggle(测试: 静音自动停) / once / status
"""
import array
import glob
import json
import os
import select
import signal
import socket
import struct
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
    "hotkey_code": 67,       # KEY_F9
    "sample_rate": 16000,
    "max_seconds": 30,       # 按住上限, 到顶强制提交
    "silence_seconds": 1.2,  # 仅 toggle 模式使用
    "silence_rms": 350,
}

sys.path.insert(0, "/data/pyuser/lib/python3.12/site-packages")

EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY = 0x01
KEY_LEFTCTRL, KEY_V = 29, 47

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


def notify(title, body="", ms=2000):
    try:
        subprocess.run(["notify-send", "-t", str(ms), title, body], timeout=3)
    except Exception:
        pass


# ---------------- evdev ----------------

def open_keyboards():
    fds, denied = [], []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            denied.append(path)
    return fds, denied


def parse_events(data):
    out = []
    for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
        _, _, typ, code, value = struct.unpack_from(EVENT_FMT, data, i)
        out.append((typ, code, value))
    return out


def drain_fds(fds):
    for fd in fds:
        while True:
            r, _, _ = select.select([fd], [], [], 0)
            if not r:
                break
            try:
                if not os.read(fd, 4096):
                    break
            except OSError:
                break


# ---------------- 录音 ----------------

def _stop_proc(proc):
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


def spawn_arecord(sr):
    return subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", str(sr), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE)


def record_ptt(cfg, srv, kbd_fds):
    """按住说话: 松开 hotkey (或到 max_seconds) 提交."""
    sr = cfg["sample_rate"]
    hotkey = cfg["hotkey_code"]
    proc = spawn_arecord(sr)
    frames = bytearray()
    t0 = time.monotonic()
    afd = proc.stdout.fileno()
    sfd = srv.fileno()
    watch = [afd, sfd] + kbd_fds
    while True:
        r, _, _ = select.select(watch, [], [], 0.2)
        released = False
        for fd in r:
            if fd == afd:
                chunk = os.read(afd, sr * 2 // 10)
                if chunk:
                    frames += chunk
                continue
            if fd == sfd:
                try:
                    conn, _ = srv.accept()
                    msg = conn.recv(64)
                    if msg == b"ping":
                        try:
                            conn.sendall(b"pong")
                        except Exception:
                            pass
                    conn.close()
                except Exception:
                    pass
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                continue
            for typ, code, value in parse_events(data):
                if typ == EV_KEY and code == hotkey and value == 0:
                    released = True
        if released or time.monotonic() - t0 > cfg["max_seconds"]:
            break
    _stop_proc(proc)
    return bytes(frames)


def record_silence(cfg, srv, kbd_fds):
    """toggle 模式 (socket 触发): 静音自动停 / 到顶提交."""
    sr = cfg["sample_rate"]
    hotkey = cfg["hotkey_code"]
    proc = spawn_arecord(sr)
    frames = bytearray()
    spoke = False
    silent_since = None
    t0 = time.monotonic()
    afd = proc.stdout.fileno()
    sfd = srv.fileno()
    watch = [afd, sfd] + kbd_fds
    mic_no_data = False
    while True:
        r, _, _ = select.select(watch, [], [], 0.2)
        for fd in r:
            if fd == afd:
                chunk = os.read(afd, sr * 2 // 10)
                if not chunk:
                    continue
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
                continue
            if fd == sfd:
                try:
                    conn, _ = srv.accept()
                    msg = conn.recv(64)
                    if msg == b"ping":
                        try:
                            conn.sendall(b"pong")
                        except Exception:
                            pass
                    else:
                        _stop_proc(proc)
                        return bytes(frames)
                    conn.close()
                except Exception:
                    pass
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                continue
            for typ, code, value in parse_events(data):
                if typ == EV_KEY and code == hotkey and value == 0:
                    _stop_proc(proc)
                    return bytes(frames)
        if not frames and time.monotonic() - t0 > 3.0 and not mic_no_data:
            notify("Vernest", "麦克风 3 秒无数据 (音频访问异常)", ms=4000)
            mic_no_data = True
        now = time.monotonic()
        if (spoke and silent_since and now - silent_since > cfg["silence_seconds"]) \
                or now - t0 > cfg["max_seconds"]:
            break
    _stop_proc(proc)
    return bytes(frames)


# ---------------- 识别/润色/输出 ----------------

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
        return (getattr(res, "text", "") or "").strip()


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
        return text


def polish(text, cfg):
    if cfg.get("deepseek_key"):
        return polish_deepseek(text, cfg)
    return polish_local(text, cfg)


def set_clipboard(text):
    helper = (
        "import sys, gi; gi.require_version('Gtk','3.0'); "
        "from gi.repository import Gtk, Gdk, GLib; "
        "cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD); "
        "cb.set_text(sys.argv[1], -1); cb.store(); "
        "GLib.timeout_add(300, Gtk.main_quit); Gtk.main()"
    )
    try:
        subprocess.run([sys.executable, "-c", helper, text], timeout=4)
        return True
    except Exception:
        pass
    try:
        subprocess.run(["wl-copy", text], timeout=3, input=text.encode("utf-8"))
        return True
    except Exception:
        return False


def deliver(text):
    """最终输出: 剪贴板 + 模拟 Ctrl+V 直接上屏."""
    set_clipboard(text)
    time.sleep(0.05)
    try:
        subprocess.run(["ydotool", "key",
                        f"{KEY_LEFTCTRL}:1", f"{KEY_V}:1",
                        f"{KEY_V}:0", f"{KEY_LEFTCTRL}:0"], timeout=3)
    except Exception:
        notify("已复制 (Ctrl+V 粘贴)", text[:90])


def pipeline(cfg, engine, samples):
    if len(samples) < cfg["sample_rate"] * 2 // 5:
        notify("Vernest", "太短了, 没听清")
        return
    raw = engine.stt(samples)
    if not raw:
        notify("Vernest", "识别结果为空")
        return
    final = polish(raw, cfg)
    deliver(final)


# ---------------- daemon ----------------

def daemon():
    cfg = load_cfg()
    kbd_fds, denied = open_keyboards()
    if not kbd_fds:
        notify("Vernest", "无法读取键盘设备 (需要 input 权限)", ms=6000)
        print("daemon: no keyboard access", flush=True)
        sys.exit(1)
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
    print(f"daemon: keyboards={len(kbd_fds)} denied={len(denied)} loading model...", flush=True)
    engine = Engine(cfg)
    print("daemon: ready (push-to-talk)", flush=True)
    notify("Vernest 就绪", "按住 F9 说话, 松开上屏")

    hotkey = cfg["hotkey_code"]
    sfd = srv.fileno()
    while True:
        r, _, _ = select.select([sfd] + kbd_fds, [], [])
        for fd in r:
            if fd == sfd:
                try:
                    conn, _ = srv.accept()
                    msg = conn.recv(64)
                    if msg == b"ping":
                        try:
                            conn.sendall(b"pong")
                        except Exception:
                            pass
                        conn.close()
                        continue
                    conn.close()
                except Exception:
                    continue
                drain_fds(kbd_fds)
                notify("Vernest", "正在听… (静音自动停)")
                pipeline(cfg, engine, record_silence(cfg, srv, kbd_fds))
                drain_fds(kbd_fds)
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                continue
            for typ, code, value in parse_events(data):
                if typ == EV_KEY and code == hotkey and value == 1:
                    drain_fds(kbd_fds)
                    notify("🎙️ 正在听", "松开 F9 上屏")
                    pipeline(cfg, engine, record_ptt(cfg, srv, kbd_fds))
                    drain_fds(kbd_fds)


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
        print("daemon: ALIVE" if reply == b"pong" else "daemon: NO-REPLY")
    except OSError:
        print("daemon: DOWN")


def once():
    cfg = load_cfg()
    engine = Engine(cfg)
    sr = cfg["sample_rate"]
    proc = spawn_arecord(sr)
    notify("Vernest", "一次性录音 5 秒…")
    time.sleep(5)
    _stop_proc(proc)
    data = b""
    try:
        data = proc.stdout.read() or b""
    except Exception:
        pass
    pipeline(cfg, engine, data)


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

