"""Rosey control server (FastAPI).

Run locally:
    export ROBOT_TOKEN=... OPERATOR_TOKEN=...
    uvicorn server.main:create_app --factory --reload

Endpoints (all /api/* need ``Authorization: Bearer $OPERATOR_TOKEN``):
    GET  /                 browser control page
    GET  /healthz          health check (no auth)
    GET  /api/status       is the robot online, latest telemetry
    POST /api/drive        {"linear": 0.5, "angular": 0} or {"left": .5, "right": .5}
    POST /api/stop         stop the motors now
    POST /api/say          {"text": "Time for your medication"}
    POST /api/command      any other command, e.g. {"type": "mode", "mode": "follow"}
    GET  /api/events       recent robot events (falls, etc.)
    WS   /ws/control       live control for apps/the web page (auth with the first message)
    WS   /ws/robot         the Jetson's connection (auth with the first message)
    WS   /ws/video         live camera for viewers (operator token; see server/video.py)
    WS   /ws/robot/video   the Jetson's camera uplink (robot token)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import __version__
from .config import Settings
from .hub import CommandTimeout, Connection, Hub, RobotOffline
from .protocol import CommandRequest, DriveRequest, SayRequest
from .video import Viewer, VideoRelay

log = logging.getLogger("rosey")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _token_ok(given: Optional[str], expected: str) -> bool:
    return isinstance(given, str) and hmac.compare_digest(given.encode(), expected.encode())


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(settings)
    video = VideoRelay()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        log.info("Rosey control server %s starting", __version__)
        yield
        # Render sends SIGTERM on redeploy: stop the robot rather than leave it on its last command.
        if hub.online:
            try:
                await hub.stop("server shutting down")
            except RobotOffline:
                pass

    app = FastAPI(title="Rosey control server", version=__version__, lifespan=lifespan)
    app.state.hub = hub
    app.state.video = video

    def require_operator(authorization: Optional[str] = Header(None)) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not _token_ok(token.strip(), settings.operator_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid operator token",
                                headers={"WWW-Authenticate": "Bearer"})

    operator = [Depends(require_operator)]

    @app.exception_handler(RobotOffline)
    async def _offline(_request, exc: RobotOffline):
        return JSONResponse({"detail": "robot offline"}, status_code=503)

    @app.exception_handler(CommandTimeout)
    async def _timeout(_request, exc: CommandTimeout):
        return JSONResponse({"detail": str(exc)}, status_code=504)

    # -- pages & health -------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True}

    # -- REST API ------------------------------------------------------------
    @app.get("/api/status", dependencies=operator)
    async def get_status() -> Dict[str, Any]:
        return hub.status()

    @app.post("/api/drive", dependencies=operator, status_code=202)
    async def drive(req: DriveRequest) -> Dict[str, Any]:
        left, right = req.wheels()
        return {"sent": await hub.drive(left, right, req.ttl_ms)}

    @app.post("/api/stop", dependencies=operator)
    async def stop() -> Dict[str, Any]:
        await hub.stop("operator")
        return {"stopped": True}

    @app.post("/api/say", dependencies=operator)
    async def say(req: SayRequest) -> Dict[str, Any]:
        return _ack_or_raise(await hub.request({"type": "say", "text": req.text}))

    @app.post("/api/command", dependencies=operator)
    async def command(req: CommandRequest) -> Dict[str, Any]:
        return _ack_or_raise(await hub.request(req.model_dump()))

    @app.get("/api/events", dependencies=operator)
    async def events(limit: int = 50) -> Dict[str, Any]:
        items = list(hub.events)[-max(1, min(limit, 200)):]
        return {"events": items}

    # -- WebSockets ----------------------------------------------------------
    async def ws_auth(ws: WebSocket, expected: str) -> Optional[Dict[str, Any]]:
        """Accept, then require {"type": "auth", "token": ...} as the first message.

        Browsers can't set headers on WebSockets, and tokens in URLs end up in logs,
        so the token travels inside the encrypted connection instead.
        """
        await ws.accept()
        try:
            msg = json.loads(await asyncio.wait_for(ws.receive_text(), settings.auth_timeout_s))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            msg = None
        if not isinstance(msg, dict) or msg.get("type") != "auth" or not _token_ok(msg.get("token"), expected):
            try:
                await ws.send_text(json.dumps({"type": "auth_error", "error": "invalid token"}))
                await ws.close(code=1008)
            except Exception:
                pass
            return None
        await ws.send_text(json.dumps({"type": "auth_ok", "server_version": __version__}))
        return msg

    @app.websocket("/ws/robot")
    async def robot_ws(ws: WebSocket) -> None:
        hello = await ws_auth(ws, settings.robot_token)
        if hello is None:
            return
        conn = Connection(ws)
        info = {k: hello.get(k) for k in ("name", "version", "capabilities")}
        await hub.attach_robot(conn, info)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("Robot sent invalid JSON")
                    continue
                if isinstance(msg, dict):
                    await hub.handle_robot_message(msg)
        except WebSocketDisconnect:
            pass
        finally:
            await hub.detach_robot(conn)

    @app.websocket("/ws/robot/video")
    async def robot_video_ws(ws: WebSocket) -> None:
        if await ws_auth(ws, settings.robot_token) is None:
            return
        await video.attach_robot(ws)
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    await video.robot_frame(msg["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            await video.detach_robot(ws)

    @app.websocket("/ws/video")
    async def video_ws(ws: WebSocket) -> None:
        if await ws_auth(ws, settings.operator_token) is None:
            return
        viewer = Viewer(ws)
        await viewer.send_text(video.status())
        await video.add_viewer(viewer)
        sender = asyncio.ensure_future(video.viewer_sender(viewer))
        viewer.wake.set()
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await video.viewer_message(viewer, msg)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            await video.remove_viewer(viewer)

    @app.websocket("/ws/control")
    async def control_ws(ws: WebSocket) -> None:
        if await ws_auth(ws, settings.operator_token) is None:
            return
        conn = Connection(ws)
        hub.clients.add(conn)
        driving = False
        tasks: set = set()

        async def run_command(ref: Any, command: Dict[str, Any]) -> None:
            try:
                req = CommandRequest.model_validate(command)
                result = await hub.request(req.model_dump())
                # The robot's reply is an ack ({"type": "ack", "id", "ok", "result"}): keep ok/result/error
                # but make sure our own type and ref win.
                body = {k: v for k, v in result.items() if k not in ("type", "id", "ref")}
                await conn.send({**body, "type": "command_result", "ref": ref})
            except (ValidationError, RobotOffline, CommandTimeout) as exc:
                await conn.send({"type": "command_result", "ref": ref, "ok": False, "error": _err(exc)})

        try:
            await conn.send({"type": "status", **hub.status()})
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                    kind = msg.get("type")
                    if kind == "drive":
                        req = DriveRequest.model_validate({k: v for k, v in msg.items() if k != "type"})
                        await hub.drive(*req.wheels(), ttl_ms=req.ttl_ms)
                        driving = True
                    elif kind == "stop":
                        await hub.stop("operator")
                        driving = False
                    elif kind == "command":
                        # Run in the background so a slow command never delays a stop.
                        task = asyncio.ensure_future(run_command(msg.get("ref"), msg.get("command") or {}))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif kind == "status":
                        await conn.send({"type": "status", **hub.status()})
                    else:
                        await conn.send({"type": "error", "error": "unknown message type %r" % kind})
                except (ValueError, AttributeError, ValidationError, RobotOffline) as exc:
                    await conn.send({"type": "error", "error": _err(exc)})
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(conn)
            for task in tasks:
                task.cancel()
            if driving and hub.online:
                try:
                    await hub.stop("controller disconnected")
                except RobotOffline:
                    pass

    return app


def _ack_or_raise(ack: Dict[str, Any]) -> Dict[str, Any]:
    if not ack.get("ok"):
        raise HTTPException(422, "robot rejected the command: %s" % ack.get("error", "unknown error"))
    return {"ok": True, "result": ack.get("result")}


def _err(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(e.get("msg", "invalid") for e in exc.errors())
    if isinstance(exc, RobotOffline):
        return "robot offline"
    return str(exc) or exc.__class__.__name__
