#!/bin/sh
# Vernest Linux 卸载
set -e
PREFIX="${VERNEST_PREFIX:-/data/vernest}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
pkill -f "vernest[.]py daemon" 2>/dev/null || true
rm -f "$HOME/.config/autostart/vernest.desktop"
python3 - <<'PY'
import subprocess
P = "org.gnome.settings-daemon.plugins.media-keys"
KK = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/vernest/"
cur = subprocess.run(["gsettings", "get", P, "custom-keybindings"],
                     capture_output=True, text=True).stdout.strip()
if KK in cur:
    new = cur.replace("'%s', " % KK, "").replace(", '%s'" % KK, "").replace("'%s'" % KK, "")
    if new == "": new = "@as []"
    subprocess.run(["gsettings", "set", P, "custom-keybindings", new], check=True)
    print("  快捷键已移除")
PY
rm -rf "$PREFIX"
echo "已卸载 (含 $PREFIX)"

