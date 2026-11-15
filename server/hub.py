"""In-memory relay between controllers and the one robot.

The Jetson sits behind a home router, so the cloud server can't connect to
it. Instead the Jetson keeps a WebSocket open to us (/ws/robot) and the hub
pushes commands down that socket. State lives in memory, so run a single
instance (Render's default).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import urllib.request
import uuid
from collections import deque
from typing import Any, Deque, Dict, Optional, Set

from fastapi import WebSocket

from .config import Settings

log = logging.getLogger("rosey.hub")


class RobotOffline(Exception):
    pass


class CommandTimeout(Exception):
    pass


class Connection:
    """A WebSocket with serialized sends (several tasks may send at once)."""

    _ids = itertools.count(1)

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = next(self._ids)
        self._lock = asyncio.Lock()

    async def send(self, msg: Dict[str, Any]) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(msg, separators=(",", ":")))


class Hub:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.robot: Optional[Connection] = None
        self.robot_info: Dict[str, Any] = {}
        self.robot_connected_at: Optional[float] = None
        self.robot_last_seen: Optional[float] = None
        self.telemetry: Optional[Dict[str, Any]] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=200)
        self.clients: Set[Connection] = set()
        self._pending: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self._background: Set["asyncio.Task[Any]"] = set()

    # -- state -------------------------------------------------------------
    @property
    def online(self) -> bool:
        return self.robot is not None

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "robot_online": self.online,
            "robot": self.robot_info if self.online else None,
            "connected_for_s": round(now - self.robot_connected_at, 1) if self.online and self.robot_connected_at else None,
            "last_seen_s_ago": round(now - self.robot_last_seen, 1) if self.robot_last_seen else None,
            "telemetry": self.telemetry,
            "controllers": len(self.clients),
        }

    # -- robot connection ----------------------------------------------------
    async def attach_robot(self, conn: Connection, info: Dict[str, Any]) -> None:
        old, self.robot = self.robot, conn
        self.robot_info = info
        self.robot_connected_at = self.robot_last_seen = time.time()
        self.telemetry = None
        log.info("Robot connected: %s", info)
        if old is not None:
            log.warning("A new robot connection replaced the previous one")
            self._fail_pending(RobotOffline("robot reconnected"))
            try:
                await old.ws.close(code=4000, reason="replaced by a newer connection")
            except Exception:
                pass
        await self.broadcast({"type": "robot", "online": True, "robot": info})

    async def detach_robot(self, conn: Connection) -> None:
        if self.robot is not conn:
            return  # an old, already replaced connection
        self.robot = None
        self._fail_pending(RobotOffline("robot disconnected"))
        log.warning("Robot disconnected")
        await self.broadcast({"type": "robot", "online": False})

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def handle_robot_message(self, msg: Dict[str, Any]) -> None:
        self.robot_last_seen = time.time()
        kind = msg.get("type")
        if kind == "ack":
            fut = self._pending.pop(str(msg.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif kind == "telemetry":
            self.telemetry = {k: v for k, v in msg.items() if k != "type"}
            self.telemetry["received_at"] = round(self.robot_last_seen, 3)
            await self.broadcast(msg)
        elif kind == "event":
            event = dict(msg)
            event["received_at"] = round(self.robot_last_seen, 3)
            self.events.append(event)
            log.warning("Robot event: %s", event)
            await self.broadcast(event)
            if self.settings.alert_webhook_url:
                self._spawn(self._post_webhook(event))
        else:
            log.debug("Ignoring robot message type %r", kind)

    # -- sending to the robot ------------------------------------------------
    async def send_to_robot(self, msg: Dict[str, Any]) -> None:
        robot = self.robot
        if robot is None:
            raise RobotOffline("robot offline")
        try:
            await robot.send(msg)
        except Exception as exc:
            raise RobotOffline("send failed: %s" % exc) from exc

    async def drive(self, left: float, right: float, ttl_ms: Optional[int] = None) -> Dict[str, Any]:
        ttl = min(ttl_ms or self.settings.drive_ttl_default_ms, self.settings.drive_ttl_max_ms)
        msg = {"type": "drive", "left": round(left, 3), "right": round(right, 3), "ttl_ms": ttl}
        await self.send_to_robot(msg)
        return msg

    async def stop(self, reason: str = "operator") -> None:
        await self.send_to_robot({"type": "stop", "reason": reason})

    async def request(self, command: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send a command and wait for the robot's ack."""
        cmd_id = uuid.uuid4().hex
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await self.send_to_robot(dict(command, id=cmd_id))
            return await asyncio.wait_for(fut, timeout or self.settings.command_timeout_s)
        except asyncio.TimeoutError:
            raise CommandTimeout("robot did not answer in time") from None
        finally:
            self._pending.pop(cmd_id, None)

    # -- controllers -----------------------------------------------------------
    async def broadcast(self, msg: Dict[str, Any]) -> None:
        for client in list(self.clients):
            try:
                await client.send(msg)
            except Exception:
                self.clients.discard(client)

    # -- misc --------------------------------------------------------------
    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _post_webhook(self, event: Dict[str, Any]) -> None:
        url = self.settings.alert_webhook_url
        body = json.dumps(event).encode()

        def post() -> int:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        try:
            status = await asyncio.get_running_loop().run_in_executor(None, post)
            log.info("Alert webhook answered %s", status)
        except Exception as exc:
            log.error("Alert webhook failed: %s", exc)
