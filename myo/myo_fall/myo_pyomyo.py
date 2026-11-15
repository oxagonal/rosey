"""Alternative backend: the blue BLED112 USB dongle that shipped with the Myo.

Use this only if the Jetson has no working Bluetooth (e.g. a carrier board
without the M.2 wireless card). It wraps `pyomyo`, which speaks to the dongle
over a serial port, and exposes the same interface as ``BleakMyo``.

pyomyo is blocking and not thread-safe, so all dongle I/O happens on one worker
thread; vibration requests are queued to that thread.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Callable, Optional

from .myo_ble import (IMU_MODE_SEND_DATA, ImuSample, VIBRATE_MEDIUM, scale_raw_imu,
                      set_mode_command)

log = logging.getLogger(__name__)

COMMAND_HANDLE = 0x19  # GATT handle of the Myo command characteristic


class PyomyoMyo:
    def __init__(self, on_imu: Callable[[ImuSample], None], tty: Optional[str] = None):
        self.on_imu = on_imu
        self.tty = tty
        self.name = "Myo (BLED112)"
        self.firmware: Optional[str] = None
        self.disconnected = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._battery: Optional[int] = None
        self._vibrations: "queue.Queue[int]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        ready: "asyncio.Future[None]" = self._loop.create_future()
        self._thread = threading.Thread(target=self._worker, args=(ready,), daemon=True)
        self._thread.start()
        await ready

    def _worker(self, ready: "asyncio.Future[None]") -> None:
        loop = self._loop
        assert loop is not None
        try:
            from pyomyo import Myo, emg_mode

            myo = Myo(tty=self.tty, mode=emg_mode.NO_DATA)
            myo.add_imu_handler(self._on_raw_imu)
            myo.add_battery_handler(self._on_battery)
            myo.connect()  # also enables IMU notifications and never-sleep
            myo.write_attr(COMMAND_HANDLE, set_mode_command(imu=IMU_MODE_SEND_DATA))
        except Exception as exc:
            loop.call_soon_threadsafe(_set_exception, ready, exc)
            return
        loop.call_soon_threadsafe(_set_result, ready)
        try:
            while not self._stop.is_set():
                myo.run()
                while not self._vibrations.empty():
                    myo.vibrate(self._vibrations.get_nowait())
        except Exception as exc:
            log.warning("pyomyo worker stopped: %s", exc)
        finally:
            try:
                myo.disconnect()
            except Exception:
                pass
            loop.call_soon_threadsafe(self.disconnected.set)

    def _on_raw_imu(self, quat, acc, gyro) -> None:
        sample = scale_raw_imu(quat, acc, gyro, time.time())
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self.on_imu, sample)

    def _on_battery(self, level: int) -> None:
        self._battery = level

    @property
    def is_connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self.disconnected.is_set()

    async def vibrate(self, kind: int = VIBRATE_MEDIUM) -> None:
        self._vibrations.put(kind)

    async def read_battery(self) -> Optional[int]:
        return self._battery

    async def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 2.0)
        self.disconnected.set()


def _set_result(fut: "asyncio.Future[None]") -> None:
    if not fut.done():
        fut.set_result(None)


def _set_exception(fut: "asyncio.Future[None]", exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
