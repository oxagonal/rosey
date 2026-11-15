"""BleakMyo against a fake `bleak` module: the GATT conversation, without hardware."""

import asyncio
import struct
import sys
import types

import pytest

from myo_fall import myo_ble


class FakeChar:
    def __init__(self, properties):
        self.properties = properties


class FakeBleakClient:
    last = None
    fail_on_notify = False
    command_properties = ["write", "write-without-response"]

    def __init__(self, device, disconnected_callback=None, timeout=None):
        FakeBleakClient.last = self
        self.device = device
        self.cb = disconnected_callback
        self.is_connected = False
        self.writes = []
        self.notify = {}
        self.disconnects = 0
        props = FakeBleakClient.command_properties
        self.services = types.SimpleNamespace(
            get_characteristic=lambda uuid: FakeChar(props) if uuid == myo_ble.COMMAND_CHAR else None)

    async def connect(self):
        self.is_connected = True

    async def read_gatt_char(self, uuid):
        if uuid == myo_ble.FIRMWARE_VERSION_CHAR:
            return bytearray(struct.pack("<4H", 1, 5, 1970, 2))
        if uuid == myo_ble.BATTERY_LEVEL_CHAR:
            return bytearray([77])
        raise KeyError(uuid)

    async def write_gatt_char(self, uuid, data, response=None):
        self.writes.append((uuid, bytes(data), response))

    async def start_notify(self, uuid, callback):
        if FakeBleakClient.fail_on_notify:
            raise RuntimeError("notify failed")
        self.notify[uuid] = callback

    async def disconnect(self):
        self.disconnects += 1
        if self.is_connected:
            self.is_connected = False
            if self.cb:
                self.cb(self)


@pytest.fixture
def fake_bleak(monkeypatch):
    module = types.ModuleType("bleak")
    module.BleakClient = FakeBleakClient
    monkeypatch.setitem(sys.modules, "bleak", module)
    FakeBleakClient.fail_on_notify = False
    FakeBleakClient.command_properties = ["write", "write-without-response"]
    return FakeBleakClient


DEVICE = types.SimpleNamespace(name="Myo", address="AA:BB:CC:DD:EE:FF")


def test_connect_sets_never_sleep_and_imu_only_then_streams(fake_bleak):
    async def run():
        got = []
        myo = myo_ble.BleakMyo(DEVICE, got.append)
        await myo.connect()
        c = fake_bleak.last
        assert myo.firmware == "1.5.1970.2"
        # never-sleep first (a fallen wearer lies still), then EMG off / IMU on / classifier off
        assert c.writes == [
            (myo_ble.COMMAND_CHAR, bytes([0x09, 1, 1]), True),
            (myo_ble.COMMAND_CHAR, bytes([0x01, 3, 0, 1, 0]), True),
        ]
        assert set(c.notify) == {myo_ble.IMU_DATA_CHAR}

        c.notify[myo_ble.IMU_DATA_CHAR](None, bytearray(struct.pack("<10h", 16384, 0, 0, 0, 0, 0, 2048, 0, 0, 160)))
        c.notify[myo_ble.IMU_DATA_CHAR](None, bytearray(b"\x00" * 5))  # malformed: dropped, no crash
        assert len(got) == 1 and got[0].accel == (0.0, 0.0, 1.0) and got[0].gyro == (0.0, 0.0, 10.0)

        await myo.vibrate(myo_ble.VIBRATE_LONG)
        assert c.writes[-1][1] == bytes([0x03, 1, 3])
        assert await myo.read_battery() == 77
        assert myo.is_connected

    asyncio.run(run())


def test_link_loss_is_reported_and_commands_become_no_ops(fake_bleak):
    async def run():
        myo = myo_ble.BleakMyo(DEVICE, lambda s: None)
        await myo.connect()
        c = fake_bleak.last
        c.is_connected = False
        c.cb(c)  # BlueZ reports the disconnect
        assert myo.disconnected.is_set() and not myo.is_connected
        writes = len(c.writes)
        await myo.vibrate(myo_ble.VIBRATE_SHORT)
        assert len(c.writes) == writes
        assert await myo.read_battery() is None

    asyncio.run(run())


def test_failed_setup_disconnects_instead_of_leaking_the_link(fake_bleak):
    fake_bleak.fail_on_notify = True

    async def run():
        myo = myo_ble.BleakMyo(DEVICE, lambda s: None)
        with pytest.raises(RuntimeError):
            await myo.connect()
        assert fake_bleak.last.disconnects == 1 and myo.disconnected.is_set()

    asyncio.run(run())


def test_uses_write_without_response_when_that_is_all_the_myo_offers(fake_bleak):
    fake_bleak.command_properties = ["write-without-response"]

    async def run():
        await myo_ble.BleakMyo(DEVICE, lambda s: None).connect()
        assert all(resp is False for _, _, resp in fake_bleak.last.writes)

    asyncio.run(run())
