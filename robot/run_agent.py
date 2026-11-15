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


def speak_in_background(text):
    """Speak through the default speaker (the Bluetooth speaker) with rosey_tts
    (Piper voice, played with paplay) on a worker thread, logging the outcome.
    Synth + playback often takes longer than the server's 5 s command timeout,
    so callers don't wait for it."""
    import logging, threading
    import rosey_tts
    log = logging.getLogger("rosey.say")

    def work():
        try:
            rosey_tts.speak(text)
            log.info("said (website): %s", text)
        except Exception as exc:
            log.error("could not say %r: %s", text, exc)
    threading.Thread(target=work, daemon=True).start()


class RoseyAgent(_RealAgent):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.add_telemetry("motors_ready", lambda: os.path.exists(PINMUX_MARKER))
        self.register("say", self._say)
        # Camera + follow mode (rosey_vision.py child process).
        self.follow_enabled = False
        self.follow_speed = 1.0
        self.vision_status = {}
        self._vision_proc = None
        self.register("follow", self._follow_command)
        self.register("notify_info", self._notify_info)
        self.register("notify_test", self._notify_test)
        self.add_telemetry("follow", lambda: dict(self.vision_status.get("follow") or {}, enabled=self.follow_enabled))
        self.add_telemetry("camera", lambda: self.vision_status.get("camera"))
        # Headless health (no screen or terminal on the robot): shown on the website.
        self.speaker_state = "starting"   # starting | ok | no output | off | disabled
        self.voice_state = "starting"     # starting | listening | restarting | off: <why>
        self.add_telemetry("speaker", lambda: self.speaker_state)
        self.add_telemetry("voice", lambda: self.voice_state)
        # Load the voice model now (about 2.5 s) so the first "say" isn't delayed.
        import threading, rosey_tts
        threading.Thread(target=rosey_tts.engine, daemon=True).start()
        self._voice_proc = None

    async def run(self):
        import asyncio
        voice = asyncio.ensure_future(self._voice_supervisor())
        vision = asyncio.ensure_future(self._vision_supervisor())
        audio = asyncio.ensure_future(self._audio_keeper())
        try:
            await super().run()
        finally:
            voice.cancel()
            vision.cancel()
            audio.cancel()
            for proc in (self._voice_proc, self._vision_proc):
                if proc is not None and proc.returncode is None:
                    proc.terminate()

    async def _say(self, msg):
        """Website "Say" box. Fails right away if the speaker can't be heard,
        instead of playing into nothing and reporting success."""
        text = str(msg.get("text", "")).strip()[:500]
        if not text:
            raise ValueError("no text")
        if self.speaker_state == "off":
            raise RuntimeError("speaker is not connected (is it switched on?)")
        if self.speaker_state == "no output":
            raise RuntimeError("speaker is connected but has no sound output")
        speak_in_background(text)
        return {"spoken": text}

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

    # -- phone notifications (ntfy) ------------------------------------------------
    async def send_event(self, event, **data):
        """Report to the website as usual, and push alerts to caregivers' phones.
        The phone push goes out even if the website connection is down."""
        import asyncio
        sent = await super().send_event(event, **data)
        import rosey_notify
        if event in rosey_notify.ALERTS:
            asyncio.get_running_loop().run_in_executor(None, rosey_notify.notify_event, dict(data, event=event))
        return sent

    async def _notify_info(self, msg):
        import rosey_notify
        t = rosey_notify.topic()
        return {"topic": t, "link": rosey_notify.subscribe_link(t), "server": rosey_notify.SERVER}

    async def _notify_test(self, msg):
        import asyncio, rosey_notify
        status = await asyncio.get_running_loop().run_in_executor(
            None, lambda: rosey_notify.send("Rosey: test notification",
                                            "Phone alerts are working. Tap to open the control page.", 3, "white_check_mark"))
        return {"sent": status == 200}

    async def _audio_keeper(self):
        """Keep the Bluetooth speaker connected and set as the default output.
        It doesn't always reconnect by itself after a reboot or going out of range."""
        import asyncio, logging, re
        log = logging.getLogger("rosey.audio")
        mac = os.environ.get("ROSEY_SPEAKER_BT", "A0:8A:35:69:64:82").strip()
        if not mac or mac == "0":
            self.speaker_state = "disabled"
            return
        sink = "bluez_sink.%s.a2dp_sink" % mac.replace(":", "_")

        async def run(*cmd, timeout=20.0):
            try:
                p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(p.communicate(), timeout)
                return p.returncode, out.decode(errors="replace")
            except (OSError, asyncio.TimeoutError) as exc:
                return -1, str(exc)

        await asyncio.sleep(5)
        was = None
        while not self._stopping.is_set():
            _, info = await run("bluetoothctl", "info", mac)
            connected = bool(re.search(r"Connected:\s*yes", info))
            if not connected:
                if was is not False:
                    log.warning("Speaker %s not connected; trying to reconnect", mac)
                await run("bluetoothctl", "connect", mac)
                _, info = await run("bluetoothctl", "info", mac)
                connected = bool(re.search(r"Connected:\s*yes", info))
            if connected:
                _, sinks = await run("pactl", "list", "short", "sinks")
                _, default = await run("pactl", "get-default-sink")
                if sink not in sinks and connected != was:
                    log.warning("Speaker is connected but has no audio output in PulseAudio "
                                "(another session may have claimed it, e.g. the login screen)")
                self.speaker_state = "ok" if sink in sinks else "no output"
                if sink in sinks and default.strip() != sink:
                    await run("pactl", "set-default-sink", sink)
                    log.info("Speaker set as default output")
            else:
                self.speaker_state = "off"
            if connected != was:
                log.info("Speaker %s", "connected" if connected else "still not connected (is it on?)")
                was = connected
            await asyncio.sleep(20 if connected else 10)

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
            self.voice_state = "off: disabled"
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("No ANTHROPIC_API_KEY in agent.env: conversation is off")
            self.voice_state = "off: no API key"
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
                        self.voice_state = "listening"
            finally:
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()
            if self._stopping.is_set():
                break
            if proc.returncode == 2:     # configuration problem (e.g. no key): don't loop
                log.error("Conversation process exited (code 2), not restarting")
                self.voice_state = "off: setup problem"
                return
            backoff = 3.0 if time.monotonic() - started > 60 else min(backoff * 2, 120.0)
            self.voice_state = "restarting"
            log.warning("Conversation process exited (code %s); restarting in %.0f s", proc.returncode, backoff)
            await asyncio.sleep(backoff)


agent_main.RobotAgent = RoseyAgent

if __name__ == "__main__":
    agent_main.main()
