#!/usr/bin/env bash
# Start Rosey's link to Render: fix the pins (needed after every boot), then run the agent.
set -e
cd "$(dirname "$0")"
[ -f agent.env ] || { echo "Missing agent.env (copy agent.env.example and add ROBOT_TOKEN)"; exit 1; }
set -a; . ./agent.env; set +a
sudo python3 pinmux_fix.py >/dev/null
exec python3 run_agent.py "$@"
