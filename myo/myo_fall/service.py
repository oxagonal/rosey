"""Long-running service: Myo -> fall detector -> UDP events.

Reconnects forever, sends a heartbeat every second whatever the connection
state, and treats "connected but no data" as a disconnect (a stalled stream
must not look like a calm wearer).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Callable, Optional

from .detector import Evaluation, FallConfig, FallDetector
from .events import UdpPublisher
from .myo_ble import VIBRATE_LONG, VIBRATE_SHORT, ImuSample
from .recording import CsvRecorder

log = logging.getLogger(__name__)

STALL_TIMEOUT_S = 3.0
BATTERY_INTERVAL_S = 60.0
LOW_BATTERY_PCT = 15


class FallService:
    def __init__(self, publisher: UdpPublisher, config: FallConfig, backend: str = "bleak",
                 address: Optional[str] = None, tty: Optional[str] = None,
                 record_path: Optional[str] = None, vibrate: bool = True,
                 myo_factory: Optional[Callable[[Callable[[ImuSample], None]], Any]] = None):
        """``myo_factory(on_imu)`` replaces Bluetooth discovery (used by tests with a fake armband)."""
        self.publisher = publisher
        self.myo_factory = myo_factory
        self.detector = FallDetector(config, on_evaluation=self._on_evaluation)
        self.backend = backend
        self.address = address
        self.tty = tty
        self.vibrate = vibrate
        self.recorder = CsvRecorder(record_path) if record_path else None
        self.myo = None
        self.battery: Optional[int] = None
        self.state = "starting"
        self._sample_count = 0
        self._last_sample_mono = 0.0
        self._tasks: set = set()
        self._stopping = asyncio.Event()

    # -- per-sample path (runs in the event loop; keep it fast) --------------
    def _on_imu(self, s: ImuSample) -> None:
        self._last_sample_mono = time.monotonic()
        self._sample_count += 1
        if self.recorder:
            self.recorder.write(s)
        event = self.detector.update(s.accel, s.gyro, s.t)
        if event is not None:
            log.warning("FALL DETECTED: %s", event.to_dict())
            self.publisher.fall(event.to_dict(), device=self.myo.name if self.myo else "?")
            if self.vibrate and self.myo is not None:
                self._spawn(self.myo.vibrate(VIBRATE_LONG))

    @staticmethod
    def _on_evaluation(ev: Evaluation) -> None:
        if not ev.accepted:
            log.info("Impact ignored (%s): %s", ev.reason, ev.features)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- heartbeat (independent of connection state) -------------------------
    def _streaming(self) -> bool:
        return (self.myo is not None and self.myo.is_connected
                and time.monotonic() - self._last_sample_mono < STALL_TIMEOUT_S)

    async def _heartbeat_loop(self) -> None:
        last_count, last_t = self._sample_count, time.monotonic()
        while not self._stopping.is_set():
            await self._wait(self._stopping, 1.0)
            now = time.monotonic()
            rate = (self._sample_count - last_count) / max(now - last_t, 1e-3)
            last_count, last_t = self._sample_count, now
            streaming = self._streaming()
            self.publisher.heartbeat(
                connected=streaming,
                device=self.myo.name if self.myo is not None else None,
                battery=self.battery,
                rate_hz=round(rate, 1),
                state=self.detector.state if streaming else self.state,
            )
            if self.recorder:
                self.recorder.flush()

    # -- connection management ---------------------------------------------
    async def _connect_once(self) -> bool:
        if self.myo_factory is not None:
            self.state = "connecting"
            self.myo = self.myo_factory(self._on_imu)
        elif self.backend == "pyomyo":
            from .myo_pyomyo import PyomyoMyo
            self.state = "connecting"
            self.myo = PyomyoMyo(self._on_imu, tty=self.tty)
        else:
            from .myo_ble import BleakMyo, find_myo
            self.state = "searching"
            log.info("Scanning for Myo%s ...", " " + self.address if self.address else "")
            device = await find_myo(self.address, timeout=10.0)
            if device is None:
                log.info("No Myo found (charged? awake? not connected to another computer?)")
                return False
            self.state = "connecting"
            self.myo = BleakMyo(device, self._on_imu)
        await self.myo.connect()
        return True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except (NotImplementedError, RuntimeError):
                pass
        self._spawn(self._heartbeat_loop())

        backoff = 2.0
        try:
            while not self._stopping.is_set():
                try:
                    connected = await self._connect_once()
                except Exception as exc:
                    log.warning("Connect failed: %s", exc)
                    connected = False
                if connected:
                    backoff = 2.0
                    await self._stream()
                    continue
                self.state = "searching"
                await self._wait(self._stopping, backoff)
                backoff = min(backoff * 1.5, 15.0)
        finally:
            self.state = "stopped"
            if self.myo is not None:
                await self.myo.disconnect()
            if self.recorder:
                self.recorder.close()
            self.publisher.status("stopped")
            for t in list(self._tasks):
                t.cancel()

    async def _stream(self) -> None:
        myo = self.myo
        self.detector.reset()  # never stitch data across a gap
        self._last_sample_mono = time.monotonic()
        self.state = "streaming"
        self.battery = await myo.read_battery()
        self.publisher.status("connected", device=myo.name, firmware=myo.firmware, battery=self.battery)
        if self.vibrate:
            await myo.vibrate(VIBRATE_SHORT)
        log.info("Streaming IMU from %s (battery %s%%)", myo.name, self.battery)

        last_batt = time.monotonic()
        while not self._stopping.is_set() and myo.is_connected:
            await self._wait(myo.disconnected, 1.0)
            now = time.monotonic()
            if now - self._last_sample_mono > STALL_TIMEOUT_S:
                log.warning("No IMU data for %.0f s; reconnecting", now - self._last_sample_mono)
                break
            if now - last_batt >= BATTERY_INTERVAL_S:
                last_batt = now
                self.battery = await myo.read_battery()
                if self.battery is not None and self.battery <= LOW_BATTERY_PCT:
                    log.warning("Myo battery low: %d%%", self.battery)

        await myo.disconnect()
        self.state = "disconnected"
        self.publisher.status("disconnected", device=myo.name)

    async def _wait(self, event: asyncio.Event, timeout: float) -> None:
        """Sleep up to ``timeout`` s, waking early if ``event`` or the stop flag is set."""
        waiters = {asyncio.ensure_future(event.wait()), asyncio.ensure_future(self._stopping.wait())}
        try:
            await asyncio.wait(waiters, timeout=max(timeout, 0.0), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
