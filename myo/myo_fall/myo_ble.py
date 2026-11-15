"""Minimal Myo armband client over Bluetooth Low Energy.

Talks to the Myo directly using the GATT protocol Thalmic Labs published in
``myohw.h``, so it needs neither Myo Connect nor the official SDK (neither runs
on a Jetson). Uses `bleak`, which drives BlueZ over D-Bus on Linux, so it works
with the Jetson Orin Nano dev kit's built-in Bluetooth or any USB BT dongle.

Only the IMU is streamed (50 Hz): EMG is switched off to save battery, and the
Myo is told never to sleep. That last part matters for fall detection: by
default the Myo sleeps once it stops moving, which is exactly what a person
lying on the floor looks like.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)


def myo_uuid(short: int) -> str:
    """Expand a 16-bit Myo id into its full 128-bit UUID."""
    return "d506%04x-a904-deb9-4748-2c7f4a124842" % short


CONTROL_SERVICE = myo_uuid(0x0001)
FIRMWARE_VERSION_CHAR = myo_uuid(0x0201)
COMMAND_CHAR = myo_uuid(0x0401)
IMU_DATA_CHAR = myo_uuid(0x0402)
BATTERY_LEVEL_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"

# Scale factors from myohw.h
ORIENTATION_SCALE = 16384.0  # unit quaternion
ACCEL_SCALE = 2048.0  # LSB per g (+-16 g full scale)
GYRO_SCALE = 16.0  # LSB per deg/s (+-2000 deg/s full scale)

# Command bytes: [command, payload_size, *payload]
CMD_SET_MODE = 0x01
CMD_VIBRATE = 0x03
CMD_SET_SLEEP_MODE = 0x09

EMG_MODE_NONE = 0x00
IMU_MODE_SEND_DATA = 0x01
CLASSIFIER_MODE_DISABLED = 0x00
SLEEP_MODE_NEVER = 0x01

VIBRATE_SHORT = 1
VIBRATE_MEDIUM = 2
VIBRATE_LONG = 3

Vec3 = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]


@dataclass
class ImuSample:
    t: float  # host wall-clock time (time.time()) when the sample arrived
    quat: Quat  # w, x, y, z
    accel: Vec3  # g
    gyro: Vec3  # deg/s


def parse_imu(data: bytes, t: Optional[float] = None) -> ImuSample:
    """Decode one IMU notification (20 bytes, 10 little-endian int16)."""
    w, x, y, z, ax, ay, az, gx, gy, gz = struct.unpack("<10h", bytes(data[:20]))
    return scale_raw_imu((w, x, y, z), (ax, ay, az), (gx, gy, gz), t)


def scale_raw_imu(quat, accel, gyro, t: Optional[float] = None) -> ImuSample:
    """Convert raw Myo integers (as also produced by pyomyo) into units."""
    return ImuSample(
        t=time.time() if t is None else t,
        quat=tuple(v / ORIENTATION_SCALE for v in quat),  # type: ignore[arg-type]
        accel=tuple(v / ACCEL_SCALE for v in accel),  # type: ignore[arg-type]
        gyro=tuple(v / GYRO_SCALE for v in gyro),  # type: ignore[arg-type]
    )


def set_mode_command(emg: int = EMG_MODE_NONE, imu: int = IMU_MODE_SEND_DATA,
                     classifier: int = CLASSIFIER_MODE_DISABLED) -> bytes:
    return bytes([CMD_SET_MODE, 3, emg, imu, classifier])


def sleep_mode_command(mode: int = SLEEP_MODE_NEVER) -> bytes:
    return bytes([CMD_SET_SLEEP_MODE, 1, mode])


def vibrate_command(kind: int) -> bytes:
    return bytes([CMD_VIBRATE, 1, kind])


def looks_like_myo(name: Optional[str], service_uuids) -> bool:
    if CONTROL_SERVICE in [u.lower() for u in (service_uuids or [])]:
        return True
    return bool(name) and "myo" in name.lower()


async def scan(timeout: float = 8.0) -> List[Tuple[str, Optional[str], int]]:
    """Return (address, name, rssi) for every advertising Myo in range."""
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for address, (dev, adv) in found.items():
        name = adv.local_name or dev.name
        if looks_like_myo(name, adv.service_uuids):
            out.append((address, name, adv.rssi))
    return sorted(out, key=lambda r: -r[2])


async def find_myo(address: Optional[str] = None, timeout: float = 10.0):
    from bleak import BleakScanner

    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    return await BleakScanner.find_device_by_filter(
        lambda dev, adv: looks_like_myo(adv.local_name or dev.name, adv.service_uuids),
        timeout=timeout,
    )


class BleakMyo:
    """One connection to one Myo. Create a new instance per (re)connect."""

    def __init__(self, device, on_imu: Callable[[ImuSample], None]):
        self.device = device
        self.on_imu = on_imu
        self.disconnected = asyncio.Event()
        self.name = getattr(device, "name", None) or getattr(device, "address", str(device))
        self.firmware: Optional[str] = None
        self._client = None
        self._write_response = True

    async def connect(self) -> None:
        from bleak import BleakClient

        self._client = BleakClient(self.device, disconnected_callback=self._on_disconnect, timeout=20.0)
        await self._client.connect()
        try:
            char = self._client.services.get_characteristic(COMMAND_CHAR)
            if char is not None:
                self._write_response = "write" in char.properties
            fw = await self._client.read_gatt_char(FIRMWARE_VERSION_CHAR)
            self.firmware = "%d.%d.%d.%d" % struct.unpack("<4H", bytes(fw[:8]))
            log.info("Connected to %s (firmware %s)", self.name, self.firmware)

            await self._command(sleep_mode_command(SLEEP_MODE_NEVER))
            await self._command(set_mode_command())
            await self._client.start_notify(IMU_DATA_CHAR, self._on_imu_notify)
        except Exception:
            await self.disconnect()
            raise

    async def _command(self, payload: bytes) -> None:
        await self._client.write_gatt_char(COMMAND_CHAR, payload, response=self._write_response)

    def _on_imu_notify(self, _char, data: bytearray) -> None:
        try:
            sample = parse_imu(data)
        except struct.error:
            log.warning("Malformed IMU packet (%d bytes)", len(data))
            return
        self.on_imu(sample)

    def _on_disconnect(self, _client) -> None:
        log.warning("Myo %s disconnected", self.name)
        self.disconnected.set()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected and not self.disconnected.is_set()

    async def vibrate(self, kind: int = VIBRATE_MEDIUM) -> None:
        if self.is_connected:
            await self._command(vibrate_command(kind))

    async def read_battery(self) -> Optional[int]:
        if not self.is_connected:
            return None
        try:
            return int((await self._client.read_gatt_char(BATTERY_LEVEL_CHAR))[0])
        except Exception as exc:  # battery is nice-to-have; never kill the stream over it
            log.debug("Battery read failed: %s", exc)
            return None

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:
                log.debug("Disconnect error: %s", exc)
        self.disconnected.set()
