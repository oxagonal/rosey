# Rosey: notes for Claude

Rosey is a three-wheeled caretaking robot for seniors living alone, built on a Jetson Orin Nano. This repo has the cloud control server, the agent that runs on the robot, and Myo armband fall detection. Read `README.md` for setup and the API.

## Layout

- `server/`: FastAPI app deployed to Render (`render.yaml`). In-memory hub that relays commands to the robot over a WebSocket the Jetson opens (`/ws/robot`). Control page at `server/static/index.html`.
- `jetson/rosey_agent/`: runs on the robot. `agent.py` (server link, dead-man stop, command handlers), `motors.py` (L293D driver, pin map from the wiring schematic), `myo_bridge.py` (forwards Myo fall alerts).
- `myo/`: Myo fall detection, **work in progress**: never run on real hardware. Detector tuned on simulated data; impact trigger checked against a real wrist dataset (`myo/tools/bits2_peaks.py`). See `myo/README.md`.
- `robot/`: the code that actually runs on the Jetson (deployed as `~/Desktop/rosey`, symlinked into this folder): agent entry point, working motor driver, camera streaming + follow mode, voice conversation. See `robot/README.md`.
- `tests/`, `myo/tests/`, `robot/tests/`: `pytest` from the repo root runs everything (`pytest.ini` sets paths).

## Commands

- Install for development: `pip install -r requirements-dev.txt`
- Test: `pytest`. Run it before every commit; it takes about 2 seconds.
- Local server: `ROBOT_TOKEN=dev-robot-token-123456 OPERATOR_TOKEN=dev-operator-token-123456 uvicorn server.main:create_app --factory --reload`
- Fake robot: `cd jetson && ROSEY_SERVER=http://localhost:8000 ROBOT_TOKEN=dev-robot-token-123456 python3 -m rosey_agent --mock`

## Rules

- **Every push to `main` deploys the server to Render.** Run `pytest` first. Use a branch for anything unfinished.
- **Safety behaviour on the robot must not be weakened**: drive commands expire (`ttl_ms`, max 2 s), motors stop on disconnect and on `stop()`, speeds are clamped. Stop commands are handled inline and must never wait behind other work. Changes here need a test.
- Code in `jetson/` and `myo/` must run on **Python 3.8+** (JetPack 5 and 6): keep `from __future__ import annotations`, no `match`, no `X | Y` types outside annotations, no `asyncio.TaskGroup` or `asyncio.timeout`.
- Hardware libraries (`Jetson.GPIO`, `bleak`, `pyomyo`) are imported lazily so tests run on any machine. Test hardware code with fakes (see `tests/test_motors.py`).
- The pin map in `motors.py` matches the wiring schematic (EN 15/33 PWM, left 29/31, right 35/37). Only change it if the wiring changes.
- Secrets (`ROBOT_TOKEN`, `OPERATOR_TOKEN`) live in Render's environment settings and `/etc/rosey/agent.env` on the Jetson. Never commit them.
- The server keeps state in memory and must run as a single instance.
- New robot commands: register a handler in the agent (`agent.register("name", fn)`); the server passes them through `/api/command` with no changes needed.

## Verified so far

- Server, agent and control page tested end to end with mock motors, including in a headless browser.
- Motor pin logic tested against a fake GPIO module.
- Myo Bluetooth client and fall service tested against a fake Bluetooth library and a fake armband.
- **Not yet verified on real hardware**: Jetson GPIO/PWM output, the Myo Bluetooth connection, fall-detection accuracy.
