import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import OPERATOR_TOKEN, ROBOT_TOKEN
from server.config import Settings
from server.main import create_app
from server.protocol import mix

AUTH = {"Authorization": "Bearer " + OPERATOR_TOKEN}


@pytest.fixture
def client():
    app = create_app(Settings(robot_token=ROBOT_TOKEN, operator_token=OPERATOR_TOKEN))
    with TestClient(app) as c:
        yield c


def connect_robot(client, token=ROBOT_TOKEN):
    ws = client.websocket_connect("/ws/robot")
    sock = ws.__enter__()
    sock.send_text(json.dumps({"type": "auth", "token": token, "name": "test-bot", "version": "t"}))
    return ws, sock


def test_health_needs_no_auth(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Rosey" in r.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": OPERATOR_TOKEN}])
def test_api_requires_operator_token(client, headers):
    assert client.get("/api/status", headers=headers).status_code == 401
    assert client.post("/api/stop", headers=headers).status_code == 401


def test_status_offline_and_commands_503(client):
    s = client.get("/api/status", headers=AUTH).json()
    assert s["robot_online"] is False
    assert client.post("/api/drive", json={"linear": 0.5, "angular": 0}, headers=AUTH).status_code == 503
    assert client.post("/api/stop", headers=AUTH).status_code == 503


def test_robot_with_wrong_token_is_rejected(client):
    ws, sock = connect_robot(client, token="nope")
    assert json.loads(sock.receive_text())["type"] == "auth_error"
    with pytest.raises(WebSocketDisconnect):
        sock.receive_text()
    ws.__exit__(None, None, None)
    assert client.get("/api/status", headers=AUTH).json()["robot_online"] is False


def test_robot_token_cannot_be_used_as_operator(client):
    assert client.get("/api/status", headers={"Authorization": "Bearer " + ROBOT_TOKEN}).status_code == 401


def test_drive_and_stop_are_relayed(client):
    ws, robot = connect_robot(client)
    assert json.loads(robot.receive_text())["type"] == "auth_ok"
    s = client.get("/api/status", headers=AUTH).json()
    assert s["robot_online"] and s["robot"]["name"] == "test-bot"

    r = client.post("/api/drive", json={"linear": 0.5, "angular": 0.25}, headers=AUTH)
    assert r.status_code == 202
    msg = json.loads(robot.receive_text())
    assert msg == {"type": "drive", "left": 0.25, "right": 0.75, "ttl_ms": 500}

    client.post("/api/drive", json={"left": -1, "right": 1, "ttl_ms": 99999}, headers=AUTH)
    msg = json.loads(robot.receive_text())
    assert (msg["left"], msg["right"], msg["ttl_ms"]) == (-1, 1, 2000)  # ttl capped

    assert client.post("/api/stop", headers=AUTH).status_code == 200
    assert json.loads(robot.receive_text())["type"] == "stop"
    ws.__exit__(None, None, None)


@pytest.mark.parametrize("body", [
    {"linear": 2},                                 # out of range
    {"linear": 0.5, "left": 0.5},                  # mixed styles
    {},                                            # nothing
    {"left": 0.1, "right": 0.1, "turbo": True},    # unknown field
])
def test_drive_validation(client, body):
    assert client.post("/api/drive", json=body, headers=AUTH).status_code == 422


def test_reserved_command_types_rejected(client):
    assert client.post("/api/command", json={"type": "drive", "left": 1}, headers=AUTH).status_code == 422


def test_telemetry_and_events_flow_to_status_and_controllers(client):
    ws, robot = connect_robot(client)
    robot.receive_text()
    with client.websocket_connect("/ws/control") as ctl:
        ctl.send_text(json.dumps({"type": "auth", "token": OPERATOR_TOKEN}))
        assert json.loads(ctl.receive_text())["type"] == "auth_ok"
        assert json.loads(ctl.receive_text())["robot_online"] is True

        robot.send_text(json.dumps({"type": "telemetry", "motors": {"left": 0, "right": 0}, "cpu_temp_c": 48.5}))
        assert json.loads(ctl.receive_text())["cpu_temp_c"] == 48.5
        robot.send_text(json.dumps({"type": "event", "event": "fall_detected", "source": "camera"}))
        assert json.loads(ctl.receive_text())["event"] == "fall_detected"

    assert client.get("/api/status", headers=AUTH).json()["telemetry"]["cpu_temp_c"] == 48.5
    events = client.get("/api/events", headers=AUTH).json()["events"]
    assert events[-1]["event"] == "fall_detected" and "received_at" in events[-1]
    ws.__exit__(None, None, None)


def test_control_socket_drive_and_stop_on_disconnect(client):
    ws, robot = connect_robot(client)
    robot.receive_text()
    with client.websocket_connect("/ws/control") as ctl:
        ctl.send_text(json.dumps({"type": "auth", "token": OPERATOR_TOKEN}))
        ctl.receive_text(), ctl.receive_text()
        ctl.send_text(json.dumps({"type": "drive", "linear": 0.4, "angular": 0, "ttl_ms": 300}))
        assert json.loads(robot.receive_text()) == {"type": "drive", "left": 0.4, "right": 0.4, "ttl_ms": 300}
        ctl.send_text(json.dumps({"type": "drive", "linear": 5}))
        assert json.loads(ctl.receive_text())["type"] == "error"
    # The controller left while driving: the server tells the robot to stop.
    msg = json.loads(robot.receive_text())
    assert msg["type"] == "stop" and msg["reason"] == "controller disconnected"
    ws.__exit__(None, None, None)


def test_control_socket_rejects_bad_token(client):
    with client.websocket_connect("/ws/control") as ctl:
        ctl.send_text(json.dumps({"type": "auth", "token": ROBOT_TOKEN}))
        assert json.loads(ctl.receive_text())["type"] == "auth_error"


def test_new_robot_connection_replaces_old(client):
    ws1, r1 = connect_robot(client)
    r1.receive_text()
    ws2, r2 = connect_robot(client)
    r2.receive_text()
    with pytest.raises(WebSocketDisconnect):
        r1.receive_text()
    client.post("/api/stop", headers=AUTH)
    assert json.loads(r2.receive_text())["type"] == "stop"
    ws2.__exit__(None, None, None)
    ws1.__exit__(None, None, None)


def test_mix():
    assert mix(1, 0) == (1, 1)
    assert mix(0, 1) == (-1, 1)  # turn left in place: left wheel back, right forward
    left, right = mix(1, 1)
    assert (left, right) == (0, 1)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ROBOT_TOKEN", "x" * 20)
    monkeypatch.setenv("OPERATOR_TOKEN", "x" * 20)
    with pytest.raises(RuntimeError, match="different"):
        Settings.from_env()
    monkeypatch.delenv("OPERATOR_TOKEN")
    with pytest.raises(RuntimeError, match="OPERATOR_TOKEN"):
        Settings.from_env()


def test_control_socket_command_result_keeps_type_and_ref(client):
    ws, robot = connect_robot(client)
    assert json.loads(robot.receive_text())["type"] == "auth_ok"
    with client.websocket_connect("/ws/control") as ctl:
        ctl.send_text(json.dumps({"type": "auth", "token": OPERATOR_TOKEN}))
        ctl.receive_text()  # auth_ok
        ctl.send_text(json.dumps({"type": "command", "ref": "phone-info-1", "command": {"type": "notify_info"}}))
        cmd = json.loads(robot.receive_text())
        while cmd.get("type") != "notify_info":
            cmd = json.loads(robot.receive_text())
        robot.send_text(json.dumps({"type": "ack", "id": cmd["id"], "ok": True, "result": {"topic": "t"}}))
        while True:
            m = json.loads(ctl.receive_text())
            if m["type"] not in ("status", "robot", "telemetry"):
                break
        assert m == {"type": "command_result", "ref": "phone-info-1", "ok": True, "result": {"topic": "t"}}
    ws.__exit__(None, None, None)
