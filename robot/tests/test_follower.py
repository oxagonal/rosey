"""Follower logic on synthetic frames."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
import rosey_follow as rf
from rosey_detect import Detection

W, H = 640, 360


def frame(seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (H, W, 3), dtype=np.uint8)


def person(x1, x2, y1=40, y2=350, score=0.8):
    return Detection(0, score, (x1, y1, x2, y2))


def paint(img, box, color):
    x1, y1, x2, y2 = map(int, box)
    img[y1:y2, x1:x2] = color
    return img


def test_searching_without_people():
    out = rf.Follower().step(frame(), [], now=0)
    assert out.state == "searching" and not out.moving


def test_centered_far_person_drives_forward_never_backward():
    img = paint(frame(), (290, 40, 350, 350), (0, 0, 200))
    out = rf.Follower().step(img, [person(290, 350)], now=0)
    assert out.state == "following" and out.left > 0 and out.right > 0
    assert out.left >= rf.MIN_WHEEL and out.right >= rf.MIN_WHEEL


def test_close_person_stops():
    img = paint(frame(), (150, 40, 490, 350), (0, 0, 200))
    out = rf.Follower().step(img, [person(150, 490)], now=0)
    assert out.state == "arrived" and not out.moving


def test_off_centre_turns_in_place_toward_person():
    img = paint(frame(), (560, 40, 620, 350), (0, 0, 200))
    out = rf.Follower().step(img, [person(560, 620)], now=0)
    assert out.pulse_s > 0 and out.left == -out.right
    # person on the right; with TURN_INVERT the robot's "turn right" is drive(-, +)
    assert (out.left < 0) == rf.TURN_INVERT


def test_obstacle_in_path_blocks_forward():
    img = paint(frame(), (290, 40, 350, 350), (0, 0, 200))
    chair = Detection(56, 0.7, (220, 200, 420, 359))
    out = rf.Follower().step(img, [person(290, 350), chair], now=0)
    assert out.state == "blocked" and not out.moving and out.obstacles


def test_keeps_original_person_not_bigger_stranger():
    fol = rf.Follower()
    img = paint(frame(1), (290, 40, 350, 350), (0, 0, 200))           # red shirt
    fol.step(img, [person(290, 350)], now=0)
    img = paint(frame(2), (300, 40, 360, 350), (0, 0, 200))
    img = paint(img, (40, 40, 200, 350), (200, 120, 0))                # bigger, blue stranger
    out = fol.step(img, [person(300, 360), person(40, 200)], now=0.1)
    assert out.target[0] == 300


def test_lost_person_stops():
    fol = rf.Follower()
    img = paint(frame(), (290, 40, 350, 350), (0, 0, 200))
    fol.step(img, [person(290, 350)], now=0)
    out = fol.step(frame(3), [], now=1)
    assert out.state == "lost" and not out.moving


def test_stuck_when_view_frozen():
    fol = rf.Follower()
    img = paint(frame(), (290, 40, 350, 350), (0, 0, 200))
    states = [fol.step(img.copy(), [person(290, 350)], now=t / 10).state for t in range(0, 30)]
    assert "stuck" in states


def test_very_close_person_backs_up_in_short_bursts():
    img = paint(frame(), (100, 40, 540, 350), (0, 0, 200))        # 69% of the width
    out = rf.Follower().step(img, [person(100, 540)], now=0)
    assert out.left < 0 and out.right < 0 and out.left == out.right
    assert abs(out.left) <= 1.0 and 0 < out.pulse_s <= 0.5


def test_backing_stops_once_they_step_back():
    fol = rf.Follower()
    img = paint(frame(), (100, 40, 540, 350), (0, 0, 200))
    fol.step(img, [person(100, 540)], now=0)
    # 45%: still close (no forward), but below BACK_DONE_W: stop backing
    img2 = paint(frame(), (176, 40, 464, 350), (0, 0, 200))
    out = fol.step(img2, [person(176, 464)], now=0.5)
    assert out.state == "arrived" and not out.moving


def test_backing_is_limited_because_it_cannot_see_behind():
    fol = rf.Follower()
    img = paint(frame(), (100, 40, 540, 350), (0, 0, 200))
    outs = [fol.step(img, [person(100, 540)], now=i * 0.5) for i in range(rf.BACK_MAX_PULSES + 3)]
    backs = [o for o in outs if o.left < 0]
    assert len(backs) == rf.BACK_MAX_PULSES
    assert not outs[-1].moving
