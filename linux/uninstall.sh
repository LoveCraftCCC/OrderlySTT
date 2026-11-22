#!/bin/sh
# OrderlySTT Linux 卸载 (2026-11-22 修正: 真停 unit、杀对进程、清 GNOME 残留)
set -e
PREFIX="${ORDERLYSTT_PREFIX:-/data/orderlystt}"
UNIT="$HOME/.config/systemd/user/orderlystt.service"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

# 1) 先停 systemd 单元 (旧版只 pkill 了一个早已改名的进程)
if [ -f "$UNIT" ]; then
  systemctl --user disable --now orderlystt.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  systemctl --user reset-failed orderlystt.service 2>/dev/null || true
fi
# 兜底: 手工启动的残留守护 (bracket 技巧避免 pkill 自杀)
pkill -f "orderlystt[.]py daemon" 2>/dev/null || true

# 2) 清理文件与 socket
rm -f /tmp/orderlystt.sock /tmp/orderlystt-daemon.pid
rm -f "$HOME/.config/autostart/vernest.desktop" "$HOME/.config/autostart/orderlystt.desktop"
rm -rf "$PREFIX"

# 3) 清理历史 GNOME 快捷键绑定 (含无斜杠残废条目)
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
    print("  快捷键残留已移除")
K = P + ".custom-keybinding:" + KK
for key in ("name", "binding", "command"):
    subprocess.run(["gsettings", "reset", K, key], capture_output=True)
PY

echo "已卸载 (含 $PREFIX 与 systemd 单元)"
