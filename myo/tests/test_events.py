import json
import socket
import time

from jetson_receiver import FallAlertReceiver
from myo_fall.events import UdpPublisher, parse_target


def recv_all(sock, timeout=0.5):
    msgs, end = [], time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msgs.append(json.loads(sock.recvfrom(65535)[0]))
        except socket.timeout:
            break
    return msgs


def test_parse_target():
    assert parse_target("192.168.1.20:6000") == ("192.168.1.20", 6000)
    assert parse_target("jetson.local") == ("jetson.local", 5005)


def test_fall_alert_is_repeated_and_deduplicated_by_receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    pub = UdpPublisher([sock.getsockname()], alert_repeats=3)
    try:
        pub.fall({"confidence": 0.8}, device="Myo")  # no event loop: repeats go out at once
        msgs = recv_all(sock)
    finally:
        pub.close()
        sock.close()
    assert len(msgs) == 3 and len({m["id"] for m in msgs}) == 1

    falls = []
    rx = FallAlertReceiver(on_fall=falls.append)
    results = [rx.handle(m) for m in msgs]
    assert results == ["fall", None, None]
    assert falls[0]["event"] == {"confidence": 0.8}


def test_receiver_tracks_heartbeats_and_goes_offline():
    events = []
    rx = FallAlertReceiver(stale_after_s=5, now=0.0,
                           on_offline=lambda reason: events.append(("offline", reason)),
                           on_online=lambda hb: events.append(("online", hb["battery"])))
    rx.check(now=4.0)
    assert events == []  # still within the grace period after start
    rx.check(now=6.0)
    assert events[-1] == ("offline", "no messages from the fall detector service")

    rx.handle({"type": "heartbeat", "connected": True, "battery": 80}, now=7.0)
    assert events[-1] == ("online", 80) and rx.online
    # Service still running but the armband dropped out
    for t in (8.0, 9.0, 10.0, 11.0, 11.9):  # < 5 s since the last streaming heartbeat
        rx.handle({"type": "heartbeat", "connected": False, "state": "searching"}, now=t)
    assert rx.online is True
    rx.handle({"type": "heartbeat", "connected": False, "state": "searching"}, now=12.5)
    assert events[-1] == ("offline", "armband not streaming (detector state: searching)")
    assert len([e for e in events if e[0] == "offline"]) == 2
