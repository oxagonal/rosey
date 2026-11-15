"""Live camera relay: the Jetson pushes JPEG frames up, browsers get the newest one.

The Jetson can't accept connections (it sits behind a home router), so it opens
``/ws/robot/video`` and sends frames up; viewers open ``/ws/video``.

Latency is kept low end to end with acknowledgements instead of queues:

* the server acks every frame it receives, and the robot keeps at most
  ``ROBOT_IN_FLIGHT`` unacknowledged frames, so a slow uplink lowers the frame
  rate instead of building up a backlog of stale video;
* each viewer acks every frame it has drawn; the server only sends a viewer the
  *newest* frame, and only while it has fewer than ``VIEWER_IN_FLIGHT`` frames
  unacknowledged, so a slow phone skips frames rather than falling behind;
* the robot is told how many viewers there are and stops sending when there
  are none (no camera bandwidth or CPU spent on video nobody watches).

Frame message (binary, robot -> server -> viewer, relayed unchanged):
    4-byte big-endian header length | UTF-8 JSON header | JPEG bytes
    header: {"seq": 12, "w": 640, "h": 360, "t": <robot time>, "overlay": {...}}

Control messages (JSON text):
    server -> robot:  {"type": "video_config", "viewers": 2, "preset": "medium"}
                      {"type": "frame_ack", "seq": 12}
    viewer -> server: {"type": "ack"}  /  {"type": "preset", "preset": "low"|"medium"|"high"}
    server -> viewer: {"type": "video_status", "robot": true, "preset": "medium", "viewers": 2}
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Any, Dict, Optional

from fastapi import WebSocket

log = logging.getLogger("rosey.video")

PRESETS = ("low", "medium", "high")
DEFAULT_PRESET = "medium"
VIEWER_IN_FLIGHT = 2
MAX_FRAME_BYTES = 1_000_000


def frame_header(data: bytes) -> Optional[Dict[str, Any]]:
    """Parse the JSON header of a frame message; None if it's malformed."""
    if len(data) < 4:
        return None
    (n,) = struct.unpack(">I", data[:4])
    if n > 4096 or 4 + n > len(data):
        return None
    try:
        head = json.loads(data[4:4 + n].decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return head if isinstance(head, dict) else None


class Viewer:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.in_flight = 0
        self.sent_seq = -1
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()

    async def send_text(self, msg: Dict[str, Any]) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(msg, separators=(",", ":")))


class VideoRelay:
    def __init__(self) -> None:
        self.robot: Optional[WebSocket] = None
        self._robot_lock = asyncio.Lock()
        self.viewers: Dict[int, Viewer] = {}
        self.preset = DEFAULT_PRESET
        self.frame: Optional[bytes] = None
        self.seq = 0
        self.frame_at = 0.0
        self.frames_in = 0
        self.bytes_in = 0

    # -- robot side -------------------------------------------------------------
    async def attach_robot(self, ws: WebSocket) -> None:
        old, self.robot = self.robot, ws
        if old is not None:
            try:
                await old.close(code=4000, reason="replaced by a newer connection")
            except Exception:
                pass
        log.info("Robot camera connected")
        await self.push_config()
        await self.broadcast_status()

    async def detach_robot(self, ws: WebSocket) -> None:
        if self.robot is not ws:
            return
        self.robot = None
        self.frame = None
        log.info("Robot camera disconnected")
        await self.broadcast_status()

    async def robot_frame(self, data: bytes) -> None:
        head = frame_header(data)
        if head is None or len(data) > MAX_FRAME_BYTES:
            return
        await self._send_robot({"type": "frame_ack", "seq": head.get("seq")})
        self.seq += 1
        self.frame, self.frame_at = data, time.time()
        self.frames_in += 1
        self.bytes_in += len(data)
        for v in self.viewers.values():
            v.wake.set()

    async def push_config(self) -> None:
        await self._send_robot({"type": "video_config", "viewers": len(self.viewers), "preset": self.preset})

    async def _send_robot(self, msg: Dict[str, Any]) -> None:
        ws = self.robot
        if ws is None:
            return
        try:
            async with self._robot_lock:
                await ws.send_text(json.dumps(msg, separators=(",", ":")))
        except Exception:
            pass

    # -- viewer side ------------------------------------------------------------
    async def add_viewer(self, viewer: Viewer) -> None:
        self.viewers[id(viewer)] = viewer
        await self.push_config()
        await self.broadcast_status()

    async def remove_viewer(self, viewer: Viewer) -> None:
        if self.viewers.pop(id(viewer), None) is not None:
            await self.push_config()
            await self.broadcast_status()

    async def viewer_message(self, viewer: Viewer, msg: Dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ack":
            viewer.in_flight = max(0, viewer.in_flight - 1)
            viewer.wake.set()
        elif kind == "preset" and msg.get("preset") in PRESETS and msg["preset"] != self.preset:
            self.preset = msg["preset"]
            await self.push_config()
            await self.broadcast_status()

    async def viewer_sender(self, viewer: Viewer) -> None:
        """Send the newest frame whenever there is one the viewer hasn't seen and room in flight."""
        while True:
            await viewer.wake.wait()
            viewer.wake.clear()
            frame, seq = self.frame, self.seq
            if frame is None or seq == viewer.sent_seq or viewer.in_flight >= VIEWER_IN_FLIGHT:
                continue
            viewer.in_flight += 1
            viewer.sent_seq = seq
            async with viewer.lock:
                await viewer.ws.send_bytes(frame)

    def status(self) -> Dict[str, Any]:
        return {"type": "video_status", "robot": self.robot is not None, "preset": self.preset,
                "viewers": len(self.viewers)}

    async def broadcast_status(self) -> None:
        msg = self.status()
        for v in list(self.viewers.values()):
            try:
                await v.send_text(msg)
            except Exception:
                pass
