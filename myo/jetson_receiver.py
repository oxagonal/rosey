#!/usr/bin/env python3
"""Receive fall alerts on the Jetson.

Standalone (standard library only), so you can copy this one file into
whatever runs on the Jetson, or import ``FallAlertReceiver`` and hook its
callbacks into your own app or ROS 2 node.

It listens for the UDP JSON events sent by ``python -m myo_fall run`` and:

* on a fall: logs it, optionally runs a shell command and/or drives a GPIO pin
  (buzzer, LED, relay) for a few seconds;
* tracks heartbeats: if the armband stops streaming (battery, range, crash,
  detector not running) for ``--stale-after`` seconds it reports the sensor
  OFFLINE and runs ``--on-offline``. Silence is not "all clear".

Examples:

    python3 jetson_receiver.py
    python3 jetson_receiver.py --on-fall 'aplay /usr/share/sounds/alsa/Front_Center.wav'
    python3 jetson_receiver.py --gpio-pin 7 --gpio-seconds 30

Commands get the event in environment variables FALL_JSON, FALL_CONFIDENCE
and FALL_DEVICE (or OFFLINE_REASON for --on-offline).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

log = logging.getLogger("fall-receiver")


class FallAlertReceiver:
    """Protocol logic, independent of sockets so it can be tested or embedded."""

    def __init__(self, stale_after_s: float = 5.0,
                 on_fall: Optional[Callable[[dict], None]] = None,
                 on_offline: Optional[Callable[[str], None]] = None,
                 on_online: Optional[Callable[[dict], None]] = None,
                 now: Optional[float] = None):
        self.stale_after_s = stale_after_s
        self.on_fall = on_fall
        self.on_offline = on_offline
        self.on_online = on_online
        self.online: Optional[bool] = None  # unknown until the first heartbeat or timeout
        self.last_heartbeat: Optional[dict] = None
        self._started = time.monotonic() if now is None else now
        self._last_streaming: Optional[float] = None
        self._last_msg: Optional[float] = None
        self._seen = OrderedDict()  # fall ids, for de-duplicating UDP repeats

    def handle(self, msg: dict, now: Optional[float] = None) -> Optional[str]:
        """Process one decoded message. Returns 'fall' for a new fall alert."""
        now = time.monotonic() if now is None else now
        self._last_msg = now
        kind = msg.get("type")
        if kind == "fall":
            fall_id = msg.get("id")
            if fall_id in self._seen:
                return None
            self._seen[fall_id] = now
            while len(self._seen) > 256:
                self._seen.popitem(last=False)
            if self.on_fall:
                self.on_fall(msg)
            return "fall"
        if kind == "heartbeat":
            self.last_heartbeat = msg
            if msg.get("connected"):
                self._last_streaming = now
                if self.online is not True:
                    self.online = True
                    if self.on_online:
                        self.on_online(msg)
            else:
                self.check(now)
        elif kind == "status":
            log.info("detector status: %s", {k: v for k, v in msg.items() if k not in ("v", "type")})
        return None

    def check(self, now: Optional[float] = None) -> None:
        """Call periodically; flips to OFFLINE when streaming heartbeats stop."""
        now = time.monotonic() if now is None else now
        ref = self._last_streaming if self._last_streaming is not None else self._started
        if now - ref < self.stale_after_s or self.online is False:
            return
        self.online = False
        if self._last_msg is None or now - self._last_msg >= self.stale_after_s:
            reason = "no messages from the fall detector service"
        else:
            hb = self.last_heartbeat or {}
            reason = "armband not streaming (detector state: %s)" % hb.get("state", "unknown")
        if self.on_offline:
            self.on_offline(reason)


def run_command(cmd: Optional[str], env_extra: dict) -> None:
    if not cmd:
        return
    env = dict(os.environ, **{k: str(v) for k, v in env_extra.items()})
    try:
        subprocess.Popen(cmd, shell=True, env=env)
    except OSError as exc:
        log.error("command failed: %s", exc)


class GpioAlarm:
    """Drive a Jetson header pin high for a while on each fall (BOARD numbering)."""

    def __init__(self, pin: int, seconds: float):
        import Jetson.GPIO as GPIO  # installed with JetPack; pip install Jetson.GPIO otherwise

        self.GPIO, self.pin, self.seconds = GPIO, pin, seconds
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        self._timer: Optional[threading.Timer] = None

    def trigger(self) -> None:
        self.GPIO.output(self.pin, self.GPIO.HIGH)
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.seconds, self.GPIO.output, (self.pin, self.GPIO.LOW))
        self._timer.daemon = True
        self._timer.start()

    def close(self) -> None:
        if self._timer:
            self._timer.cancel()
        self.GPIO.cleanup(self.pin)


def main() -> None:
    ap = argparse.ArgumentParser(description="Receive Myo fall alerts on the Jetson.")
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--stale-after", type=float, default=5.0, help="seconds without data before OFFLINE")
    ap.add_argument("--on-fall", help="shell command to run on each fall")
    ap.add_argument("--on-offline", help="shell command to run when the sensor goes offline")
    ap.add_argument("--gpio-pin", type=int, help="Jetson header pin (BOARD numbering) to drive high on a fall")
    ap.add_argument("--gpio-seconds", type=float, default=10.0)
    ap.add_argument("-v", "--verbose", action="store_true", help="log every heartbeat")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    gpio = GpioAlarm(args.gpio_pin, args.gpio_seconds) if args.gpio_pin else None

    def on_fall(msg: dict) -> None:
        ev = msg.get("event", {})
        log.warning("*** FALL DETECTED *** device=%s confidence=%s peak=%sg cues=%s",
                    msg.get("device"), ev.get("confidence"), ev.get("peak_g"), ev.get("cues"))
        if gpio:
            gpio.trigger()
        run_command(args.on_fall, {"FALL_JSON": json.dumps(msg), "FALL_CONFIDENCE": ev.get("confidence"),
                                   "FALL_DEVICE": msg.get("device")})

    def on_offline(reason: str) -> None:
        log.error("SENSOR OFFLINE: %s. Falls cannot be detected right now.", reason)
        run_command(args.on_offline, {"OFFLINE_REASON": reason})

    def on_online(hb: dict) -> None:
        log.info("Sensor online: %s, battery %s%%, %s Hz", hb.get("device"), hb.get("battery"), hb.get("rate_hz"))

    rx = FallAlertReceiver(args.stale_after, on_fall=on_fall, on_offline=on_offline, on_online=on_online)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(0.5)
    log.info("Listening for fall alerts on udp://%s:%d", args.bind, args.port)
    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                rx.check()
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                log.warning("ignoring non-JSON datagram from %s", addr[0])
                continue
            if msg.get("type") == "heartbeat":
                log.debug("heartbeat from %s: %s", addr[0], msg)
            rx.handle(msg)
            rx.check()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if gpio:
            gpio.close()


if __name__ == "__main__":
    main()
