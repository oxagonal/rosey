# Rosey: handoff notes for the next session (written 2026-11-15 ~01:55)

Read this first, then `PROGRESS.md` (full history) and `README.md` (file map) in this folder.

## Where things are
- Project folder: `~/Desktop/rosey`. Its code files are **symlinks** into `~/Desktop/rosey/repo/robot/`
  (GitHub: oxagonal/rosey, `gh` logged in as oxagonal, admin). New `.py` files need a symlink too
  (`ln -s repo/robot/X.py X.py`).
- Not in git (stay in `~/Desktop/rosey`): `agent.env` (secrets: ROSEY_SERVER, ROBOT_TOKEN,
  ANTHROPIC_API_KEY, ROSEY_MIC, ROSEY_TTS_VOLUME=60), `voices/` (Piper female voice
  en_US-lessac-medium), `models/whisper/`, `logs/`.
- Data paths use `ROSEY_HOME` (= ~/Desktop/rosey), because Python resolves symlinked script folders.
- Website: https://rosey-control.onrender.com (Render, auto-deploys on every push to `main`).
- Boot service: `rosey.service` (systemd) runs pinmux_fix.py as root, then `run_agent.py`.
  Restart without sudo: `kill $(pgrep -f "^/usr/bin/python3 /home/henry/Desktop/rosey/run_agent.py")`
  (systemd restarts it). Logs: `journalctl -u rosey -f`.

## User preferences
- **Push every change to GitHub** (user asked: "push all current and future changes to repo").
  Test first: `cd repo && .venv/bin/python -m pytest` (83 pass) and `python3 -m pytest robot/tests -p no:cacheprovider` (25 pass, system Python has OpenCV).
  Commit as `oxagonal <oxagonal@users.noreply.github.com>`.
- Keep `PROGRESS.md` updated. Voice should stay the Piper female voice ("the woman from before").

## What runs (run_agent.py supervises two child processes)
- `rosey_converse.py`: USB mic -> wake word "Rosey" -> Whisper -> Claude (claude-opus-5-5) -> Piper.
  Each turn includes the latest camera frame + follow state, approximate city (IP -> Amherst, MA)
  and live weather (Open-Meteo) via `rosey_context.py`.
- `rosey_vision.py`: webcam (onn 1440p, /dev/v4l/by-id/...Sonix...index0, captured 1280x720 MJPEG).
  Streams to the website only while viewed (presets low/medium/high). Runs follow mode
  (`rosey_follow.py`) and fall watch (`rosey_fall.py`, detector ~2/s, Claude confirms).
  Writes `/run/user/1000/rosey-camera.jpg/.json` once a second for the conversation.
- Follow safety gate is in run_agent.py (only while follow on + server connected, ttl <= 600 ms;
  manual drive/stop, disconnect, camera restart or a fall turn it off). Tested in robot/tests.

## Done this session (2026-11-15)
- Restart after reboot: reconnected the Bluetooth speaker, pinned the USB mic (ROSEY_MIC).
- Website: live camera panel, Auto follow button, fall/help "Needs attention" banner with snapshot,
  browser notifications (only while the page is open).
- Volume 50 -> 60. Voice regression (espeak) after the repo move: fixed with ROSEY_HOME.
- Detector CPU fix (onnxruntime spin-wait off): camera process 144% -> 42% CPU.

## Open items / next steps
1. **User's pending question: notifications app.** Offered two options, awaiting their choice:
   (a) ntfy.sh via the existing ALERT_WEBHOOK_URL (quick; note the event JSON includes a base64
   image, so a small formatter may be needed), or (b) PWA + Web Push (VAPID, pywebpush); Render free
   tier has no disk, so subscriptions need a small free DB. Recommended (a) now, (b) later.
2. **Not tested on real hardware yet:** follow mode driving on the floor (mock motors only), obstacle
   avoidance, and camera fall detection with a real person lying down.
3. **Possible reversed turning:** old follow.py needed TURN_INVERT=True, which suggests drive(+L,-R)
   turns the robot LEFT, so the website's turn buttons may be reversed. Ask the user to check.
4. **Rotate the Anthropic API key** (it was pasted into an earlier chat).
5. After a reboot check: `systemctl is-active rosey`, `/run/rosey/pinmux.ok`, speaker connected
   (`bluetoothctl connect A0:8A:35:69:64:82`), Piper voice (no "using espeak" in the journal).
6. Camera-only obstacle avoidance can't see walls/cables/steps; suggested adding a distance sensor.
