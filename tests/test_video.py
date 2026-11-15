import json
import struct

import pytest
from fastapi.testclient import TestClient

from conftest import OPERATOR_TOKEN, ROBOT_TOKEN
from server.config import Settings
from server.main import create_app
from server.video import VIEWER_IN_FLIGHT, frame_header


@pytest.fixture
def client():
    app = create_app(Settings(robot_token=ROBOT_TOKEN, operator_token=OPERATOR_TOKEN))
    with TestClient(app) as c:
        yield c


def frame(seq, jpeg=b"\xff\xd8jpeg\xff\xd9"):
    head = json.dumps({"seq": seq, "w": 4, "h": 3}).encode()
    return struct.pack(">I", len(head)) + head + jpeg


def auth(sock, token):
    sock.send_text(json.dumps({"type": "auth", "token": token}))
    return json.loads(sock.receive_text())


def recv_json(sock, kind):
    while True:
        m = json.loads(sock.receive_text())
        if m.get("type") == kind:
            return m


def recv_frame(sock):
    while True:
        m = sock.receive()
        if m.get("bytes") is not None:
            return m["bytes"]


def test_frame_header_parsing():
    assert frame_header(frame(7))["seq"] == 7
    assert frame_header(b"\x00\x00\x00\x10{}") is None
    assert frame_header(b"xx") is None


def test_robot_video_needs_robot_token(client):
    with client.websocket_connect("/ws/robot/video") as r:
        assert auth(r, OPERATOR_TOKEN)["type"] == "auth_error"
    with client.websocket_connect("/ws/video") as v:
        assert auth(v, ROBOT_TOKEN)["type"] == "auth_error"


def test_viewer_count_and_preset_reach_robot(client):
    with client.websocket_connect("/ws/robot/video") as r:
        assert auth(r, ROBOT_TOKEN)["type"] == "auth_ok"
        assert recv_json(r, "video_config")["viewers"] == 0
        with client.websocket_connect("/ws/video") as v:
            assert auth(v, OPERATOR_TOKEN)["type"] == "auth_ok"
            assert recv_json(v, "video_status")["robot"] is True
            assert recv_json(r, "video_config")["viewers"] == 1
            v.send_text(json.dumps({"type": "preset", "preset": "low"}))
            assert recv_json(r, "video_config")["preset"] == "low"
            v.send_text(json.dumps({"type": "preset", "preset": "bogus"}))
        assert recv_json(r, "video_config")["viewers"] == 0


def test_frames_relayed_with_flow_control(client):
    with client.websocket_connect("/ws/robot/video") as r:
        auth(r, ROBOT_TOKEN)
        with client.websocket_connect("/ws/video") as v:
            auth(v, OPERATOR_TOKEN)
            recv_json(r, "video_config")
            recv_json(r, "video_config")
            r.send_bytes(frame(1))
            assert recv_json(r, "frame_ack")["seq"] == 1
            assert recv_frame(v) == frame(1)
            # Viewer doesn't ack: after VIEWER_IN_FLIGHT frames the server holds off,
            # then sends only the NEWEST frame once the viewer acks.
            for i in range(2, 2 + VIEWER_IN_FLIGHT + 3):
                r.send_bytes(frame(i))
                recv_json(r, "frame_ack")
            got = [recv_frame(v) for _ in range(VIEWER_IN_FLIGHT - 1)]
            assert got == [frame(2)]
            v.send_text(json.dumps({"type": "ack"}))
            assert recv_frame(v) == frame(1 + VIEWER_IN_FLIGHT + 3)


def test_malformed_frames_dropped(client):
    with client.websocket_connect("/ws/robot/video") as r:
        auth(r, ROBOT_TOKEN)
        r.send_bytes(b"garbage")
        r.send_bytes(frame(5))
        assert recv_json(r, "frame_ack")["seq"] == 5
