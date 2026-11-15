"""Follow mode must never weaken the agent's safety rules.

Run:  cd ~/Desktop/rosey && repo/.venv/bin/python -m pytest tests -q
"""
import asyncio, os, sys, time, types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.modules.setdefault("rosey_tts", types.SimpleNamespace(engine=lambda: None, speak=lambda t: None))
os.environ["ROSEY_PINMUX_MARKER"] = __file__          # an existing file = motors ready

import pytest
import run_agent
from rosey_agent.motors import MockMotors


class FakeProc:
    returncode = None
    def __init__(self):
        self.sent = []
        self.stdin = types.SimpleNamespace(write=lambda b: self.sent.append(b))


def make_agent(online=True):
    a = run_agent.RoseyAgent("http://x", "t" * 20, MockMotors())
    a._vision_proc = FakeProc()
    a._ws = object() if online else None
    return a


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_follow_drive_ignored_when_follow_off():
    a = make_agent()
    a.follow_drive({"type": "drive", "left": 0.8, "right": 0.8, "ttl_ms": 400})
    assert not a.motors.moving


def test_follow_drive_ignored_when_server_offline():
    a = make_agent(online=False)
    run(a._follow_command({"enabled": True}))
    a.follow_drive({"type": "drive", "left": 0.8, "right": 0.8, "ttl_ms": 400})
    assert not a.motors.moving


def test_follow_drive_moves_and_has_short_ttl():
    a = make_agent()
    run(a._follow_command({"enabled": True}))
    a.follow_drive({"type": "drive", "left": 5, "right": 0.6, "ttl_ms": 99999})
    assert a.motors.left == 1.0                        # clamped
    assert a._drive_deadline - time.monotonic() <= 0.61   # ttl capped at 600 ms


def test_halt_line_stops():
    a = make_agent()
    run(a._follow_command({"enabled": True}))
    a.follow_drive({"type": "drive", "left": 0.6, "right": 0.6})
    a.follow_drive({"type": "halt"})
    assert not a.motors.moving


@pytest.mark.parametrize("msg", [{"type": "stop", "reason": "operator"},
                                 {"type": "drive", "left": 0.2, "right": 0.2, "ttl_ms": 300}])
def test_manual_command_turns_follow_off(msg):
    a = make_agent()
    run(a._follow_command({"enabled": True}))
    run(a._dispatch(msg))
    assert not a.follow_enabled
    a.follow_drive({"type": "drive", "left": 0.9, "right": 0.9})
    if msg["type"] == "stop":
        assert not a.motors.moving
    else:
        assert a.motors.left == 0.2                    # manual command wins, follow ignored


def test_connection_loss_turns_follow_off_and_stops():
    a = make_agent()
    run(a._follow_command({"enabled": True}))
    a.follow_drive({"type": "drive", "left": 0.6, "right": 0.6})
    a._halt("server connection lost")
    assert not a.follow_enabled and not a.motors.moving


def test_deadman_stops_but_keeps_follow_on():
    a = make_agent()
    run(a._follow_command({"enabled": True}))
    a.follow_drive({"type": "drive", "left": 0.6, "right": 0.6})
    a._halt("dead-man timeout (no fresh drive command)")
    assert a.follow_enabled and not a.motors.moving


def test_follow_refused_without_camera_or_motor_pins(monkeypatch):
    a = make_agent()
    a._vision_proc = None
    with pytest.raises(RuntimeError):
        run(a._follow_command({"enabled": True}))
    a = make_agent()
    monkeypatch.setattr(run_agent, "PINMUX_MARKER", "/nonexistent")
    a.motors = types.SimpleNamespace(moving=False, stop=lambda: None)
    with pytest.raises(RuntimeError):
        run(a._follow_command({"enabled": True}))


def test_disable_tells_vision_process():
    a = make_agent()
    run(a._follow_command({"enabled": True, "speed": 0.5}))
    run(a._follow_command({"enabled": False}))
    assert b'"follow": false' in a._vision_proc.sent[-1]


def test_camera_fall_stops_disables_follow_and_alerts():
    a = make_agent()
    run(a._follow_command({"enabled": True}))
    a.follow_drive({"type": "drive", "left": 0.6, "right": 0.6})
    events = []
    async def send_event(name, **data):
        events.append((name, data))
    a.send_event = send_event
    spoken = []
    sys.modules["rosey_tts"].speak = spoken.append
    run(a._on_camera_fall({"description": "Person lying on the floor", "confidence": 0.9, "image": "data:x"}))
    assert not a.follow_enabled and not a.motors.moving
    assert events[0][0] == "fall_detected" and events[0][1]["source"] == "camera"
    assert spoken and "caregiver" in spoken[0]


def test_alert_events_push_to_phone(monkeypatch):
    import rosey_notify
    pushed = []
    monkeypatch.setattr(rosey_notify, "notify_event", pushed.append)
    a = make_agent(online=False)                  # offline: website can't get it, phone still must
    async def go():
        await a.send_event("fall_detected", source="camera", description="x")
        await a.send_event("telemetry_note", x=1)
        await asyncio.sleep(0.05)
    run(go())
    assert [p["event"] for p in pushed] == ["fall_detected"]


def test_notify_message_and_image(monkeypatch):
    import base64, rosey_notify
    calls = []
    monkeypatch.setattr(rosey_notify, "send", lambda *a, **k: calls.append(a) or 200)
    img = "data:image/jpeg;base64," + base64.b64encode(b"JPEG").decode()
    assert rosey_notify.notify_event({"event": "fall_detected", "source": "camera",
                                      "description": "Person on the floor.", "image": img})
    title, msg, prio, tags, image = calls[0]
    assert prio == 5 and "fall" in title and "Person on the floor" in msg and image == b"JPEG"
    assert not rosey_notify.notify_event({"event": "something_else"})
    assert rosey_notify._ascii("it’s 41°F — ok") == "it's 41 degF - ok"
