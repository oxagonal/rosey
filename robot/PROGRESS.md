# Rosey: Project Progress

## 2026-11-14
- Created the project folder at ~/Desktop/rosey.
- Wired per schematic: Jetson Orin Nano + L293D, 2 TT motors (left = A, right = B).
  Pins (BOARD): EN_A 15, A1 29, A2 31, EN_B 33, B1 35, B2 37.
- Added `motors.py` (Car class: forward/backward/left/right/stop/brake).
  Stock Jetson.GPIO 2.1.7 can't detect the "Super" dev kit, so the script sets
  JETSON_MODEL_NAME=JETSON_ORIN_NANO. Runs as user `henry` (gpio group), no sudo needed.
- Ran the test sequence (1 s each: forward, backward, spin left, spin right). No software errors.
  Not yet confirmed: whether the wheels actually turned and which way they spin.
- Enable pins are plain on/off (full speed). For speed control, pins 15/33 can be set to
  hardware PWM later with jetson-io.
- Ran forward for 1 s on request. No errors.
- The 1 s forward run did NOT move the car. Cause: the Jetson rebooted at 23:39, and on
  JetPack 6 the header pins come back tristated (disconnected) after every boot.
- Found the earlier session's working code in ~/motors and copied it here:
  `motors.py` (raw gpiochip driver, software PWM, SWAP_SIDES=True), `pinmux_fix.py`,
  `wasd.py`, `follow.py`, `models/`. My Jetson.GPIO version is kept as
  `motors_jetsongpio_unused.py` (it skips the pinmux fix, so don't use it).
- **After every reboot:** `sudo python3 pinmux_fix.py`, then e.g. `python3 motors.py forward 1.0 1`.
- Ran pinmux_fix + forward 1 s at 23:45.
- Drove forward 1 s again on request (pinmux re-applied first).
- Drove forward 5 s on request (pinmux re-applied first).

## Render connection (2026-11-14)
- Backend repo github.com/oxagonal/rosey cloned to `repo/`. It has a FastAPI server
  (deployed at https://rosey-control.onrender.com, /healthz returned OK) and a Jetson agent
  (`repo/jetson/rosey_agent`) that connects out to the server over WebSocket.
- The agent's own motor driver needs Jetson.GPIO (can't detect this board) and skips the
  pinmux fix. So I added:
  - `run_agent.py`: runs the repo's agent with our working `motors.py` (side swap, software PWM).
  - `start_rosey.sh`: loads `agent.env`, runs `pinmux_fix.py` with sudo, starts the agent.
  - `agent.env.example`: copy to `agent.env` and add ROBOT_TOKEN from the Render dashboard.
- Installed `websockets` 16.1.1 (pip --user).
- Waiting on: ROBOT_TOKEN, then a live test from the control page.
- `repo/` is linked to github.com/oxagonal/rosey (origin) and updated to 4540609
  ("Add Myo fall detection (WIP) and CLAUDE.md"). Pulling works; pushing needs GitHub
  credentials on the Jetson (none set up yet: no SSH key, no token, no git name/email).
  Note from CLAUDE.md: every push to `main` redeploys Render, so run `pytest` first and use a branch.

## 2026-11-15: Jetson connected to Render
- GitHub CLI installed, logged in as oxagonal (`gh auth setup-git`), pushes over HTTPS.
- `agent.env` (chmod 600, not in git) holds ROSEY_SERVER + ROBOT_TOKEN.
- Agent running (`run_agent.py`): server shows robot_online=true, telemetry flowing.
- End-to-end test: POST /api/drive linear=1.0 ttl 1000 ms -> 202, motor pins went high
  (EN 15/33 + forward 29/35), then the dead-man timer stopped the motors. Works.
- Not done yet: the agent doesn't start at boot (it was started by hand from this session);
  the Jetson fixes aren't pushed to GitHub. Repo tests can't run on the Jetson as-is:
  requirements.txt pins fastapi==0.163.0, which needs Python 3.11+ (Jetson has 3.10).

## 2026-11-15 (later): website driving + start at boot
- Jetson had rebooted (agent not running). Restarted the agent by hand; it connected to Render.
- Added `rosey.service` + `install_service.sh`: systemd runs `pinmux_fix.py` as root, then
  `run_agent.py` as henry, at every boot, restarting if it crashes.
  Installed 00:23: enabled + active, pins fixed, connected to Render. Logs: `journalctl -u rosey -f`.
- `pinmux_fix.py` now leaves `/run/rosey/pinmux.ok` (cleared on reboot). `run_agent.py` reports
  `motors_ready` in telemetry from that file (override path with ROSEY_PINMUX_MARKER for tests).
- Control page (commit 2c7bcb5, merged to main and deployed to Render 00:24): amber
  "Jetson connected, but the motors are not enabled" when motors_ready is false; offline hint
  now says `sudo systemctl start rosey`. Agent reconnected on its own ~2 s after the redeploy.
- Tested locally with headless Chromium + mock motors: green / amber / offline states all render;
  holding Up sent +0.50/+0.50, the turn button sent +0.40/-0.40, releasing stopped instantly.
- Repo venv (`repo/.venv`) has fastapi 0.162.5 (0.163 needs Py 3.11): `.venv/bin/python -m pytest` -> 60 passed.
- 00:26 website test: drive commands reached the Jetson (log showed them) but the wheels didn't
  move. Cause: the buck converter (motor power) wasn't connected. Hardware fix, no code change.
  If the website drives but the wheels are dead, check the buck/motor power first.
- Confirmed by user after connecting the buck: the car drives from the website
  (https://rosey-control.onrender.com) through the Jetson. End-to-end works on real hardware.

## 2026-11-15: "Speak through Rosey" on the Bluetooth speaker
- Website "Say" failed: the agent's say handler needs `espeak-ng`, which isn't installed.
  Also a systemd service can't find the desktop's PulseAudio (where the BT speaker
  A0:8A:35:69:64:82 is the default sink) because it has no XDG_RUNTIME_DIR.
- Fix in `run_agent.py` (no sudo needed): sets XDG_RUNTIME_DIR/PULSE_SERVER to /run/user/1000,
  and registers its own `say` that uses espeak-ng if installed, else `spd-say -w`
  (speech-dispatcher, preinstalled, same espeak-ng voice). Tested directly: speech played.
- Restarted the agent at 00:33 by stopping it (systemd restarts it). Note: speech needs a
  logged-in desktop session, since that's where PulseAudio + the Bluetooth link live.
- Speech was being played but was too quiet: the Bluetooth sink was at 18% (-45 dB), and the
  espeak voice is much quieter than music. Set the JLab Pop Party sink to 100%
  (`pactl set-sink-volume bluez_sink.A0_8A_35_69_64_82.a2dp_sink 100%`).
- New `rosey_tts.py`: libespeak-ng via ctypes (no install), espeak volume 200, boosted to 95% peak,
  0.35 s lead-in silence (BT wake-up), played with paplay. `run_agent.py`'s say now uses it
  in a thread. Agent restarted 00:38. CLI test: `python3 rosey_tts.py "hello"`.
- Still too quiet at 100%: added soft-clip compression to rosey_tts (ROSEY_TTS_DRIVE, default 3):
  speech RMS -13.9 -> -8.3 dBFS (~+6 dB) at the same peak. Agent restarted.
- Speaker was "barely audible" though it used to be loud. Diagnosis: on connect, BlueZ logged
  "AVRCP: failed to init uinput" (kernel has no CONFIG_INPUT_UINPUT) and the transport had no
  Volume property, so the speaker's own hardware volume was stuck and unreachable. Reconnecting
  (`bluetoothctl disconnect/connect A0:8A:35:69:64:82`) brought it back loud. Now the speaker
  pins its hardware volume at 64/127 and snaps the PulseAudio sink back to 50% on any change,
  so the system volume slider does nothing.
  => If it's ever quiet again: reconnect the speaker first.
- Speech loudness is set in software: `rosey_tts.py` plays with `paplay --volume`,
  ROSEY_TTS_VOLUME (0-100, default 50 as the user asked). Agent restarted 00:43.

## 2026-11-15: natural voice (Piper)
- User installed piper-tts 1.8.0 (pip --user) + voice `voices/en_US-lessac-medium` (female, US).
- `rosey_tts.py` uses Piper when installed and the model exists (ROSEY_VOICE / ROSEY_VOICES_DIR),
  else falls back to espeak. Piper output is only normalised (no compression). Volume still 50%.
  Speed on the Orin Nano CPU: ~2.5 s model load, ~4x faster than real time.
- run_agent.py preloads the voice at startup so the first Say isn't delayed. Agent restarted.

## 2026-11-15: USB microphone
- New mic on USB-A: "USB PnP Sound Device" (ALSA card 3, TI PCM2902 chip, mono).
  PulseAudio source `alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono`,
  set as default source. (The webcam also has a mic; not used.)
- Gain was 40% (too quiet). Sweep with Rosey speaking near it: 100% clips, best speech-to-hiss at
  65-70%. Set to 70%. Recording works (`parecord -d <source> --channels=1 --rate=16000 x.wav`).
- Not yet decided what the mic is for (listen from website / voice commands / talk to Claude).

## 2026-11-15: talking with Claude
- `rosey_converse.py`: USB mic (parec, 16 kHz) -> energy VAD (adapts to room noise) ->
  faster-whisper `base.en` int8 on CPU (`models/whisper`, ~2 s per phrase) -> Claude
  (`claude-opus-5-5`, effort low, streamed, server-side fallbacks "default") -> Piper, spoken
  sentence by sentence. Wake word "Rosey" (also Rosie/Rosy...), then 10 s to reply without it.
  Mic is ignored while Rosey talks (flag file /run/user/1000/rosey-speaking from rosey_tts).
  Remembers the last 20 exchanges; forgets after 30 min of quiet.
- Safety: phrases like "help", "I fell", "chest pain" emit a `help` line -> run_agent sends a
  `help_requested` event (shows as an alert on the website, and goes to ALERT_WEBHOOK_URL if set).
- Privacy: speech without the wake word is never logged, only its length.
- run_agent.py supervises it as a child process (restarts with backoff; exit code 2 = config
  problem, no restart). Disable with ROSEY_CONVERSE=0 in agent.env.
- ANTHROPIC_API_KEY is in agent.env (600). Key works: first words back in ~0.9 s.
  The user pasted the key into the chat, so it should be rotated in the Anthropic console.
- Test mode without Claude: `python3 rosey_converse.py --echo` (repeats what it heard).
- Bug found + fixed: parec pipe reads return partial frames; read until each frame is whole.
- Faster responses (2026-11-15 ~01:10):
  - Instant two-note chime as soon as "Rosey" is recognised (plays while Claude thinks).
  - End-of-speech wait 0.9 -> 0.7 s; max utterance 10 s; "nearest voice" rule: frames 15 dB
    below the current talker's level count as silence (background voices can't hold it open).
  - Whisper: 4 threads (faster than 6), no second VAD pass, no timestamps: ~1.5-2 s per phrase.
    Whisper always encodes a 30 s window, so short phrases still cost ~1.5 s; tiny.en is ~0.7 s
    faster but less accurate (kept base.en).
  - Bluetooth wake-up pause skipped when the speaker played something in the last 4 s.
  - Clock moved to a mid-conversation system note: she stopped mentioning the time unprompted.
- Bug fixed: killing the voice process mid-sentence left the "speaking" flag set, so Rosey
  ignored the mic for up to 2 min. The flag now holds the PID; a dead writer is ignored.
- Measured with a test phrase played from the speaker while people talked nearby: the
  utterance still ran to the 10 s cap (the chatter was as loud as the test phrase). After that:
  speech-to-text ~2 s, first spoken sentence ~4-5 s after speech ended (chime ~2 s).

## 2026-11-15 ~01:22: restart after reboot
- The service had started on its own (pins fixed, connected to Render), but live speaking was down:
  the JLab speaker wasn't connected, and PulseAudio's default mic had gone back to the Jetson's
  built-in input, so rosey_converse kept logging "microphone stream ended".
- Fix: `bluetoothctl connect A0:8A:35:69:64:82`; set the USB mic as the default source (70%) and the
  speaker as the default sink (100%). Pinned the mic in agent.env (`ROSEY_MIC=alsa_input.usb-C-Media...`)
  so a changed default can't break it again. Restarted the agent: connected, listening, no mic errors.
- After a reboot, check: speaker connected? (`bluetoothctl info A0:8A:35:69:64:82`)

## 2026-11-15 ~01:45: live camera, auto follow, weather, repo
- Webcam (onn, Sonix, MJPEG up to 2592x1944 @ 30 fps; autofocus on) captured at 1280x720.
  `rosey_vision.py` streams to the server only while someone watches. Presets: low 320x180 @ 8 fps
  (~60-75 KB/s), medium 640x360 @ 15 fps (~320-400 KB/s), high = camera's own 720p JPEG @ 15 fps
  (~1.2 MB/s, no CPU). Half-size decode via libjpeg scaling; ack-based flow control on both legs.
  Local latency capture -> viewer: 6-23 ms.
- Follow mode (`rosey_follow.py`): NanoDet person detection ~15/s with 3 threads, locks onto the closest
  person and keeps them by clothing colour; turn-in-place pulses when off-centre, forward with steering
  when centred, stops when close/lost. Obstacles: close detected objects in the path block forward
  driving, side ones steer it away; "stuck" if the floor view freezes while driving. TURN_INVERT=True
  (from the follow.py live test). Tested with mock motors + headless Chromium only, NOT on the floor yet.
- Possible issue to check: follow.py needed TURN_INVERT, which suggests drive(+L,-R) turns the robot
  LEFT. If so, the website's turn buttons are reversed too.
- Website (commit 19e5b09, deployed): camera panel, Auto follow button, camera fps.
- Rosey's conversation now gets her approximate city (IP lookup -> Amherst, MA) and live weather
  (Open-Meteo) in the system note (`rosey_context.py`).
- Speech volume 50 -> 60 (`ROSEY_TTS_VOLUME=60` in agent.env).
- All robot code moved into the GitHub repo under `robot/`; ~/Desktop/rosey files are symlinks to it.

## 2026-11-15 ~01:50: Rosey sees, camera fall detection, voice fix
- Conversation: each turn now carries the camera's latest 640x360 frame (written once a second to
  /run/user/1000/rosey-camera.jpg by rosey_vision) + follow state. Not kept in history. No added latency
  (first sentence ~1 s with or without the image). ROSEY_VISION_CONTEXT=0 turns it off.
- Fall detection (`rosey_fall.py`): detector runs ~2/s even when not following; a wide, low person box
  in most checks over 3 s -> Claude confirms on the frame (~4.5 s) -> agent stops, sends
  `fall_detected` (source camera, description, 320x180 snapshot) and Rosey says she alerted the
  caregiver. Re-alerts at most every 2 min; a "no" suppresses re-checks for 30 s. If Claude can't be
  reached, it alerts anyway, marked "not double-checked". NOT yet tested with a real person lying down.
- Website: red "Needs attention" banner with snapshot for falls/help requests, browser notifications
  while the page is open.
- Bug: after moving code into the repo, the voice switched to espeak. Python resolves the script's
  symlinked folder, so rosey_tts looked for voices in repo/robot. Fixed with ROSEY_HOME
  (~/Desktop/rosey) for data outside git. New .py files need a symlink in ~/Desktop/rosey.

## 2026-11-15 ~02:00: start at boot + phone notifications
- Speaker keeper in run_agent: reconnects the JLab speaker within ~20 s if it drops and keeps it the
  default output (tested: disconnected it, came back by itself).
- `install_boot.sh` (needs sudo, user must run): `loginctl enable-linger henry` so PulseAudio (speaker
  + mic) starts at boot without a desktop login; re-enables rosey.service; `--autologin` optional.
  NOT yet run / not yet tested with a real reboot.
- Phone notifications (`rosey_notify.py`, ntfy.sh): falls + help requests pushed by the robot itself
  (works even if the website is down), urgent priority, camera snapshot attached, tap opens the site.
  Topic is in agent.env (ROSEY_NTFY_TOPIC). Website "Phone alerts" section: setup steps + test button.
  Test notifications (text + photo) accepted by ntfy (HTTP 200).
- Server bug fixed: command results over /ws/control had their type overwritten by the robot's "ack",
  so the page never saw any command result ("Say" never logged "done"). Regression test added.

## 2026-11-15 ~03:15: fully headless boot
- Goal: next power-on with no screen/keyboard/terminal; everything must start by itself.
- rosey.service pulls in bluetooth.service and user@1000.service (henry's PulseAudio) at boot.
- install_boot.sh also disables the login screen's PulseAudio (it can grab the BT speaker),
  sets Wi-Fi autoconnect, restarts Rosey and prints a status check.
- Website Robot panel: new Speaker and Voice status, so problems are visible without a terminal.
- Verified after an agent restart: server connected, conversation listening, speaker keeper
  reconnected the speaker by itself. 86 + 26 tests pass. A real power-cycle is still untested.

## 2026-11-15 ~04:15: fixes after the first headless test
- "Rosey didn't speak": the Bluetooth speaker wasn't connected from boot (03:56) until 04:10
  (probably switched off); speech played into nothing. Keeper now retries every 10 s (was 30 s).
- Follow mode had less power than manual 100%: it defaulted to 70%, turned at ~49% in 0.05-0.18 s
  pulses and allowed wheels down to 45%. Now: default 100% (slider 70-100%), turns always at full
  power in 0.12-0.28 s bursts, wheels never below 70%, forward speed stays near max until close.
- Website "Say" box: robot now acks right away and speaks in the background (synth + playback took
  longer than the server's 5 s timeout), and it fails with a clear error if the speaker is off or
  has no sound output, instead of reporting success silently. Result shows in the website log.
- Still silent after that: the speaker's own volume was at 18%. The JLab speaker uses Bluetooth
  absolute volume, so PulseAudio's sink volume IS the speaker's +/- level and the speaker ignores
  requests to raise it (tried: it snaps back). With the voice at 60% on top, Rosey was inaudible.
  Now: ROSEY_TTS_VOLUME=100 (agent.env), the keeper logs the speaker level and the website shows
  "connected, volume N%" (with "press + on the speaker" below 40%).
- Real cause of the silence: a user service `jlab-volume-cap` (~/.local/bin/jlab-volume-cap,
  added 2026-11-14 18:54, not by the Rosey project) sets the JLab speaker to 18% on every
  connect and caps it at 50%. User chose to turn it off: `systemctl --user disable --now
  jlab-volume-cap` (script file kept; re-enable to bring the cap back). Speaker now at 100%.
  (AVRCP absolute volume was a false lead; reverted.)
- Follow mode now backs up: person box wider than 58% of the frame -> reverse in 0.3 s bursts
  at 0.75 until below 48%; at most 6 bursts per 10 s since the camera can't see behind.
- Floor test: follow mode "felt great at 50%" -> default follow speed 50% (slider 40-100%).
  Turns stay at full power and wheels >= 70%, so 50% mainly limits forward speed.
- Louder voice: everything was already at max (speaker 100%, ROSEY_TTS_VOLUME=100), so
  ROSEY_TTS_DRIVE 3 -> 5 (more compression, slightly harsher) in agent.env.
- "Keep more distance": stop at 26% of frame width (was 34%), resume below 20% (was 26%),
  back up above 44% (was 58%) until below 35% (was 48%).
- 04:44 alert said "couldn't double-check: ValueError": Claude's fall check returned no JSON.
  It thinks before answering (120-200 output tokens on a normal frame) and max_tokens was 300, so
  a hard frame can run out before the verdict. Now max_tokens=500 (user choice) and failures are logged with
  the stop reason and text. (The alert was still sent, marked unconfirmed, as designed.)
- Speaker loudness root cause (btmon trace): the JLab supports AVRCP absolute volume and reports
  its own amplifier at 23/127 = 18%, remembered from the old jlab-volume-cap script. Setting
  MediaTransport1.Volume as henry didn't change it; waiting on a root trace of SetAbsoluteVolume.
