"""FallService end to end with a fake armband: alerts, buzzing, heartbeats, reconnects."""

import asyncio
import json
import socket
import time

from myo_fall import service as service_mod
from myo_fall import sim
from myo_fall.detector import FallConfig
from myo_fall.events import UdpPublisher
from myo_fall.myo_ble import VIBRATE_LONG, VIBRATE_SHORT
from myo_fall.recording import read_csv
from myo_fall.service import FallService


class FakeMyo:
    """Plays recorded samples into the service like a connected armband would."""

    def __init__(self, on_imu, samples, freeze_after=None):
        self.on_imu = on_imu
        self.samples = samples
        self.freeze_after = freeze_after
        self.name = "FakeMyo"
        self.firmware = "1.5.1970.2"
        self.disconnected = asyncio.Event()
        self.vibrations = []
        self._task = None

    async def connect(self):
        self._task = asyncio.ensure_future(self._play())

    async def _play(self):
        for i, s in enumerate(self.samples):
            if self.freeze_after is not None and i >= self.freeze_after:
                break  # stream stops but the link stays "up"
            self.on_imu(s)
            if i % 10 == 0:
                await asyncio.sleep(0.001)
        await asyncio.sleep(3600)

    @property
    def is_connected(self):
        return not self.disconnected.is_set()

    async def vibrate(self, kind):
        self.vibrations.append(kind)

    async def read_battery(self):
        return 80

    async def disconnect(self):
        if self._task:
            self._task.cancel()
        self.disconnected.set()


class UdpSink:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.addr = self.sock.getsockname()
        self.msgs = []

    def collect(self):
        while True:
            try:
                self.msgs.append(json.loads(self.sock.recv(65535)))
            except BlockingIOError:
                return self.msgs

    def has(self, **fields):
        return any(all(m.get(k) == v for k, v in fields.items()) for m in self.collect())


async def wait_for(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within %.1f s" % timeout)


def start(samples_factory, sink, **kw):
    made = []

    def factory(on_imu):
        myo = samples_factory(on_imu)
        made.append(myo)
        return myo

    pub = UdpPublisher([sink.addr], alert_repeats=2, alert_spacing_s=0.01)
    svc = FallService(pub, FallConfig(), myo_factory=factory, **kw)
    return svc, made, asyncio.ensure_future(svc.run())


def test_fall_is_published_buzzed_and_recorded(tmp_path):
    async def run():
        sink = UdpSink()
        rec = tmp_path / "session.csv"
        svc, made, task = start(lambda on_imu: FakeMyo(on_imu, sim.fall_forward_trip()), sink, record_path=str(rec))

        await wait_for(lambda: sink.has(type="fall"), 10)
        fall = next(m for m in sink.msgs if m["type"] == "fall")
        assert fall["device"] == "FakeMyo" and "rotation" in fall["event"]["cues"]
        assert sink.has(type="status", status="connected")
        assert made[0].vibrations[0] == VIBRATE_SHORT  # "connected" buzz
        await wait_for(lambda: VIBRATE_LONG in made[0].vibrations, 2)  # "fall detected" buzz
        await wait_for(lambda: sink.has(type="heartbeat", connected=True, battery=80), 3)

        svc._stopping.set()
        await asyncio.wait_for(task, 5)
        assert sink.has(type="status", status="stopped")
        assert len(list(read_csv(str(rec)))) == len(sim.fall_forward_trip())

    asyncio.run(run())


def test_frozen_stream_is_treated_as_disconnect(monkeypatch):
    monkeypatch.setattr(service_mod, "STALL_TIMEOUT_S", 0.3)

    async def run():
        sink = UdpSink()
        svc, made, task = start(lambda on_imu: FakeMyo(on_imu, sim.adl_walking(), freeze_after=50), sink)
        await wait_for(lambda: len(made) >= 2, 5)  # gave up on the frozen link and reconnected
        assert made[0].disconnected.is_set()
        assert sink.has(type="status", status="disconnected")
        svc._stopping.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(run())


def test_lost_link_reconnects_and_resets_detector():
    async def run():
        sink = UdpSink()
        svc, made, task = start(lambda on_imu: FakeMyo(on_imu, sim.adl_walking()), sink)
        await wait_for(lambda: made and svc._sample_count > 100, 5)
        made[0].disconnected.set()  # armband out of range
        await wait_for(lambda: len(made) >= 2, 5)
        assert sink.has(type="status", status="disconnected")
        assert svc.detector.state == svc.detector.IDLE
        svc._stopping.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(run())
