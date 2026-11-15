"""Runs on the Jetson: keeps a WebSocket open to the control server and obeys it.

Safety rules, enforced here on the robot (not in the cloud) so they hold even
when the network doesn't:

* Dead-man switch: every drive command expires after ``ttl_ms`` (<= max_ttl_ms).
  If no fresh command arrives the motors stop. Lag, a frozen browser tab or a
  dropped connection therefore means "stop", never "keep going".
* Losing the server connection stops the motors immediately.
* Speeds are clamped to -1..1 and scaled by the motors' max_speed.

Extending: register a handler for any new command type, e.g.

    async def follow(msg):            # {"type": "mode", "mode": "follow"}
        ...
        return {"mode": "follow"}     # becomes the ack's "result"
    agent.register("mode", follow)

and report things back with ``await agent.send_event("fall_detected", detail={...})``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import shutil
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from . import __version__
from .motors import clamp

log = logging.getLogger("rosey.agent")

Handler = Callable[[Dict[str, Any]], Union[Optional[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]]


def to_ws_url(server: str) -> str:
    """'https://rosey.onrender.com' -> 'wss://rosey.onrender.com/ws/robot'."""
    url = server.strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    elif not url.startswith(("ws://", "wss://")):
        url = "wss://" + url
    if not url.endswith("/ws/robot"):
        url += "/ws/robot"
    return url


def cpu_temp_c() -> Optional[float]:
    """Hottest CPU thermal zone, in degrees C (None off-Jetson)."""
    base = "/sys/class/thermal"
    temps = []
    try:
        for zone in os.listdir(base):
            try:
                with open(os.path.join(base, zone, "type")) as f:
                    kind = f.read().strip().lower()
                if "cpu" not in kind:
                    continue
                with open(os.path.join(base, zone, "temp")) as f:
                    temps.append(int(f.read().strip()) / 1000.0)
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    return round(max(temps), 1) if temps else None


class RobotAgent:
    def __init__(self, server: str, token: str, motors, name: str = "rosey",
                 max_ttl_ms: int = 2000, telemetry_interval_s: float = 1.0):
        self.url = to_ws_url(server)
        self.token = token
        self.motors = motors
        self.name = name
        self.max_ttl_ms = max_ttl_ms
        self.telemetry_interval_s = telemetry_interval_s
        self.handlers: Dict[str, Handler] = {}
        self.telemetry_sources: Dict[str, Callable[[], Any]] = {}
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._drive_deadline = 0.0
        self._started = time.monotonic()
        self._stopping = asyncio.Event()
        self._tasks: set = set()
        self.register("say", say_handler)
        self.register("ping", lambda msg: {"pong": True, "time": time.time()})

    # -- extension points ------------------------------------------------------
    def register(self, command_type: str, handler: Handler) -> None:
        self.handlers[command_type] = handler

    def add_telemetry(self, key: str, source: Callable[[], Any]) -> None:
        """Include ``source()`` under ``key`` in every telemetry message."""
        self.telemetry_sources[key] = source

    async def send_event(self, event: str, **data: Any) -> bool:
        """Report something to the server (and from there to caregivers). False if offline."""
        return await self._send({"type": "event", "event": event, "time": time.time(), **data})

    # -- connection loop -------------------------------------------------------
    async def run(self) -> None:
        import websockets

        backoff = 1.0
        log.info("Connecting to %s", self.url)
        while not self._stopping.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=20, ping_interval=20,
                                              ping_timeout=20, max_size=2 ** 20) as ws:
                    # Run the session until it ends or stop() is called (e.g. systemctl stop),
                    # then leave the `async with`, which closes the socket cleanly.
                    session = asyncio.ensure_future(self._session(ws))
                    stopper = asyncio.ensure_future(self._stopping.wait())
                    try:
                        await asyncio.wait({session, stopper}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        stopper.cancel()
                        if not session.done():
                            session.cancel()
                            await asyncio.gather(session, return_exceptions=True)
                    if not session.cancelled() and session.result():
                        backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Connection problem: %s: %s", type(exc).__name__, exc)
            finally:
                self._ws = None
                self._halt("server connection lost")
            if self._stopping.is_set():
                break
            # Jitter avoids reconnect storms; a sleeping free Render service takes ~1 min to wake.
            delay = backoff * (0.8 + 0.4 * random.random())
            log.info("Reconnecting in %.0f s", delay)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        self._stopping.set()
        self._halt("agent stopping")

    async def _session(self, ws) -> bool:
        await ws.send(json.dumps({"type": "auth", "token": self.token, "name": self.name,
                                  "version": __version__, "capabilities": sorted(["drive", "stop", *self.handlers])}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if reply.get("type") != "auth_ok":
            log.error("Server rejected the robot token (check ROBOT_TOKEN matches the server)")
            await asyncio.sleep(30)  # don't hammer the server with a bad token
            return False
        log.info("Connected to control server (server %s)", reply.get("server_version"))
        self._ws = ws
        loops = [asyncio.ensure_future(self._telemetry_loop()), asyncio.ensure_future(self._watchdog_loop())]
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        finally:
            for t in loops:
                t.cancel()
        return True

    async def _send(self, msg: Dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            async with self._send_lock:
                await ws.send(json.dumps(msg, separators=(",", ":")))
            return True
        except Exception as exc:
            log.debug("send failed: %s", exc)
            return False

    # -- commands --------------------------------------------------------------
    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "drive":
            # Handled inline (never queued behind a slow command) to keep latency low.
            try:
                left, right = clamp(msg.get("left", 0.0)), clamp(msg.get("right", 0.0))
                ttl = min(int(msg.get("ttl_ms") or 500), self.max_ttl_ms)
            except (TypeError, ValueError):
                self._halt("malformed drive command")
                return
            self.motors.set(left, right)
            self._drive_deadline = time.monotonic() + ttl / 1000.0
        elif kind == "stop":
            self._halt(msg.get("reason", "stop command"))
        else:
            task = asyncio.ensure_future(self._run_handler(kind, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_handler(self, kind: Optional[str], msg: Dict[str, Any]) -> None:
        cmd_id = msg.get("id")
        handler = self.handlers.get(kind or "")
        if handler is None:
            ack = {"ok": False, "error": "unknown command %r" % kind}
        else:
            try:
                result = handler(msg)
                if inspect.isawaitable(result):
                    result = await result
                ack = {"ok": True, "result": result}
            except Exception as exc:
                log.warning("Command %r failed: %s", kind, exc, exc_info=log.isEnabledFor(logging.DEBUG))
                ack = {"ok": False, "error": str(exc) or type(exc).__name__}
        if cmd_id is not None:
            await self._send({"type": "ack", "id": cmd_id, **ack})

    def _halt(self, reason: str) -> None:
        self._drive_deadline = 0.0
        if self.motors.moving:
            log.info("Stopping motors: %s", reason)
        self.motors.stop()

    # -- background loops --------------------------------------------------------
    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            if self.motors.moving and time.monotonic() > self._drive_deadline:
                self._halt("dead-man timeout (no fresh drive command)")

    async def _telemetry_loop(self) -> None:
        while True:
            msg = {
                "type": "telemetry",
                "motors": {"left": round(self.motors.left, 3), "right": round(self.motors.right, 3)},
                "uptime_s": round(time.monotonic() - self._started, 1),
                "cpu_temp_c": cpu_temp_c(),
            }
            for key, source in self.telemetry_sources.items():
                try:
                    msg[key] = source()
                except Exception as exc:
                    msg[key] = {"error": str(exc)}
            await self._send(msg)
            await asyncio.sleep(self.telemetry_interval_s)


async def say_handler(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Speak text through the robot's speaker with espeak-ng (sudo apt install espeak-ng).

    Swap this for your own text-to-speech (e.g. the voice stack that talks to Claude).
    """
    text = str(msg.get("text", "")).strip()[:500]
    if not text:
        raise ValueError("no text")
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe is None:
        raise RuntimeError("espeak-ng is not installed on the robot (sudo apt install espeak-ng)")
    proc = await asyncio.create_subprocess_exec(exe, "--", text, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("speech failed: %s" % err.decode(errors="replace").strip())
    return {"spoken": text}
