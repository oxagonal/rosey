#!/usr/bin/env python3
"""Connect Rosey to the Render control server.

Runs the repo's agent (repo/jetson/rosey_agent) but drives the wheels with the
known-good ~/Desktop/rosey/motors.py (raw gpiochip + software PWM + SWAP_SIDES),
because the agent's own driver needs Jetson.GPIO, which can't detect this board.

Needs ROSEY_SERVER and ROBOT_TOKEN (see agent.env). Use start_rosey.sh, which
also runs pinmux_fix.py first.
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("ROSEY_HOME", HERE)     # ~/Desktop/rosey (unresolved): voices, whisper model
sys.path.insert(0, HERE)                              # motors.py
# rosey_agent: ~/Desktop/rosey/repo/jetson when run from the Desktop folder (symlinks),
# ../jetson when run from inside the repo (tests).
for _p in (os.path.join(HERE, "repo", "jetson"), os.path.join(os.path.dirname(HERE), "jetson")):
    if os.path.isdir(os.path.join(_p, "rosey_agent")):
        sys.path.insert(0, _p)
        break

import motors as hw                                   # noqa: E402
import rosey_agent.__main__ as agent_main             # noqa: E402
from rosey_agent.motors import clamp                  # noqa: E402


class RoseyMotors:
    """Adapter: the interface rosey_agent expects, on top of motors.Motors."""

    def __init__(self, max_speed=1.0, **_ignored):
        self.max_speed = max_speed
        self.left = self.right = 0.0
        self._m = hw.Motors()

    def set(self, left, right):
        clamp = lambda v: max(-1.0, min(1.0, float(v)))
        left, right = clamp(left) * self.max_speed, clamp(right) * self.max_speed
        if (left, right) != (self.left, self.right):
            self._m.drive(left, right)
            self.left, self.right = left, right

    def stop(self):
        self.set(0.0, 0.0)

    @property
    def moving(self):
        return self.left != 0.0 or self.right != 0.0

    def close(self):
        self._m.close()


def make_motors(mock=False, **kw):
    if mock:
        from rosey_agent.motors import MockMotors
        return MockMotors(**kw)
    return RoseyMotors(**kw)


agent_main.make_motors = make_motors

# Report whether the header pins were un-tristated since boot (pinmux_fix.py
# leaves this marker in /run, which is wiped on reboot). Without it the agent
# connects fine but the wheels can't move, and the website warns about that.
PINMUX_MARKER = os.environ.get("ROSEY_PINMUX_MARKER", "/run/rosey/pinmux.ok")
_RealAgent = agent_main.RobotAgent


# Speech: the Bluetooth speaker is the default output of the desktop's
# PulseAudio. A systemd service doesn't know where that audio server is, so
# point it there (same user, so access is allowed).
_run = "/run/user/%d" % os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", _run)
if os.path.exists(_run + "/pulse/native"):
    os.environ.setdefault("PULSE_SERVER", "unix:%s/pulse/native" % _run)


async def say(msg):
    """Speak through the default speaker (the Bluetooth speaker) with rosey_tts:
    libespeak-ng boosted to near full scale, played with paplay. spd-say was
    too quiet and reports success even when nothing is heard."""
    import asyncio
    import rosey_tts
    text = str(msg.get("text", "")).strip()[:500]
    if not text:
        raise ValueError("no text")
    # Blocking synth + playback runs in a thread so drive/stop commands are never delayed.
    await asyncio.get_running_loop().run_in_executor(None, rosey_tts.speak, text)
    return {"spoken": text}


class RoseyAgent(_RealAgent):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.add_telemetry("motors_ready", lambda: os.path.exists(PINMUX_MARKER))
        self.register("say", say)
        # Camera + follow mode (rosey_vision.py child process).
        self.follow_enabled = False
        self.follow_speed = 0.7
        self.vision_status = {}
        self._vision_proc = None
        self.register("follow", self._follow_command)
        self.add_telemetry("follow", lambda: dict(self.vision_status.get("follow") or {}, enabled=self.follow_enabled))
        self.add_telemetry("camera", lambda: self.vision_status.get("camera"))
        # Load the voice model now (about 2.5 s) so the first "say" isn't delayed.
        import threading, rosey_tts
        threading.Thread(target=rosey_tts.engine, daemon=True).start()
        self._voice_proc = None

    async def run(self):
        import asyncio
        voice = asyncio.ensure_future(self._voice_supervisor())
        vision = asyncio.ensure_future(self._vision_supervisor())
        try:
            await super().run()
        finally:
            voice.cancel()
            vision.cancel()
            for proc in (self._voice_proc, self._vision_proc):
                if proc is not None and proc.returncode is None:
                    proc.terminate()

    # -- follow mode ---------------------------------------------------------------
    # Safety: follow drive commands from the vision process reach the wheels only
    # while follow mode is on AND the server is connected. They carry a short ttl,
    # so the normal dead-man watchdog stops the wheels if the vision process stalls.
    # Any manual drive/stop from the website, a lost server connection or the
    # agent stopping turns follow mode off.
    def set_follow(self, enabled, reason="", speed=None):
        import logging
        if speed is not None:
            self.follow_speed = max(0.3, min(1.0, float(speed)))
        changed = enabled != self.follow_enabled
        self.follow_enabled = enabled
        if changed:
            logging.getLogger("rosey.follow").info("Follow mode %s%s", "on" if enabled else "off",
                                                   " (%s)" % reason if reason else "")
        self._to_vision({"follow": enabled, "speed": self.follow_speed})
        if not enabled and changed:
            self.motors.stop()
            self._drive_deadline = 0.0

    def _to_vision(self, msg):
        proc = self._vision_proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write((json.dumps(msg) + "\n").encode())
        except Exception:
            pass

    async def _follow_command(self, msg):
        enabled = bool(msg.get("enabled"))
        if enabled and (self._vision_proc is None or self._vision_proc.returncode is not None):
            raise RuntimeError("the camera process isn't running")
        if enabled and not os.path.exists(PINMUX_MARKER) and self.motors.__class__.__name__ != "MockMotors":
            raise RuntimeError("motors are not enabled (run sudo systemctl restart rosey)")
        self.set_follow(enabled, "website", msg.get("speed"))
        return {"follow": self.follow_enabled, "speed": self.follow_speed}

    def follow_drive(self, msg):
        """A drive/halt line from the vision process."""
        if not self.follow_enabled or self._ws is None:
            return
        if msg.get("type") == "halt":
            self.motors.stop()
            self._drive_deadline = 0.0
            return
        try:
            left, right = clamp(msg.get("left", 0.0)), clamp(msg.get("right", 0.0))
            ttl = min(int(msg.get("ttl_ms") or 300), 600, self.max_ttl_ms)
        except (TypeError, ValueError):
            self._halt("malformed follow command")
            return
        self.motors.set(left, right)
        self._drive_deadline = time.monotonic() + ttl / 1000.0

    async def _dispatch(self, msg):
        if msg.get("type") in ("drive", "stop") and self.follow_enabled:
            self.set_follow(False, "manual %s from the website" % msg.get("type"))
        await super()._dispatch(msg)

    def _halt(self, reason):
        if self.follow_enabled and not reason.startswith("dead-man"):
            self.set_follow(False, reason)
        super()._halt(reason)

    async def _on_camera_fall(self, msg):
        """The camera thinks someone fell: stop moving, alert the caregiver, talk to them."""
        import asyncio, logging
        logging.getLogger("rosey.fall").warning("Camera fall alert: %s", msg.get("description"))
        if self.follow_enabled:
            self.set_follow(False, "fall detected")
        self._halt("fall detected")
        await self.send_event("fall_detected", source="camera", description=msg.get("description"),
                              confidence=msg.get("confidence"), unconfirmed=msg.get("unconfirmed", False),
                              image=msg.get("image"))
        text = ("It looks like you may have fallen. I've sent an alert to your caregiver. "
                "Are you okay? If you're hurt, please call 911 or press your medical alert button.")
        try:
            import rosey_tts
            await asyncio.get_running_loop().run_in_executor(None, rosey_tts.speak, text)
        except Exception as exc:
            logging.getLogger("rosey.fall").error("Couldn't speak the fall message: %s", exc)

    async def _vision_supervisor(self):
        """Run rosey_vision.py (camera stream + follow detection) and relay its lines."""
        import asyncio, logging, sys
        log = logging.getLogger("rosey.vision")
        if os.environ.get("ROSEY_VISION", "1") == "0":
            log.info("Camera disabled (ROSEY_VISION=0)")
            return
        backoff = 3.0
        while not self._stopping.is_set():
            started = time.monotonic()
            proc = self._vision_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", os.path.join(HERE, "rosey_vision.py"), cwd=HERE,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=2 ** 16)
            if self.follow_enabled:            # restarted while following: stay safe, turn it off
                self.set_follow(False, "camera process restarted")
            try:
                async for raw in proc.stdout:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    kind = msg.get("type")
                    if kind in ("drive", "halt"):
                        self.follow_drive(msg)
                    elif kind == "status":
                        self.vision_status = msg
                    elif kind == "fall":
                        asyncio.ensure_future(self._on_camera_fall(msg))
            finally:
                if self.follow_enabled:
                    self.set_follow(False, "camera process stopped")
                self.vision_status = {}
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()
            if self._stopping.is_set():
                break
            backoff = 3.0 if time.monotonic() - started > 60 else min(backoff * 2, 60.0)
            log.warning("Camera process exited (code %s); restarting in %.0f s", proc.returncode, backoff)
            await asyncio.sleep(backoff)

    async def _voice_supervisor(self):
        """Run rosey_converse.py (mic -> Claude -> voice) as a child process, restart it
        if it dies, and turn its "help" lines into website alerts. A separate process,
        so a crash in the speech libraries can't take motor control down with it."""
        import asyncio, json, logging, sys, time
        log = logging.getLogger("rosey.voice")
        if os.environ.get("ROSEY_CONVERSE", "1") == "0":
            log.info("Conversation disabled (ROSEY_CONVERSE=0)")
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("No ANTHROPIC_API_KEY in agent.env: conversation is off")
            return
        backoff = 3.0
        while not self._stopping.is_set():
            started = time.monotonic()
            proc = self._voice_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", os.path.join(HERE, "rosey_converse.py"),
                cwd=HERE, stdout=asyncio.subprocess.PIPE)          # stderr -> journal
            try:
                async for raw in proc.stdout:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    kind = msg.get("type")
                    if kind == "help":
                        log.warning("Help requested by voice: %s", msg.get("text"))
                        await self.send_event("help_requested", source="voice", text=msg.get("text"))
                    elif kind in ("heard", "said"):
                        log.info("%s: %s", kind, msg.get("text"))
                    elif kind == "ready":
                        log.info("Conversation listening")
            finally:
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()
            if self._stopping.is_set():
                break
            if proc.returncode == 2:     # configuration problem (e.g. no key): don't loop
                log.error("Conversation process exited (code 2), not restarting")
                return
            backoff = 3.0 if time.monotonic() - started > 60 else min(backoff * 2, 120.0)
            log.warning("Conversation process exited (code %s); restarting in %.0f s", proc.returncode, backoff)
            await asyncio.sleep(backoff)


agent_main.RobotAgent = RoseyAgent

if __name__ == "__main__":
    agent_main.main()
