#!/usr/bin/env bash
# Make all of Rosey start when the Jetson powers on, with nobody logging in.
#   sudo bash ~/Desktop/rosey/install_boot.sh               # recommended
#   sudo bash ~/Desktop/rosey/install_boot.sh --autologin   # also log the desktop in automatically
#
# What starts at boot afterwards:
#   * rosey.service (system): motor pin fix, then run_agent.py -> server link, voice
#     conversation, camera stream, follow mode, fall watch, speaker keeper.
#   * henry's user session (linger): PulseAudio, which the speaker and mic need.
#     Without this, audio only exists after someone logs into the desktop.
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
USER_NAME=${SUDO_USER:-henry}
cd "$(dirname "$(readlink -f "$0")")"

loginctl enable-linger "$USER_NAME"
echo "✓ $USER_NAME's audio session (PulseAudio) now starts at boot"

cp rosey.service /etc/systemd/system/rosey.service
systemctl daemon-reload
systemctl enable rosey >/dev/null
echo "✓ rosey.service enabled at boot"

if [ "$1" = "--autologin" ]; then
  conf=/etc/gdm3/custom.conf
  cp -n "$conf" "$conf.rosey-backup" || true
  sed -i -E "s/^#?\s*AutomaticLoginEnable\s*=.*/AutomaticLoginEnable = true/; s/^#?\s*AutomaticLogin\s*=.*/AutomaticLogin = $USER_NAME/" "$conf"
  grep -q "^AutomaticLoginEnable" "$conf" || sed -i "/^\[daemon\]/a AutomaticLoginEnable = true\nAutomaticLogin = $USER_NAME" "$conf"
  echo "✓ desktop logs in automatically as $USER_NAME (backup: $conf.rosey-backup)"
fi
echo "Done. Test with: sudo reboot   (then check the website and say \"Rosey\")"
