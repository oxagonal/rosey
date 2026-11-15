# Rosey: handoff notes for the next session (updated 2026-11-15 ~03:15)

Read this first, then `PROGRESS.md` (full history) and `README.md` (file map) in this folder.

## Where things are
- Project folder: `~/Desktop/rosey`. Its code files are **symlinks** into `~/Desktop/rosey/repo/robot/`
  (GitHub: oxagonal/rosey, `gh` logged in as oxagonal, admin). New `.py` files need a symlink too
  (`ln -s repo/robot/X.py X.py`).
- Not in git (stay in `~/Desktop/rosey`): `agent.env` (secrets: ROSEY_SERVER, ROBOT_TOKEN,
  ANTHROPIC_API_KEY, ROSEY_MIC, ROSEY_TTS_VOLUME=100), `voices/` (Piper female voice
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
- `rosey_converse.py`: USB mic -> wake word "Rosey" -> Whisper -> Claude (replies: claude-sonnet-5-5; fall checks: claude-opus-5-5) -> Piper.
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

## Added later the same night (~02:00)
- Phone notifications via ntfy (`rosey_notify.py`), sent by the robot itself; topic in agent.env
  (ROSEY_NTFY_TOPIC); website "Phone alerts" section shows setup + test button. A real installable
  app (PWA + Web Push) is still a possible later upgrade.
- Speaker keeper in run_agent (auto-reconnects the BT speaker). `install_boot.sh` written.

## Headless boot (~03:15): the robot now runs with no screen, keyboard or terminal
- `install_boot.sh` (needs sudo once): linger for henry (PulseAudio at boot), installs/enables
  rosey.service, turns off the login screen's PulseAudio (so gdm can't grab the BT speaker),
  Wi-Fi autoconnect, then restarts Rosey and prints status. `--autologin` = extra fallback.
- rosey.service now `Wants=/After=` bluetooth.service and user@1000.service (henry's audio session).
- Wi-Fi is eduroam, a system connection with a saved password (connects with nobody logged in).
- The website's Robot panel shows headless health: **Speaker** (connected / not connected /
  no sound output) and **Voice** (listening / restarting / off: why), plus camera fps and motor pins.
- With no terminal, diagnose from the website: Jetson offline = power/Wi-Fi; "Motor pins not
  fixed" = pinmux failed; Speaker "not connected" = speaker off/out of range (keeper retries every
  10 s); Speaker "no sound output" = another audio session took it (rerun install with --autologin).
- The JLab Pop Party speaker must be switched ON; it doesn't power on by itself.

## Open items / next steps
1. **If not done yet: run `sudo bash ~/Desktop/rosey/install_boot.sh`** (check: `loginctl
   show-user henry -p Linger` must say yes). Then a real power-cycle test.
2. **Not tested on real hardware yet:** follow mode driving on the floor (mock motors only), obstacle
   avoidance, and camera fall detection with a real person lying down.
3. **Possible reversed turning:** old follow.py needed TURN_INVERT=True, which suggests drive(+L,-R)
   turns the robot LEFT, so the website's turn buttons may be reversed. Ask the user to check.
4. **Rotate the Anthropic API key** (it was pasted into an earlier chat).
5. After a reboot check: `systemctl is-active rosey`, `/run/rosey/pinmux.ok`, speaker connected
   (`bluetoothctl connect A0:8A:35:69:64:82`), Piper voice (no "using espeak" in the journal).
6. Camera-only obstacle avoidance can't see walls/cables/steps; suggested adding a distance sensor.

## 04:25 fixes
- Speaker volume: the old `jlab-volume-cap` user service (18% on connect, max 50%) was the reason
  Rosey was inaudible; disabled at the user's request. Speaker at 100%, voice ROSEY_TTS_VOLUME=100.
  Website Speaker status shows the volume level.
- Follow mode: full power (default 100%, turns at full power, wheels >= 70%) and backs away in short
  bursts when the person comes too close. Not yet tested on the floor.
- Website Say box: acks immediately, errors clearly if the speaker is off.
- Follow default speed 50% (user: "felt great at 50%"). ROSEY_TTS_DRIVE=5 for a louder voice.
- Volume (05:10): speaker amplifier pinned at full by the keeper; the Ubuntu JLab slider = website
  slider is the only knob. Needs ~/.config/pulse/default.pa (copy in robot/pulse-default.pa;
  restore it after an OS reinstall). Replies run on Claude Sonnet 5.5 (ROSEY_REPLY_MODEL).
