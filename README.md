# Rosey control server

Cloud backend for controlling **Rosey**, the Jetson Orin Nano caretaking robot, from a phone, a browser or other services. It deploys to [Render](https://render.com) from this repo.

```
 phone / browser / scripts                 Render (this server)                  Jetson Orin Nano (at home)
 ─────────────────────────   HTTPS + WS   ─────────────────────   WebSocket   ──────────────────────────────
  control page  (/)          ───────────▶   REST API  /api/*      ◀─────────── rosey_agent (jetson/)
  REST API      (/api/*)                    live hub  /ws/control  opened by      ├─ L293D motor driver (GPIO/PWM)
  live socket   (/ws/control)◀───────────   robot link /ws/robot   the Jetson     ├─ dead-man safety stop
                                            events, telemetry                     └─ say / custom commands / events
```

**The Jetson connects out to the server.** A robot on home Wi-Fi sits behind a router, so a cloud server can't reach it directly. The agent keeps a WebSocket open to Render, and commands come back down that socket. No port forwarding or static IP is needed.

## What's in here

| Path | What it is |
|---|---|
| `server/` | FastAPI app that Render runs: REST API, WebSocket hub, auth and the control page |
| `server/static/index.html` | Phone-friendly control page: hold-to-drive pad, speed, STOP, speak, telemetry, events |
| `jetson/rosey_agent/` | Agent that runs on the robot: server link, motor driver, safety and command handlers |
| `jetson/rosey_agent/motor_test.py` | Wiring check that spins each wheel in turn |
| `jetson/rosey-agent.service` | systemd unit so the agent starts on boot |
| `render.yaml` | Render Blueprint: one-click deploy that also generates the secrets |
| `tests/` | Server, motor-logic and end-to-end tests (real server + real agent + mock motors) |
| `myo/` | Myo armband fall detection (work in progress, see [myo/README.md](myo/README.md)) |
| `CLAUDE.md` | Project notes that Claude Code reads automatically when someone works on this repo with it |

---

## 1. Deploy the server to Render

1. Push this repo to GitHub.
2. In Render, go to **New → Blueprint**, pick the repo and click **Apply**. `render.yaml` creates a free Python web service called `rosey-control`.
3. When it's live, open the service's **Environment** page. Render generated two secrets:
   - `OPERATOR_TOKEN` is your login for the control page and API. Treat it like a password.
   - `ROBOT_TOKEN` goes on the Jetson (step 2).
4. Open `https://<your-service>.onrender.com`. The page asks for the operator token and shows "Robot offline" until the Jetson connects.

Notes:
- **Free plan:** the service sleeps after 15 minutes without inbound traffic and takes about a minute to wake. While the robot is connected, its telemetry every second keeps the service awake. The free plan's 750 instance-hours a month covers one service running all month.
- **Region:** `render.yaml` uses `virginia`. Change it before the first deploy to the region nearest the robot, because driving lag depends on it. It can't be changed later.
- Interactive API docs are at `/docs`. Calls still need the token.

## 2. Set up the Jetson

```bash
git clone <this repo> ~/rosey-control
```

```bash
cd ~/rosey-control/jetson && python3 -m pip install --user -r requirements.txt
```

**Enable PWM on pins 15 and 33.** These are the Orin Nano's hardware-PWM pins, and your schematic wires the L293D enable pins to them. They only output PWM after the pinmux is configured:

```bash
sudo /opt/nvidia/jetson-io/jetson-io.py
```

In the menu, choose *Configure Jetson 40pin Header → Configure header pins manually*, enable the PWM functions on pins 15 and 33, then save and reboot. Menu wording varies a little between JetPack versions. Without this the agent still runs, but only at full speed or off, and it logs a warning.

**GPIO permissions.** JetPack normally sets these up for the default user. If you get permission errors:

```bash
sudo groupadd -f -r gpio && sudo usermod -a -G gpio $USER
```

Then log out and back in.

**Check the wiring with the wheels off the ground:**

```bash
python3 -m rosey_agent.motor_test
```

It prints what each step should do: left forward, left back, right forward, right back, both forward, spin. If a wheel turns the wrong way, add `--invert-left` or `--invert-right`, or set `ROSEY_INVERT_LEFT=1` in the env file below. TT motors on opposite sides of a chassis are mirror images, so one usually needs inverting. The ramp at the end shows the lowest speed that actually moves the wheels; tune `--min-duty` to match.

**Run it:**

```bash
export ROSEY_SERVER=https://<your-service>.onrender.com ROBOT_TOKEN=<from Render>
```

```bash
python3 -m rosey_agent
```

The control page should switch to "Robot online".

**Start on boot:**

```bash
sudo mkdir -p /etc/rosey && sudo cp agent.env.example /etc/rosey/agent.env && sudo chmod 600 /etc/rosey/agent.env
```

```bash
sudo nano /etc/rosey/agent.env
```

```bash
sed "s|YOUR_USER|$USER|g; s|REPO_DIR|$(pwd)|g" rosey-agent.service | sudo tee /etc/systemd/system/rosey-agent.service
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now rosey-agent
```

```bash
journalctl -u rosey-agent -f
```

## 3. Using the API

All `/api/*` calls need `Authorization: Bearer $OPERATOR_TOKEN`.

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/healthz` | | No auth (used by Render) |
| GET | `/api/status` | | Robot online?, robot info, latest telemetry |
| POST | `/api/drive` | `{"linear": 0.5, "angular": 0}` or `{"left": 0.5, "right": 0.5}`, optional `"ttl_ms"` | Values −1…1, `angular` > 0 turns left. Returns 202 |
| POST | `/api/stop` | | Stop now |
| POST | `/api/say` | `{"text": "Time for your medication"}` | Speaks on the robot (espeak-ng by default) |
| POST | `/api/command` | `{"type": "mode", "mode": "follow", ...}` | Any command the agent registers; returns the robot's answer |
| GET | `/api/events?limit=50` | | Recent robot events (e.g. `fall_detected`) |

```bash
curl -X POST https://<your-service>.onrender.com/api/drive -H "Authorization: Bearer $OPERATOR_TOKEN" -H "Content-Type: application/json" -d '{"linear": 0.4, "angular": 0, "ttl_ms": 1000}'
```

If the robot isn't connected you get `503`. If it doesn't answer a command within 5 s you get `504`. If it rejects one you get `422`.

**Live control** (`/ws/control`) is what the web page uses and suits apps that drive continuously. Send `{"type":"auth","token":"<OPERATOR_TOKEN>"}` first, then `{"type":"drive",...}` about 10 times a second while driving, plus `{"type":"stop"}` or `{"type":"command","ref":1,"command":{...}}`. The server pushes `status`, `robot`, `telemetry`, `event` and `command_result` messages. If a controller disconnects while driving, the robot is told to stop.

## 4. Safety design

These rules matter for a robot that drives around a senior's home:

- **Dead-man switch on the robot.** Every drive command expires after `ttl_ms` (default 500 ms, max 2 s). If no fresh command arrives the motors stop, so lag, a dropped connection or a frozen phone means "stop", never "keep going". This runs on the Jetson, so it works even when the internet doesn't.
- **Losing the server stops the motors** at once. The agent keeps reconnecting with backoff.
- **Separate secrets.** The robot token can't be used to control the robot and vice versa. Tokens are compared in constant time and sent inside the encrypted connection, not in URLs.
- **Speed limits.** `ROSEY_MAX_SPEED` caps top speed on the robot. The page also has a speed slider, default 50%.
- Not built yet, and needed before unsupervised use: obstacle stop from the IR sensor and camera. Hook it into the agent so it overrides remote drive commands (see below).

## 5. Wiring notes (from the schematic)

The code matches the schematic (BOARD pin numbers):

| L293D | Jetson | Purpose |
|---|---|---|
| 1 EN1,2 | 15 (PWM) | Left speed |
| 2 1A / 7 2A | 29 / 31 | Left direction |
| 9 EN3,4 | 33 (PWM) | Right speed |
| 10 3A / 15 4A | 35 / 37 | Right direction |
| 16 VCC1 | 2 (5 V) | Logic supply |
| 8 VCC2 | Buck 6 V | Motor supply |

Things worth checking on the bench:

- **One shared ground.** Battery −, buck GND, the L293D GND pins and Jetson GND (pin 6) must all be connected, or the logic signals have no reference.
- **Pull-down resistors on the enables.** Add 10 kΩ from L293D pins 1 and 9 to GND. Jetson header pins can float while it boots, which can twitch the motors before the agent takes over.
- **Decoupling.** Put a 100 nF capacitor right at VCC1 and at VCC2, plus about 100 µF on VCC2. Motor noise otherwise gets onto the logic supply.
- **Logic levels are fine.** The L293D reads ≥ 2.3 V as high, so the Jetson's 3.3 V GPIO drives it directly.
- **Expect some voltage loss.** The L293D drops roughly 2–3 V, so the TT motors see about 3–4 V from the 6 V rail. It's rated 600 mA per channel. If the chip runs hot or the robot stalls on carpet, a TB6612FNG driver is the usual upgrade.

## 6. Extending

Add a command on the robot: it becomes callable through `/api/command` and the live socket with no server changes.

```python
# in jetson/rosey_agent/__main__.py, after the agent is created
async def set_mode(msg):                 # POST /api/command {"type": "mode", "mode": "follow"}
    follow_controller.mode = msg["mode"]
    return {"mode": msg["mode"]}         # returned to the caller as "result"
agent.register("mode", set_mode)
```

Report events from any module (camera fall detection, the medication schedule and so on). Controllers see them live and they're listed at `/api/events`:

```python
await agent.send_event("fall_detected", source="camera", detail={"confidence": 0.92})
```

Add live values to telemetry with `agent.add_telemetry("battery_v", read_battery)`.

- **Alerts to family.** Set `ALERT_WEBHOOK_URL` in Render, and every robot event is POSTed there as JSON. It can be any endpoint that accepts JSON: an [ntfy.sh](https://ntfy.sh) topic for phone push, a Zapier/Make webhook that texts emergency contacts, or your own service.
- **Myo armband fall alerts** from [`myo/`](myo/README.md): run the agent with `--myo-udp 5006` and `python3 -m myo_fall run --udp 127.0.0.1:5006`. Falls are forwarded as `fall_detected` events, and the armband's battery and streaming status appear in telemetry.
- **Text-to-speech:** `say_handler` in `agent.py` uses espeak-ng (`sudo apt install espeak-ng`). Swap in your own voice stack.

## 7. Development

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
```

```bash
pytest
```

Run the server locally:

```bash
export ROBOT_TOKEN=dev-robot-token-123456 OPERATOR_TOKEN=dev-operator-token-123456
```

```bash
uvicorn server.main:create_app --factory --reload
```

In another terminal, run a robot with mock motors (it logs instead of using GPIO):

```bash
cd jetson && ROSEY_SERVER=http://localhost:8000 ROBOT_TOKEN=dev-robot-token-123456 python3 -m rosey_agent --mock
```

Then open http://localhost:8000.

## Limitations

- State (telemetry, recent events) lives in memory, so it resets on each deploy or restart, and the server must run as a **single instance**. Events that matter, such as falls, should also go out through the webhook.
- One robot per server. A second robot connection replaces the first.
- The token is a single shared secret, which is fine for a team prototype. For family members, add per-user accounts before giving out access.
