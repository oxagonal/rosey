import struct

import pytest

from myo_fall import myo_ble
from myo_fall.recording import read_csv, write_csv
from myo_fall.sim import fall_sideways


def test_uuids_match_myohw():
    assert myo_ble.CONTROL_SERVICE == "d5060001-a904-deb9-4748-2c7f4a124842"
    assert myo_ble.COMMAND_CHAR == "d5060401-a904-deb9-4748-2c7f4a124842"
    assert myo_ble.IMU_DATA_CHAR == "d5060402-a904-deb9-4748-2c7f4a124842"


def test_parse_imu_scaling():
    raw = struct.pack("<10h", 16384, 0, 0, 0, 0, 0, -2048, 160, -32, 0)
    s = myo_ble.parse_imu(raw, t=1.0)
    assert s.quat == (1.0, 0.0, 0.0, 0.0)
    assert s.accel == (0.0, 0.0, -1.0)
    assert s.gyro == (10.0, -2.0, 0.0)
    assert s.t == 1.0


def test_parse_imu_rejects_short_packet():
    with pytest.raises(struct.error):
        myo_ble.parse_imu(b"\x00" * 10)


def test_commands():
    assert myo_ble.set_mode_command() == bytes([0x01, 3, 0x00, 0x01, 0x00])
    assert myo_ble.sleep_mode_command() == bytes([0x09, 1, 0x01])
    assert myo_ble.vibrate_command(myo_ble.VIBRATE_LONG) == bytes([0x03, 1, 3])


def test_looks_like_myo():
    assert myo_ble.looks_like_myo(None, [myo_ble.CONTROL_SERVICE.upper()])
    assert myo_ble.looks_like_myo("My Myo", [])
    assert not myo_ble.looks_like_myo("Headphones", ["0000180f-0000-1000-8000-00805f9b34fb"])


def test_csv_roundtrip(tmp_path):
    samples = fall_sideways()[:50]
    path = tmp_path / "x.csv"
    write_csv(str(path), samples)
    back = list(read_csv(str(path)))
    assert len(back) == len(samples)
    assert back[10].accel == pytest.approx(samples[10].accel, abs=1e-4)
    assert back[10].t == pytest.approx(samples[10].t, abs=1e-3)
