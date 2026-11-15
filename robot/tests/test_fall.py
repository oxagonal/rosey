import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
np = pytest.importorskip("numpy")
import rosey_fall as rf
from rosey_detect import Detection

IMG = np.zeros((360, 640, 3), np.uint8)
LYING = Detection(0, 0.8, (150, 250, 450, 340))
STANDING = Detection(0, 0.8, (280, 20, 360, 350))


def test_lying_shape():
    assert rf.lying_box(LYING.box, 640, 360)
    assert not rf.lying_box(STANDING.box, 640, 360)
    assert not rf.lying_box((0, 100, 640, 300), 640, 360)        # close-up torso filling the width


def test_needs_persistence_then_confirms_once():
    fw = rf.FallWatch()
    assert not any(fw.observe(IMG, [LYING], now=t * 0.5) for t in range(3))
    assert fw.observe(IMG, [LYING], now=1.5)
    fw._ask = lambda jpg: {"fallen": True, "confidence": 0.9, "description": "x"}
    assert fw.confirm(b"", now=1.6)["fallen"]
    assert not any(fw.observe(IMG, [LYING], now=2 + t * 0.5) for t in range(20))   # quiet after alert


def test_standing_people_never_trigger():
    fw = rf.FallWatch()
    assert not any(fw.observe(IMG, [STANDING], now=t * 0.5) for t in range(40))


def test_claude_says_no_suppresses_rechecks():
    fw = rf.FallWatch()
    fw._ask = lambda jpg: {"fallen": False, "confidence": 0.9}
    for t in range(4):
        fw.observe(IMG, [LYING], now=t * 0.5)
    assert not fw.confirm(b"", now=2)["fallen"]
    assert not any(fw.observe(IMG, [LYING], now=2.5 + t * 0.5) for t in range(40))


def test_unreachable_claude_still_alerts():
    fw = rf.FallWatch()
    def boom(jpg):
        raise ConnectionError()
    fw._ask = boom
    v = fw.confirm(b"", now=0)
    assert v["fallen"] and v["unconfirmed"]
