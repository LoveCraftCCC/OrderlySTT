#!/bin/sh
# Vernest Linux 一键安装 (用户级, 无需 sudo; 可重复执行)
set -e
PREFIX="${VERNEST_PREFIX:-/data/vernest}"
SRC="$(cd "$(dirname "$0")" && pwd)"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

mkdir -p "$PREFIX"
cp -f "$SRC/vernest.py" "$PREFIX/vernest.py"
chmod +x "$PREFIX/vernest.py"

# config 首次生成 (重装保留)
if [ ! -f "$PREFIX/config.json" ]; then
  printf '{\n  "model_dir": "%s/../models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",\n  "deepseek_key": ""\n}\n' "$SRC" > "$PREFIX/config.json"
  chmod 600 "$PREFIX/config.json"
fi

# 可选录入 DeepSeek key
if [ -t 0 ] && [ -z "${VERNEST_NO_KEY:-}" ]; then
  printf "DeepSeek API key (回车=跳过, 跳过则仅本地识别): "
  read -r k
  if [ -n "$k" ]; then
    python3 - "$PREFIX/config.json" "$k" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg["deepseek_key"] = sys.argv[2]
json.dump(cfg, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PY
    chmod 600 "$PREFIX/config.json"
    echo "  key 已写入 (600)"
  fi
fi

# XDG 开机自启
mkdir -p "$HOME/.config/autostart"
printf '[Desktop Entry]\nType=Application\nName=Vernest Voice Input\nExec=%s/vernest.py daemon\nX-GNOME-Autostart-enabled=true\n' "$PREFIX" > "$HOME/.config/autostart/vernest.desktop"

# GNOME 快捷键 F9 (python 处理 GVariant 列表, 不破坏既有绑定)
python3 - <<'PY'
import subprocess
P = "org.gnome.settings-daemon.plugins.media-keys"
KK = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vernest/"  # 末尾斜杠必须有
cur = subprocess.run(["gsettings", "get", P, "custom-keybindings"],
                     capture_output=True, text=True).stdout.strip()
if KK not in cur:
    new = "['%s']" % KK if cur in ("@as []", "[]") else cur.replace("[", "['%s', " % KK, 1)
    subprocess.run(["gsettings", "set", P, "custom-keybindings", new], check=True)
K = P + ".custom-keybinding:" + KK
subprocess.run(["gsettings", "set", K, "name", "Vernest 语音输入"], check=True)
subprocess.run(["gsettings", "set", K, "binding", "F9"], check=True)
subprocess.run(["gsettings", "set", K, "command", "/data/vernest/vernest.py toggle"], check=True)
print("  快捷键 F9 已绑定")
PY

# 启动守护进程 (若未运行)
"$PREFIX/vernest.py" status 2>/dev/null | grep -q ALIVE || \
  (setsid nohup "$PREFIX/vernest.py" daemon > "$PREFIX/daemon.log" 2>&1 &)
sleep 1
echo "安装完成: F9 = 语音输入 | 重登自启 | 日志: $PREFIX/daemon.log"

