#!/bin/sh
# OrderlySTT Linux 一键安装 (用户级, 无需 sudo; 可重复执行)
# 2026-11-22 加固版: 独立 venv + 依赖锁定 + 离线自检 + 健康校验 + 崩溃防循环.
set -e
PREFIX="${ORDERLYSTT_PREFIX:-/data/orderlystt}"
SRC="$(cd "$(dirname "$0")" && pwd)"
MODEL_DIR="$(cd "$SRC/../models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17" && pwd)"
VENV="$PREFIX/venv"
UNIT="$HOME/.config/systemd/user/orderlystt.service"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

command -v uv >/dev/null 2>&1 || { echo "缺少 uv (需要 ~/.local/bin/uv 在 PATH 中)" >&2; exit 1; }

# 1) 独立运行时: Python 3.14 + 锁定依赖. 系统 Python 升级不会打断它.
mkdir -p "$PREFIX"
if [ ! -x "$VENV/bin/python" ]; then
  echo "创建 venv (Python 3.14) -> $VENV"
  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY \
    uv venv --python 3.14 "$VENV"
fi
echo "安装/校验依赖 (requirements.txt)"
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY \
  uv pip install --python "$VENV/bin/python" -r "$SRC/requirements.txt"

# 2) 部署脚本 + 配置 (重装保留已有 config, 含 deepseek_key)
cp -f "$SRC/orderlystt.py" "$PREFIX/orderlystt.py"
chmod +x "$PREFIX/orderlystt.py"
if [ ! -f "$PREFIX/config.json" ]; then
  printf '{\n  "model_dir": "%s",\n  "polish_enabled": false,\n  "deepseek_key": ""\n}\n' "$MODEL_DIR" > "$PREFIX/config.json"
fi
chmod 600 "$PREFIX/config.json"

# 3) 离线自检: 整条识别链不通就不装守护 (不需要麦克风)
echo "离线识别自检 (test_wavs):"
"$VENV/bin/python" "$PREFIX/orderlystt.py" selftest || { echo "识别自检失败, 终止安装" >&2; exit 1; }

# 可选录入 DeepSeek key (仅在交互终端; 回车=跳过, 不贴聊天)
if [ -t 0 ] && [ -z "${ORDERLYSTT_NO_KEY:-}" ]; then
  printf "DeepSeek API key (回车=跳过, 跳过则仅本地识别): "
  read -r k
  if [ -n "$k" ]; then
    "$VENV/bin/python" - "$PREFIX/config.json" "$k" <<'PY'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["deepseek_key"] = sys.argv[2]
with open(p, "w") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=1)
PY
    chmod 600 "$PREFIX/config.json"
    echo "  key 已写入 (600); 润色需用 config.json 的 polish_enabled=true 启用"
  fi
fi

# 4) GNOME 快捷键: 明确不注册. F9 由守护进程 evdev 直读; 注册会双触发.
#    顺手清理历史遗留的 vernest 绑定 (含无斜杠残废条目).
python3 - <<'PY'
import subprocess
P = "org.gnome.settings-daemon.plugins.media-keys"
KK = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vernest/"
out = subprocess.run(["gsettings", "get", P, "custom-keybindings"],
                     capture_output=True, text=True)
cur = out.stdout.strip()
new = cur
for variant in (KK, KK.rstrip("/")):
    new = new.replace("'%s', " % variant, "").replace(", '%s'" % variant, "").replace("'%s'" % variant, "")
if new != cur:
    subprocess.run(["gsettings", "set", P, "custom-keybindings", new or "@as []"], check=True)
    print("  已清理残留 GNOME 快捷键绑定 (F9 由 evdev 直读)")
else:
    print("  GNOME 快捷键: 无残留 (正确)")
K = P + ".custom-keybinding:" + KK
for key in ("name", "binding", "command"):
    subprocess.run(["gsettings", "reset", K, key], capture_output=True)
PY

# 5) systemd 用户单元: 启动限流 + 永久失败不重启 + 日志限流.
mkdir -p "$HOME/.config/systemd/user"
cat > "$UNIT" <<EOF
[Unit]
Description=OrderlySTT voice input daemon (传令兵)
After=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
ExecStart=$VENV/bin/python $PREFIX/orderlystt.py daemon
Environment=WAYLAND_DISPLAY=wayland-0
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
Restart=on-failure
RestartSec=10
RestartPreventExitStatus=75
TimeoutStartSec=60
LogRateLimitIntervalSec=30
LogRateLimitBurst=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
rm -f "$HOME/.config/autostart/vernest.desktop" "$HOME/.config/autostart/orderlystt.desktop"

# 6) 启动并做健康校验 (失败就打印 journal, 不报假成功)
systemctl --user daemon-reload
systemctl --user reset-failed orderlystt.service 2>/dev/null || true
systemctl --user enable orderlystt.service
# 显式 restart: 重装必须让新脚本生效 (enable --now 对已 active 的单元不会重启)
systemctl --user restart orderlystt.service
i=0
while [ "$i" -lt 8 ]; do
  if "$VENV/bin/python" "$PREFIX/orderlystt.py" status 2>/dev/null | grep -q ALIVE; then break; fi
  i=$((i + 1)); sleep 2
done
if ! systemctl --user is-active --quiet orderlystt.service; then
  echo "守护进程未 active, journal 尾部:" >&2
  journalctl --user -u orderlystt -n 30 --no-pager >&2
  exit 1
fi
"$VENV/bin/python" "$PREFIX/orderlystt.py" status
echo "安装完成: 按住 F9 说话, 松开上屏 | 日志: journalctl --user -u orderlystt -f"
