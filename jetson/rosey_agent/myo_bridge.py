"""Optional: forward Myo armband fall alerts (from the myo-fall-detect service) to the server.

myo-fall-detect sends JSON over UDP: "fall" alerts (repeated, with an id) and a
"heartbeat" every second. This listens for them, forwards each new fall as a
``fall_detected`` event, and adds the armband's status to the robot telemetry.

Point the Myo service at this port (it accepts several --udp targets):
    python -m myo_fall run --udp 127.0.0.1:5006
    python -m rosey_agent --myo-udp 5006
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

log = logging.getLogger("rosey.myo")


class MyoBridge(asyncio.DatagramProtocol):
    def __init__(self, agent, stale_after_s: float = 5.0):
        self.agent = agent
        self.stale_after_s = stale_after_s
        self.last_heartbeat: Optional[Dict[str, Any]] = None
        self._last_hb_time = 0.0
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "heartbeat":
            self.last_heartbeat, self._last_hb_time = msg, time.monotonic()
        elif kind == "fall":
            fall_id = str(msg.get("id"))
            if fall_id in self._seen:
                return
            self._seen[fall_id] = time.monotonic()
            while len(self._seen) > 128:
                self._seen.popitem(last=False)
            log.warning("Myo fall alert: %s", msg.get("event"))
            asyncio.ensure_future(self.agent.send_event(
                "fall_detected", source="myo_armband", device=msg.get("device"), detail=msg.get("event")))

    def status(self) -> Dict[str, Any]:
        hb = self.last_heartbeat
        fresh = hb is not None and time.monotonic() - self._last_hb_time < self.stale_after_s
        return {
            "streaming": bool(fresh and hb.get("connected")),
            "battery": hb.get("battery") if hb else None,
            "rate_hz": hb.get("rate_hz") if fresh else 0.0,
        }


async def start_myo_bridge(agent, port: int, host: str = "127.0.0.1") -> MyoBridge:
    loop = asyncio.get_running_loop()
    _, bridge = await loop.create_datagram_endpoint(lambda: MyoBridge(agent), local_addr=(host, port))
    agent.add_telemetry("myo", bridge.status)
    log.info("Listening for Myo fall alerts on udp://%s:%d", host, port)
    return bridge
