"""Real server (uvicorn) + real Jetson agent (mock motors) talking over a real WebSocket."""

import asyncio
import json
import socket
import time

import httpx
import uvicorn
import websockets

from conftest import OPERATOR_TOKEN, ROBOT_TOKEN
from rosey_agent.agent import RobotAgent, to_ws_url
from rosey_agent.motors import MockMotors
from server.config import Settings
from server.main import create_app

AUTH = {"Authorization": "Bearer " + OPERATOR_TOKEN}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(cond, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not met within %.1fs" % timeout)


async def scenario():
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    app = create_app(Settings(robot_token=ROBOT_TOKEN, operator_token=OPERATOR_TOKEN))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.ensure_future(server.serve())
    await wait_for(lambda: server.started)

    motors = MockMotors()
    agent = RobotAgent(base, ROBOT_TOKEN, motors, name="e2e-bot", telemetry_interval_s=0.2)
    agent.register("echo", lambda msg: {"echo": msg.get("value")})
    agent_task = asyncio.ensure_future(agent.run())

    async with httpx.AsyncClient(base_url=base, headers=AUTH, trust_env=False) as http:
        await wait_for(lambda: app.state.hub.online)
        status = (await http.get("/api/status")).json()
        assert status["robot"]["name"] == "e2e-bot"
        assert "echo" in status["robot"]["capabilities"]

        # Drive: reaches the motors, then the dead-man switch stops them.
        r = await http.post("/api/drive", json={"linear": 0.6, "angular": 0.0, "ttl_ms": 300})
        assert r.status_code == 202
        await wait_for(lambda: (motors.left, motors.right) == (0.6, 0.6))
        t0 = time.monotonic()
        await wait_for(lambda: not motors.moving, timeout=2)
        assert 0.15 < time.monotonic() - t0 < 0.8

        # Explicit stop
        await http.post("/api/drive", json={"left": -0.5, "right": 0.5, "ttl_ms": 2000})
        await wait_for(lambda: motors.moving)
        await http.post("/api/stop")
        await wait_for(lambda: not motors.moving, timeout=0.5)

        # Commands with acknowledgements
        r = await http.post("/api/command", json={"type": "echo", "value": 42})
        assert r.status_code == 200 and r.json() == {"ok": True, "result": {"echo": 42}}
        r = await http.post("/api/command", json={"type": "warp_drive"})
        assert r.status_code == 422 and "unknown command" in r.json()["detail"]

        # Telemetry arrives and reflects the stopped motors
        hub = app.state.hub
        await wait_for(lambda: hub.telemetry is not None and hub.telemetry["motors"] == {"left": 0.0, "right": 0.0})
        assert "uptime_s" in hub.telemetry

        # Live controller sees robot events (e.g. a fall) as they happen
        async with websockets.connect("ws://127.0.0.1:%d/ws/control" % port) as ctl:
            await ctl.send(json.dumps({"type": "auth", "token": OPERATOR_TOKEN}))
            assert json.loads(await ctl.recv())["type"] == "auth_ok"
            assert await agent.send_event("fall_detected", source="camera", detail={"confidence": 0.9})
            while True:
                msg = json.loads(await asyncio.wait_for(ctl.recv(), 3))
                if msg["type"] == "event":
                    break
            assert msg["event"] == "fall_detected" and msg["detail"] == {"confidence": 0.9}

            # Driving from the control socket, then dropping it: the robot stops.
            await ctl.send(json.dumps({"type": "drive", "linear": 0.3, "angular": 0, "ttl_ms": 2000}))
            await wait_for(lambda: motors.moving)
        await wait_for(lambda: not motors.moving, timeout=1)

        events = (await http.get("/api/events")).json()["events"]
        assert events[-1]["event"] == "fall_detected"

        # Server goes away while driving: the robot stops at once and keeps retrying.
        await http.post("/api/drive", json={"linear": 0.5, "angular": 0, "ttl_ms": 2000})
        await wait_for(lambda: motors.moving)
    server.should_exit = True
    await asyncio.wait_for(server_task, 15)
    await wait_for(lambda: not motors.moving, timeout=1)

    agent.stop()
    await asyncio.wait_for(agent_task, 5)


def test_end_to_end():
    asyncio.run(scenario())


def test_ws_url():
    assert to_ws_url("https://rosey.onrender.com/") == "wss://rosey.onrender.com/ws/robot"
    assert to_ws_url("http://localhost:8000") == "ws://localhost:8000/ws/robot"
    assert to_ws_url("rosey.onrender.com") == "wss://rosey.onrender.com/ws/robot"
    assert to_ws_url("wss://x.example/ws/robot") == "wss://x.example/ws/robot"
