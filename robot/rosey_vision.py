#!/usr/bin/env python3
"""Rosey's camera: live video to the website + person following.

Run by run_agent.py as a child process (like rosey_converse.py). It owns the
webcam and talks to the agent over stdin/stdout, one JSON object per line:

    agent -> vision (stdin):   {"follow": true, "speed": 0.7}   /  {"follow": false}
    vision -> agent (stdout):  {"type": "drive", "left": .6, "right": .5, "ttl_ms": 400}
                               {"type": "halt"}
                               {"type": "status", "camera": {...}, "follow": {...}}

The agent decides whether follow drive commands reach the wheels (only while
follow mode is on and the server is connected), so this process can't move
the robot on its own.

Video goes straight to the control server over its own WebSocket
(/ws/robot/video, see repo/server/video.py), only while someone is watching.

Optimisations:
  * the camera delivers MJPEG; frames stay compressed until needed, and are
    decoded at half size directly by libjpeg's DCT scaling (IMREAD_REDUCED_*),
    which is ~3x cheaper than decoding full size and resizing;
  * "high" sends the camera's own 720p JPEG untouched (no decode, no re-encode);
  * nothing is decoded or sent when nobody is watching and follow is off;
  * the robot keeps at most 2 frames in flight to the server, so a slow uplink
    drops the frame rate instead of adding delay;
  * follow boxes are sent as numbers and drawn by the browser, not burned in.

CLI test (no agent, no server):  python3 rosey_vision.py --bench
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import threading
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CAM = os.environ.get("ROSEY_CAMERA", "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._onn_USB_2.0_webcam_SN0001-video-index0")
CAP_W, CAP_H, CAP_FPS = 1280, 720, 30

# preset -> (decode reduction, output width, jpeg quality, max fps); reduction 0 = send camera JPEG as is
PRESETS = {
    "low": (cv2.IMREAD_REDUCED_COLOR_4, 320, 55, 8),       # 320x180
    "medium": (cv2.IMREAD_REDUCED_COLOR_2, 640, 60, 15),   # 640x360
    "high": (0, 1280, None, 15),                           # camera's own 1280x720 JPEG
}
ROBOT_IN_FLIGHT = 2


def emit(kind: str, **data) -> None:
    sys.stdout.write(json.dumps(dict(type=kind, **data), separators=(",", ":")) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    print("rosey_vision: " + msg, file=sys.stderr, flush=True)


# -- camera ---------------------------------------------------------------------
class Camera:
    """Reads compressed MJPEG frames in a thread; keeps only the newest."""

    def __init__(self, dev: str = CAM):
        self.dev = dev
        self.raw = None
        self.stamp = 0.0
        self.seq = 0
        self.ok = False
        self.fps = 0.0
        self.error = ""
        self.cond = threading.Condition()
        threading.Thread(target=self._loop, daemon=True, name="camera").start()

    def _open(self):
        dev = self.dev if os.path.exists(self.dev) else "/dev/video0"
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_H)
        cap.set(cv2.CAP_PROP_FPS, CAP_FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)       # give us the JPEG bytes, don't decode
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        if not cap.isOpened():
            raise RuntimeError("can't open camera %s" % dev)
        return cap

    def _loop(self):
        while True:
            try:
                cap = self._open()
                n, t0, fails = 0, time.monotonic(), 0
                while True:
                    ok, buf = cap.read()
                    if not ok or buf is None or buf.ndim != 2:
                        fails += 1
                        if fails > 30:
                            raise RuntimeError("camera stopped sending frames (unplugged?)")
                        time.sleep(0.02)
                        continue
                    fails = 0
                    with self.cond:
                        self.raw, self.stamp, self.ok, self.error = buf, time.monotonic(), True, ""
                        self.seq += 1
                        self.cond.notify_all()
                    n += 1
                    if time.monotonic() - t0 >= 2:
                        self.fps, n, t0 = n / (time.monotonic() - t0), 0, time.monotonic()
            except Exception as exc:
                with self.cond:
                    self.ok, self.error, self.fps = False, str(exc), 0.0
                log("camera problem: %s; retrying in 3 s" % exc)
                try:
                    cap.release()
                except Exception:
                    pass
                time.sleep(3)

    def latest(self):
        with self.cond:
            return self.raw, self.stamp, self.seq

    def wait_newer(self, seq: int, after: float = 0.0, timeout: float = 1.0):
        end = time.monotonic() + timeout
        with self.cond:
            while self.raw is None or self.seq == seq or self.stamp <= after:
                left = end - time.monotonic()
                if left <= 0:
                    return None, 0.0, seq
                self.cond.wait(left)
            return self.raw, self.stamp, self.seq


# -- fall watch -----------------------------------------------------------------
FALL_WATCH = os.environ.get("ROSEY_FALL_WATCH", "1") != "0" and bool(os.environ.get("ANTHROPIC_API_KEY"))
FALL_EVERY_S = 0.5


class FallCheck:
    """Runs rosey_fall.FallWatch on detector passes; confirmations happen in a thread
    so the follow loop never waits on the network."""

    def __init__(self):
        from rosey_fall import FallWatch
        self.watch = FallWatch()
        self.busy = False

    def observe(self, img, dets) -> None:
        if self.busy or not self.watch.observe(img, dets):
            return
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        small = cv2.resize(img, (320, 180), interpolation=cv2.INTER_AREA)
        _, thumb = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
        self.busy = True
        threading.Thread(target=self._confirm, args=(jpg.tobytes(), thumb.tobytes()), daemon=True).start()

    def _confirm(self, jpg: bytes, thumb: bytes) -> None:
        import base64
        try:
            log("possible fall seen; asking Claude to confirm")
            v = self.watch.confirm(jpg)
            log("fall check: %s" % json.dumps(v))
            if v.get("fallen"):
                emit("fall", description=v.get("description") or "A person may have fallen.",
                     confidence=v.get("confidence"), unconfirmed=bool(v.get("unconfirmed")),
                     image="data:image/jpeg;base64," + base64.b64encode(thumb).decode())
        finally:
            self.busy = False


# -- follow loop ----------------------------------------------------------------
class FollowLoop:
    def __init__(self, cam: Camera):
        self.cam = cam
        self.enabled = False
        self.speed = 0.7
        self.lock = threading.Lock()
        self.state, self.detail, self.det_fps = "off", "", 0.0
        self.overlay = None           # latest boxes for the video overlay
        self._reset = False
        threading.Thread(target=self._run, daemon=True, name="follow").start()

    def set(self, enabled: bool, speed=None) -> None:
        with self.lock:
            if speed is not None:
                self.speed = max(0.3, min(1.0, float(speed)))
            if enabled and not self.enabled:
                self._reset = True
            self.enabled = enabled
            if not enabled:
                self.state, self.detail, self.overlay = "off", "", None
        if not enabled:
            emit("halt")

    def _run(self):
        from rosey_detect import Detector
        from rosey_follow import Follower
        det = None
        fol = Follower()
        seq, after = 0, 0.0
        n, t0 = 0, time.monotonic()
        fall = FallCheck() if FALL_WATCH else None
        last_pass = 0.0
        while True:
            if not self.enabled:
                # Not following: still run the detector about twice a second to watch for falls.
                if fall is None:
                    time.sleep(0.05)
                    continue
                gap = FALL_EVERY_S - (time.monotonic() - last_pass)
                if gap > 0:
                    time.sleep(min(gap, 0.05))
                    continue
            if det is None:
                if self.enabled:
                    self.state, self.detail = "starting", "loading the person detector"
                det = Detector(threads=3)
            with self.lock:
                if self._reset:
                    fol.reset()
                    self._reset = False
                fol.max_speed = self.speed
            raw, stamp, seq = self.cam.wait_newer(seq, after)
            last_pass = time.monotonic()
            if raw is None:
                if self.enabled:
                    emit("halt")
                    self.state, self.detail = "no camera", self.cam.error or "waiting for the camera"
                continue
            img = cv2.imdecode(raw, cv2.IMREAD_REDUCED_COLOR_2)       # 640x360
            if img is None:
                continue
            dets = det(img, threshold=0.35)
            if fall is not None:
                fall.observe(img, dets)
            if not self.enabled:
                continue
            out = fol.step(img, dets)
            h, w = img.shape[:2]
            norm = lambda b: [round(b[0] / w, 4), round(b[1] / h, 4), round(b[2] / w, 4), round(b[3] / h, 4)]
            with self.lock:
                self.state, self.detail = out.state, out.detail
                self.overlay = {"state": out.state, "target": norm(out.target) if out.target else None,
                                "obstacles": [norm(b) for b in out.obstacles][:8]}
            n += 1
            if time.monotonic() - t0 >= 2:
                self.det_fps, n, t0 = n / (time.monotonic() - t0), 0, time.monotonic()

            if out.pulse_s > 0:
                # Turn in a short burst, stop, let the picture settle, then look again
                # (stops camera/detector lag from making it overshoot).
                emit("drive", left=out.left, right=out.right, ttl_ms=int(out.pulse_s * 1000) + 100)
                time.sleep(out.pulse_s)
                emit("halt")
                time.sleep(0.1)
                after = time.monotonic()
            elif out.moving:
                emit("drive", left=out.left, right=out.right, ttl_ms=400)
                after = 0.0
            else:
                emit("halt")
                after = 0.0


# -- video uplink -----------------------------------------------------------------
def encode(raw, preset: str):
    """Return (jpeg_bytes, w, h) for a preset from the camera's raw MJPEG frame."""
    reduce_flag, width, quality, _ = PRESETS[preset]
    if reduce_flag == 0:
        return raw.tobytes(), CAP_W, CAP_H
    img = cv2.imdecode(raw, reduce_flag)
    if img is None:
        return None, 0, 0
    if img.shape[1] != width:
        img = cv2.resize(img, (width, int(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return (jpg.tobytes() if ok else None), img.shape[1], img.shape[0]


def ws_url() -> str:
    url = os.environ.get("ROSEY_SERVER", "").strip().rstrip("/")
    for a, b in (("https://", "wss://"), ("http://", "ws://")):
        if url.startswith(a):
            url = b + url[len(a):]
    return url + "/ws/robot/video"


async def video_uplink(cam: Camera, follow: FollowLoop, stats: dict) -> None:
    import websockets
    token = os.environ.get("ROBOT_TOKEN", "")
    loop = asyncio.get_running_loop()
    backoff = 2.0
    while True:
        try:
            async with websockets.connect(ws_url(), open_timeout=20, ping_interval=20, ping_timeout=20,
                                          max_size=2 ** 20, write_limit=2 ** 18) as ws:
                await ws.send(json.dumps({"type": "auth", "token": token}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if reply.get("type") != "auth_ok":
                    log("server rejected the robot token for video")
                    await asyncio.sleep(60)
                    continue
                log("video link up")
                backoff = 2.0
                cfg = {"viewers": 0, "preset": "medium"}
                in_flight = [0]
                wake = asyncio.Event()

                async def reader():
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue
                        m = json.loads(raw)
                        if m.get("type") == "video_config":
                            cfg["viewers"] = int(m.get("viewers") or 0)
                            if m.get("preset") in PRESETS:
                                cfg["preset"] = m["preset"]
                            log("viewers=%d preset=%s" % (cfg["viewers"], cfg["preset"]))
                        elif m.get("type") == "frame_ack":
                            in_flight[0] = max(0, in_flight[0] - 1)
                        wake.set()

                rd = asyncio.ensure_future(reader())
                seq, sent, n, t0 = 0, 0, 0, time.monotonic()
                last_send = 0.0
                try:
                    while not rd.done():
                        if cfg["viewers"] <= 0 or in_flight[0] >= ROBOT_IN_FLIGHT:
                            wake.clear()
                            try:
                                await asyncio.wait_for(wake.wait(), 1.0)
                            except asyncio.TimeoutError:
                                pass
                            stats.update(stream_fps=0.0 if cfg["viewers"] <= 0 else stats.get("stream_fps", 0.0))
                            continue
                        preset = cfg["preset"]
                        min_gap = 1.0 / PRESETS[preset][3]
                        wait = min_gap - (time.monotonic() - last_send)
                        if wait > 0:
                            await asyncio.sleep(wait)
                        last_send = time.monotonic()
                        raw, stamp, seq = await loop.run_in_executor(None, cam.wait_newer, seq, 0.0, 1.0)
                        if raw is None:
                            continue
                        jpg, w, h = await loop.run_in_executor(None, encode, raw, preset)
                        if jpg is None:
                            continue
                        sent += 1
                        head = json.dumps({"seq": sent, "w": w, "h": h, "t": round(time.time(), 3),
                                           "overlay": follow.overlay}, separators=(",", ":")).encode()
                        in_flight[0] += 1
                        await ws.send(struct.pack(">I", len(head)) + head + jpg)
                        n += 1
                        if time.monotonic() - t0 >= 2:
                            stats.update(stream_fps=round(n / (time.monotonic() - t0), 1), stream_kb=len(jpg) // 1024)
                            n, t0 = 0, time.monotonic()
                finally:
                    rd.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log("video link problem: %s: %s" % (type(exc).__name__, exc))
        stats["stream_fps"] = 0.0
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


async def stdin_commands(follow: FollowLoop) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:                       # agent went away: stop and exit
            follow.set(False)
            os._exit(0)
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "follow" in msg:
            follow.set(bool(msg["follow"]), msg.get("speed"))


async def status_loop(cam: Camera, follow: FollowLoop, stats: dict) -> None:
    while True:
        emit("status",
             camera={"ok": cam.ok, "fps": round(cam.fps, 1), "error": cam.error or None,
                     "stream_fps": stats.get("stream_fps", 0.0), "stream_kb": stats.get("stream_kb")},
             follow={"enabled": follow.enabled, "state": follow.state, "detail": follow.detail,
                     "detect_fps": round(follow.det_fps, 1) if follow.enabled else None})
        await asyncio.sleep(1.0)


SNAPSHOT_DIR = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
SNAPSHOT_JPG = os.path.join(SNAPSHOT_DIR, "rosey-camera.jpg")
SNAPSHOT_JSON = os.path.join(SNAPSHOT_DIR, "rosey-camera.json")


def snapshot_loop(cam: Camera, follow: FollowLoop) -> None:
    """Once a second, leave the newest frame (640x360) and what the camera process
    knows in /run/user/<uid> (RAM only), for the conversation to look at.
    Atomic rename, so a reader never sees half a file."""
    seq = 0
    while True:
        time.sleep(1.0)
        try:
            raw, stamp, seq = cam.wait_newer(seq, 0.0, 1.0)
            if raw is None:
                continue
            jpg, w, h = encode(raw, "medium")
            if jpg is None:
                continue
            with open(SNAPSHOT_JPG + ".tmp", "wb") as f:
                f.write(jpg)
            os.replace(SNAPSHOT_JPG + ".tmp", SNAPSHOT_JPG)
            info = {"time": time.time(), "w": w, "h": h,
                    "follow": {"enabled": follow.enabled, "state": follow.state, "detail": follow.detail},
                    "overlay": follow.overlay}
            with open(SNAPSHOT_JSON + ".tmp", "w") as f:
                json.dump(info, f)
            os.replace(SNAPSHOT_JSON + ".tmp", SNAPSHOT_JSON)
        except Exception as exc:
            log("snapshot problem: %s" % exc)
            time.sleep(5)


async def amain() -> None:
    cam = Camera()
    follow = FollowLoop(cam)
    stats: dict = {}
    if os.environ.get("ROSEY_VISION_CONTEXT", "1") != "0":
        threading.Thread(target=snapshot_loop, args=(cam, follow), daemon=True, name="snapshot").start()
    await asyncio.gather(video_uplink(cam, follow, stats), stdin_commands(follow), status_loop(cam, follow, stats))


def bench() -> None:
    cam = Camera()
    raw, _, _ = cam.wait_newer(0, 0.0, 5.0)
    if raw is None:
        raise SystemExit("no camera frames: %s" % cam.error)
    time.sleep(2.5)
    print("camera: %.1f fps, raw JPEG %d KB" % (cam.fps, cam.latest()[0].size // 1024))
    for p in PRESETS:
        t = time.monotonic()
        for _ in range(20):
            jpg, w, h = encode(cam.latest()[0], p)
        print("%-6s %4dx%-4d %5.1f ms/frame  %3d KB" % (p, w, h, (time.monotonic() - t) * 50, len(jpg) // 1024))


if __name__ == "__main__":
    if "--bench" in sys.argv:
        bench()
        os._exit(0)        # camera thread is blocked in read(); don't wait for it
    else:
        asyncio.run(amain())
