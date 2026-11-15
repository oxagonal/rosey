import asyncio
import json

from rosey_agent.myo_bridge import MyoBridge


class FakeAgent:
    def __init__(self):
        self.events = []

    async def send_event(self, event, **data):
        self.events.append((event, data))
        return True


def test_forwards_each_fall_once_and_reports_status():
    async def run():
        agent = FakeAgent()
        bridge = MyoBridge(agent)
        assert bridge.status()["streaming"] is False
        bridge.datagram_received(json.dumps({"type": "heartbeat", "connected": True, "battery": 77,
                                             "rate_hz": 50.0}).encode(), None)
        fall = json.dumps({"type": "fall", "id": "abc", "device": "Myo", "event": {"confidence": 0.8}}).encode()
        for _ in range(5):  # myo-fall-detect repeats alerts over UDP
            bridge.datagram_received(fall, None)
        bridge.datagram_received(b"not json", None)
        await asyncio.sleep(0)
        assert agent.events == [("fall_detected", {"source": "myo_armband", "device": "Myo",
                                                   "detail": {"confidence": 0.8}})]
        assert bridge.status() == {"streaming": True, "battery": 77, "rate_hz": 50.0}

    asyncio.run(run())
