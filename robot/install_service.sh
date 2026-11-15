#!/usr/bin/env bash
# Install and start the Rosey agent as a boot service. Run: sudo bash install_service.sh
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
cd "$(dirname "$0")"
pkill -f run_agent.py || true          # a hand-started agent would fight the service
cp rosey.service /etc/systemd/system/rosey.service
systemctl daemon-reload
systemctl enable --now rosey
sleep 4
systemctl --no-pager status rosey | head -15
