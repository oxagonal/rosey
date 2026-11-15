#!/usr/bin/env bash
# Make all of Rosey start when the Jetson powers on, headless: no screen, no keyboard,
# nobody logging in. Run once:
#   sudo bash ~/Desktop/rosey/install_boot.sh
#   sudo bash ~/Desktop/rosey/install_boot.sh --autologin   # extra fallback, see below
#
# What starts at boot afterwards:
#   * rosey.service (system): motor pin fix (as root), then run_agent.py as henry ->
#     website link, voice conversation, camera stream, follow mode, fall watch,
#     phone alerts, Bluetooth speaker keeper. Restarts itself if it ever exits.
#   * henry's user session (linger + Wants=user@1000): PulseAudio, which the speaker
#     and mic need. Without this, audio only exists after someone logs into the desktop.
#   * The login screen's own PulseAudio is turned off, so it can't grab the Bluetooth
#     speaker before Rosey does (a common cause of "connected but silent" at boot).
#   * Wi-Fi (eduroam) is a system connection with a saved password: connects at boot.
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
USER_NAME=${SUDO_USER:-henry}
cd "$(dirname "$(readlink -f "$0")")"

loginctl enable-linger "$USER_NAME"
echo "✓ $USER_NAME's audio session (PulseAudio) now starts at boot"

cp rosey.service /etc/systemd/system/rosey.service
systemctl daemon-reload
systemctl enable rosey bluetooth >/dev/null 2>&1 || systemctl enable rosey >/dev/null
echo "✓ rosey.service enabled at boot"

# Login screen (gdm) must not start its own PulseAudio: it would take the BT speaker.
GDM_HOME=$(getent passwd gdm | cut -d: -f6)
if [ -n "$GDM_HOME" ] && [ -d "$GDM_HOME" ]; then
  mkdir -p "$GDM_HOME/.config/pulse" "$GDM_HOME/.config/systemd/user"
  printf 'autospawn = no\ndaemon-binary = /bin/true\n' > "$GDM_HOME/.config/pulse/client.conf"
  ln -sf /dev/null "$GDM_HOME/.config/systemd/user/pulseaudio.socket"
  ln -sf /dev/null "$GDM_HOME/.config/systemd/user/pulseaudio.service"
  chown -R gdm:gdm "$GDM_HOME/.config" 2>/dev/null || true
  echo "✓ login screen no longer grabs the Bluetooth speaker"
fi

# Wi-Fi: make sure saved connections reconnect by themselves.
for c in $(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}'); do
  nmcli connection modify "$c" connection.autoconnect yes 2>/dev/null || true
done
echo "✓ Wi-Fi reconnects at boot"

if [ "$1" = "--autologin" ]; then
  conf=/etc/gdm3/custom.conf
  cp -n "$conf" "$conf.rosey-backup" || true
  sed -i -E "s/^#?\s*AutomaticLoginEnable\s*=.*/AutomaticLoginEnable = true/; s/^#?\s*AutomaticLogin\s*=.*/AutomaticLogin = $USER_NAME/" "$conf"
  grep -q "^AutomaticLoginEnable" "$conf" || sed -i "/^\[daemon\]/a AutomaticLoginEnable = true\nAutomaticLogin = $USER_NAME" "$conf"
  echo "✓ desktop logs in automatically as $USER_NAME (backup: $conf.rosey-backup)"
fi

systemctl restart rosey
sleep 8
echo
echo "Status now:"
echo "  rosey service:  $(systemctl is-active rosey)"
echo "  motor pins:     $([ -e /run/rosey/pinmux.ok ] && echo fixed || echo NOT fixed)"
echo "  linger:         $(loginctl show-user "$USER_NAME" -p Linger --value)"
echo "Done. On the next power-on everything starts by itself; check the website's"
echo "Robot panel (Speaker: connected, Voice: listening) and say \"Rosey\"."
