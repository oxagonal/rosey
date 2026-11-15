"""Send events to the Jetson as JSON over UDP.

UDP keeps this dependency-free and works the same whether the receiver is on
the same Jetson (127.0.0.1) or another machine on the LAN. Because UDP can drop
packets, fall alerts are sent several times with the same ``id``; receivers
de-duplicate on it. A heartbeat goes out every second so the receiver can raise
its own alarm if the sensor goes quiet (dead battery, out of range, crash):
for a safety system, silence must never look like "no fall".

Message format (one JSON object per datagram):

    {"type": "fall", "id": "...", "ts": 1731600000.12, "source": "myo-fall@jetson",
     "device": "Myo", "event": {"confidence": 0.8, "peak_g": 4.9, "cues": [...], ...}}
    {"type": "heartbeat", "ts": ..., "source": ..., "connected": true, "device": "Myo",
     "battery": 81, "rate_hz": 49.9, "state": "idle"}
    {"type": "status", "ts": ..., "source": ..., "status": "connected" | "disconnected" | "searching", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_PORT = 5005


def parse_target(spec: str) -> Tuple[str, int]:
    """'192.168.1.20:5005' -> ('192.168.1.20', 5005); a bare host gets the default port."""
    host, _, port = spec.rpartition(":") if ":" in spec else (spec, "", "")
    return (host or "127.0.0.1", int(port) if port else DEFAULT_PORT)


class UdpPublisher:
    def __init__(self, targets: Iterable[Tuple[str, int]], source: Optional[str] = None,
                 alert_repeats: int = 5, alert_spacing_s: float = 0.2):
        self.targets: List[Tuple[str, int]] = list(targets)
        self.source = source or "myo-fall@%s" % socket.gethostname()
        self.alert_repeats = alert_repeats
        self.alert_spacing_s = alert_spacing_s
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def _sendto(self, payload: bytes) -> None:
        for target in self.targets:
            try:
                self._sock.sendto(payload, target)
            except OSError as exc:  # e.g. network down: keep detecting, keep trying
                log.warning("UDP send to %s:%d failed: %s", target[0], target[1], exc)

    def send(self, msg_type: str, repeats: int = 1, **fields) -> dict:
        msg = {"v": PROTOCOL_VERSION, "type": msg_type, "ts": round(time.time(), 3), "source": self.source}
        msg.update(fields)
        payload = json.dumps(msg, separators=(",", ":")).encode()
        self._sendto(payload)
        if repeats > 1:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            for k in range(1, repeats):
                if loop is not None:
                    loop.call_later(k * self.alert_spacing_s, self._sendto, payload)
                else:
                    self._sendto(payload)
        return msg

    def fall(self, event: dict, device: str) -> dict:
        return self.send("fall", repeats=self.alert_repeats, id=uuid.uuid4().hex, device=device, event=event)

    def heartbeat(self, **fields) -> dict:
        return self.send("heartbeat", **fields)

    def status(self, status: str, **fields) -> dict:
        return self.send("status", repeats=2, status=status, **fields)

    def close(self) -> None:
        self._sock.close()
