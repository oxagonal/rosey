# robot/: what actually runs on Rosey's Jetson

This folder is the code running on the robot (Jetson Orin Nano Super, JetPack 6,
Python 3.10). On the Jetson it is deployed as `~/Desktop/rosey`, whose files are
symlinks into this folder (`~/Desktop/rosey/repo` is this repo), so a `git pull`
there updates the robot. Restart with `sudo systemctl restart rosey`.

| File | What it does |
|---|---|
| `run_agent.py` | Entry point (systemd `rosey.service`). Runs `jetson/rosey_agent` with the working motor driver below, and supervises the voice and camera processes. Follow-mode safety gate lives here. |
| `motors.py`, `pinmux_fix.py` | Raw gpiochip L293D driver (software PWM), and the root-only pin fix needed after every boot. |
| `rosey_vision.py` | Webcam: streams JPEG frames to the server (`/ws/robot/video`) while someone watches; runs person detection + following when follow mode is on. |
| `rosey_follow.py` | Follow + camera-based obstacle avoidance logic (pure, unit tested). |
| `rosey_detect.py` | NanoDet-Plus object detector (onnxruntime, CPU). Models in `models/`. |
| `rosey_converse.py` | Mic → wake word "Rosey" → Whisper → Claude → Piper voice. |
| `rosey_context.py` | Live facts for the conversation: approximate city (from IP) and weather (Open-Meteo). |
| `rosey_tts.py` | Text to speech (Piper voice, espeak fallback). |
| `follow.py`, `wasd.py` | Older stand-alone tools (face-turn follow, keyboard drive). |
| `PROGRESS.md` | Running log of what was done and found on the hardware. |

Not in git (download/create on the Jetson):

- `agent.env` (secrets: `ROSEY_SERVER`, `ROBOT_TOKEN`, `ANTHROPIC_API_KEY`; see `agent.env.example`)
- `models/whisper/` (faster-whisper `base.en`, downloaded on first run)
- `voices/en_US-lessac-medium.onnx` (+ `.json`) from the Piper voices release

Optional `agent.env` settings: `ROSEY_VISION=0` (camera off), `ROSEY_CONVERSE=0`,
`ROSEY_CAMERA=/dev/videoN`, `ROSEY_LOCATION=auto|"City, ST"`, `ROSEY_WEATHER=0`,
`ROSEY_UNITS=us|metric`, `ROSEY_TTS_VOLUME=0-100`, `ROSEY_MIC=<pulse source>`.

Tests: `python3 -m pytest robot/tests` (needs numpy + OpenCV for the follower tests;
they're skipped elsewhere).

## Follow mode safety

- Follow drive commands from the camera process only reach the wheels while follow
  mode is on **and** the server is connected; each has a ≤600 ms ttl, so the normal
  dead-man watchdog stops the wheels if the camera process stalls.
- Any manual drive or STOP from the website, losing the server, or the camera
  process restarting turns follow mode off.
- It never reverses, stops when the person is close, stops when it loses them, and
  pauses when the floor view stops changing while driving (wheels pushing against
  something). Obstacle detection is camera-only: it sees objects the detector knows
  (furniture, people, bottles...) but not walls, cables or steps.
