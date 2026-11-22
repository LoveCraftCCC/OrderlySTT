#!/usr/bin/env python3
"""OrderlySTT (传令兵) Linux 原生语音输入 — push-to-talk.

按住 F9 说话 -> 松开 -> 识别 -> (可选)润色 -> 直接粘贴到光标处.
evdev 直读键盘 + 设备热插拔自愈, 空闲零 CPU.

运行时由 install.sh 建立的独立 venv 提供 (Python 3.14 + sherpa-onnx cp314 wheel);
本文件不再绑定任何 site-packages 绝对路径, 系统 Python 升级不会打断它.
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

PREFIX = "/data/orderlystt"
SOCK = "/tmp/orderlystt.sock"
CONFIG = os.path.join(PREFIX, "config.json")
PIDFILE = "/tmp/orderlystt-daemon.pid"
BRAND = "传令兵 OrderlySTT"
SYSTEM_PYTHON = "/usr/bin/python3"   # 仅用于 Gtk 剪贴板助手 (gi 是系统包)
EXIT_PERMANENT = 75                   # systemd RestartPreventExitStatus: 不再重启

DEFAULTS = {
    "model_dir": "/data/projects/STT-YanQi/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
    "polish_enabled": False,
    "deepseek_key": "",
    "polish_server": "http://127.0.0.1:47640/polish",
    "hotkey_code": 67,
    "sample_rate": 16000,
    "max_seconds": 30,
    "silence_seconds": 1.2,
    "silence_rms": 350,
    "rescan_seconds": 30,
    "no_speech_seconds": 8,          # 静音模式: 一直没人说话就提前收工
}

EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY = 1
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
        subprocess.run(["notify-send", "-t", str(ms), BRAND,
                        title + ((" | " + body) if body else "")], timeout=3)
    except Exception:
        pass


def parse_events(data):
    out = []
    for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
        _, _, typ, code, value = struct.unpack_from(EVENT_FMT, data, i)
        out.append((typ, code, value))
    return out


class Keyboards:
    """evdev 设备集合: 热插拔自愈 (路径差集清理 + EOF/错误即弃 + 周期重扫)."""

    def __init__(self):
        self.fds = {}   # fd -> path

    def scan(self):
        now_paths = set(glob.glob("/dev/input/event*"))
        # 节点消失 -> 主动弃 fd (不等 EOF, 避免旧 fd 变成永远可读的忙轮询源)
        for fd, path in list(self.fds.items()):
            if path not in now_paths:
                self.drop(fd)
        live = set(self.fds.values())
        denied = 0
        for path in sorted(now_paths - live):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self.fds[fd] = path
            except OSError:
                denied += 1
        return len(self.fds), denied

    def drop(self, fd):
        try:
            os.close(fd)
        except OSError:
            pass
        self.fds.pop(fd, None)

    def watch(self):
        return list(self.fds)

    def drain(self, max_rounds=64):
        """清空挂起的按键事件, 有上限: 设备持续灌事件时不至于死循环."""
        for _ in range(max_rounds):
            fds = self.watch()
            if not fds:
                break
            r, _, _ = select.select(fds, [], [], 0)
            if not r:
                break
            for fd in r:
                try:
                    if not os.read(fd, 4096):
                        self.drop(fd)
                except OSError:
                    self.drop(fd)


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


def _read_kbd(kb, fds_r, hotkey):
    """读一批键盘 fd, 返回是否出现 hotkey 松开(value=0)."""
    released = False
    for fd in fds_r:
        try:
            data = os.read(fd, 4096)
        except OSError:
            kb.drop(fd)
            continue
        if not data:
            kb.drop(fd)
            continue
        for typ, code, value in parse_events(data):
            if typ == EV_KEY and code == hotkey and value == 0:
                released = True
    return released


def _serve_socket(srv, sfd, fds_r):
    """录音期间只处理 ping, 忽略 record (防排队)."""
    if sfd not in fds_r:
        return
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


def _record_loop(cfg, srv, kb, silence_mode):
    """PTT / 静音两种录音共用的循环. arecord 退出或管道 EOF 立即收工, 绝不空转."""
    sr = cfg["sample_rate"]
    hotkey = cfg["hotkey_code"]
    try:
        proc = spawn_arecord(sr)
    except OSError as e:
        notify("录音启动失败", str(e))
        return b""
    frames = bytearray()
    spoke = False
    silent_since = None
    mic_no_data = False
    t0 = time.monotonic()
    afd = proc.stdout.fileno()
    sfd = srv.fileno()
    try:
        while True:
            r, _, _ = select.select([afd, sfd] + kb.watch(), [], [], 0.2)
            if _read_kbd(kb, [fd for fd in r if fd not in (afd, sfd)], hotkey):
                break
            _serve_socket(srv, sfd, r)
            if afd in r:
                chunk = os.read(afd, sr * 2 // 10)
                if not chunk:
                    # 管道 EOF: arecord 已退出/麦克风掉线, 立刻停 (旧版在此空转至 max_seconds)
                    break
                frames += chunk
                if silence_mode:
                    arr = array.array("h")
                    arr.frombytes(chunk[: len(chunk) // 2 * 2])
                    rms = (sum(x * x for x in arr) / max(1, len(arr))) ** 0.5
                    now = time.monotonic()
                    if rms > cfg["silence_rms"]:
                        spoke = True
                        silent_since = None
                    elif spoke and silent_since is None:
                        silent_since = now
            now = time.monotonic()
            if proc.poll() is not None:
                break
            if not frames and now - t0 > 3.0 and not mic_no_data:
                notify("麦克风 3 秒无数据 (音频访问异常)")
                mic_no_data = True
            if silence_mode and not spoke and now - t0 > cfg["no_speech_seconds"]:
                break
            if (silence_mode and spoke and silent_since
                    and now - silent_since > cfg["silence_seconds"]):
                break
            if now - t0 > cfg["max_seconds"]:
                break
    finally:
        _stop_proc(proc)
    return bytes(frames)


def record_ptt(cfg, srv, kb):
    return _record_loop(cfg, srv, kb, silence_mode=False)


def record_silence(cfg, srv, kb):
    return _record_loop(cfg, srv, kb, silence_mode=True)


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
        samples = samples[: len(samples) // 2 * 2]   # 防奇数长度
        data = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
        s = self.rec.create_stream()
        s.accept_waveform(self.sr, data)
        if hasattr(self.rec, "decode_stream"):
            ret = self.rec.decode_stream(s)
            res = ret if ret is not None and hasattr(ret, "text") else s.result
        else:
            self.rec.decode(s)
            res = s.result
        return (getattr(res, "text", "") or "").strip()


def preflight(cfg):
    """启动前检查: 失败原因明确 -> exit 75, 不再被 systemd 无限重启."""
    md = cfg["model_dir"]
    for name in ("tokens.txt", "model.int8.onnx"):
        if not os.path.isfile(os.path.join(md, name)):
            return "模型文件缺失: " + os.path.join(md, name)
    try:
        import sherpa_onnx  # noqa: F401
        import numpy  # noqa: F401
    except Exception as e:
        return "依赖不可用: %s: %s" % (type(e).__name__, e)
    return None


def _no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def polish_local(text, cfg):
    """本地润色服务 (127.0.0.1), 直连不走代理."""
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
    """云端润色: 走环境代理 (HTTPS_PROXY), 不再用 no-proxy opener."""
    if not cfg.get("deepseek_key"):
        return text
    try:
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
                     "Authorization": "Bearer" + " " + cfg["deepseek_key"]})
        with urllib.request.urlopen(req, timeout=8) as r:
            p = (json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or "").strip()
            return p or text
    except Exception:
        return text


def polish(text, cfg):
    if not cfg.get("polish_enabled"):
        return text                      # 默认关闭: 零网络调用
    if cfg.get("deepseek_key"):
        return polish_deepseek(text, cfg)
    return polish_local(text, cfg)


def set_clipboard(text):
    """优先 wl-copy (Wayland 原生, 无 300ms Gtk 主循环), 失败再试 Gtk 助手."""
    try:
        subprocess.run(["wl-copy", text], timeout=3, input=text.encode("utf-8"),
                       check=True)
        return True
    except Exception:
        pass
    helper = (
        "import sys, gi; gi.require_version('Gtk','3.0'); "
        "from gi.repository import Gtk, Gdk, GLib; "
        "cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD); "
        "cb.set_text(sys.argv[1], -1); cb.store(); "
        "GLib.timeout_add(300, Gtk.main_quit); Gtk.main()"
    )
    try:
        subprocess.run([SYSTEM_PYTHON, "-c", helper, text], timeout=4, check=True)
        return True
    except Exception:
        return False


def deliver(text):
    if not set_clipboard(text):
        notify("剪贴板不可用, 未上屏", text[:90])
        return
    time.sleep(0.05)
    try:
        subprocess.run(["ydotool", "key",
                        f"{KEY_LEFTCTRL}:1", f"{KEY_V}:1",
                        f"{KEY_V}:0", f"{KEY_LEFTCTRL}:0"],
                       timeout=3, check=True)
    except Exception as e:
        notify("注入失败 (已复制到剪贴板, 请手动 Ctrl+V)", "%s: %s" % (type(e).__name__, e))


def pipeline(cfg, engine, samples):
    if len(samples) < cfg["sample_rate"] * 2 // 5:
        notify("太短了, 没听清")
        return
    raw = engine.stt(samples)
    if not raw:
        notify("识别结果为空")
        return
    final = polish(raw, cfg)
    deliver(final)


def _already_running():
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(0.5)
        c.connect(SOCK)
        c.sendall(b"ping")
        return c.recv(16) == b"pong"
    except OSError:
        return False


def _cleanup_files():
    for p in (SOCK, PIDFILE):
        try:
            os.remove(p)
        except OSError:
            pass


def daemon():
    cfg = load_cfg()
    err = preflight(cfg)
    if err:
        print("daemon: preflight failed: " + err, flush=True)
        notify("启动失败", err)
        sys.exit(EXIT_PERMANENT)
    if _already_running():
        print("daemon: already running, refusing to steal socket", flush=True)
        sys.exit(0)

    kb = Keyboards()
    n_ok, n_denied = kb.scan()
    for _ in range(3):                    # 图形会话未就绪时有界重试, 不靠 systemd 重启
        if n_ok:
            break
        time.sleep(5)
        n_ok, n_denied = kb.scan()
    if n_ok == 0:
        print("daemon: no keyboard access after 3 tries", flush=True)
        notify("无法读取键盘设备 (需要 input 组权限)")
        sys.exit(EXIT_PERMANENT)

    _cleanup_files()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)                 # 别让本地其他用户触发录音
    srv.listen(4)
    pfd = os.open(PIDFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(pfd, "w") as f:
        f.write(str(os.getpid()))

    def bye(*_):
        _cleanup_files()
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    print(f"daemon: keyboards={n_ok} denied={n_denied} loading model...", flush=True)
    try:
        engine = Engine(cfg)
    except Exception as e:
        print("daemon: model load failed: %s: %s" % (type(e).__name__, e), flush=True)
        notify("模型加载失败", str(e))
        _cleanup_files()
        sys.exit(EXIT_PERMANENT)
    print("daemon: ready (push-to-talk)", flush=True)
    notify("就绪: 按住 F9 说话, 松开上屏")

    hotkey = cfg["hotkey_code"]
    sfd = srv.fileno()
    last_scan = time.monotonic()
    while True:
        r, _, _ = select.select([sfd] + kb.watch(), [], [], 1.0)
        now = time.monotonic()
        if now - last_scan > cfg["rescan_seconds"]:
            kb.scan()
            last_scan = now
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
                kb.drain()
                notify("正在听… (静音自动停)")
                pipeline(cfg, engine, record_silence(cfg, srv, kb))
                kb.drain()
                continue
            f9_down = False
            try:
                data = os.read(fd, 4096)
            except OSError:
                kb.drop(fd)
                continue
            if not data:
                kb.drop(fd)   # 设备已拔/重建: 弃 fd, 下轮重扫收新节点
                continue
            for typ, code, value in parse_events(data):
                if typ == EV_KEY and code == hotkey and value == 1:
                    f9_down = True
            if f9_down:
                kb.drain()
                notify("正在听 | 松开 F9 上屏")
                pipeline(cfg, engine, record_ptt(cfg, srv, kb))
                kb.drain()


def toggle():
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2)
        c.connect(SOCK)
        c.send(b"record")
        c.close()
    except OSError:
        notify("守护进程未运行")


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


def selftest():
    """离线自检: 用模型自带的 5 个样本 wav 验证整条识别链, 不需要麦克风."""
    cfg = load_cfg()
    err = preflight(cfg)
    if err:
        print("selftest: FAIL (preflight): " + err)
        return 1
    md = cfg["model_dir"]
    engine = Engine(cfg)
    wavs = sorted(glob.glob(os.path.join(md, "test_wavs", "*.wav")))
    if not wavs:
        print("selftest: FAIL (no test_wavs)")
        return 1
    ok = True
    for w in wavs:
        try:
            with wave.open(w, "rb") as f:
                sr, sw, ch = f.getframerate(), f.getsampwidth(), f.getnchannels()
                data = f.readframes(f.getnframes())
            if (sr, sw, ch) != (cfg["sample_rate"], 2, 1):
                print("%s: SKIP (sr=%s sw=%s ch=%s)" % (os.path.basename(w), sr, sw, ch))
                continue
            text = engine.stt(data)
            print("%s: %s" % (os.path.basename(w), repr(text)))
            if not text:
                ok = False
        except Exception as e:
            print("%s: ERROR %s: %s" % (os.path.basename(w), type(e).__name__, e))
            ok = False
    print("selftest: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "daemon":
        daemon()
    elif cmd == "toggle":
        toggle()
    elif cmd == "status":
        status()
    elif cmd == "selftest":
        sys.exit(selftest())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
