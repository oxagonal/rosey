"""Person following + camera-based obstacle avoidance for Rosey (pure logic, no I/O).

    fol = Follower()
    out = fol.step(frame_bgr, detections, now)   # detections from rosey_detect.Detector
    out.left, out.right   wheel speeds -1..1 (already scaled by max_speed)
    out.pulse_s           >0: turn in place for this long, then stop and look again
    out.state             "searching" / "following" / "arrived" / "lost" / "blocked" / "stuck"

Following: locks onto the closest (largest) person when switched on, then keeps
tracking that same person by position + clothing colour, so it doesn't jump to
someone else in a crowded room. If they're lost it stops and waits.

Obstacle avoidance (camera only, so best-effort):
  * detected objects/other people that are close (low in the frame or big) and in
    the robot's path block forward driving; ones off to a side steer it away;
  * "stuck" check: driving forward but the floor area of the image isn't changing
    means the wheels are pushing against something, so it stops for a few seconds;
  * it never drives backwards and stops when the person is close.

Conventions: frame x grows to the right. TURN_INVERT matches follow.py (on this
robot drive(+L, -R) turns away from the person, found in a live test).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

PERSON = 0

# -- tuning --------------------------------------------------------------------
TURN_INVERT = True
DEADBAND = 0.15            # |offset| below this = centred (fraction of half-width)
TURN_IN_PLACE = 0.45       # |offset| above this: stop and turn in place first
TURN_SPEED = 1.0           # turning in place needs full power (TT motors stall below that on carpet)
PULSE_MIN, PULSE_MAX = 0.12, 0.28   # long enough to get the wheels past static friction
STEER_GAIN = 0.6           # how hard to steer while driving forward
MIN_WHEEL = 0.70           # below this the TT motors hum or crawl (manual 100% works best)
ARRIVE_W = 0.34            # person box width (fraction of frame) at which we stop
GO_W = 0.26                # ... and start following again (hysteresis)
LOST_AFTER_S = 0.6         # no match for this long -> stop
MATCH_SIM = 0.55           # clothing-colour similarity needed to re-find the person
OBSTACLE_SCORE = 0.40
STUCK_AFTER_S = 1.5        # forward with a frozen floor view for this long = stuck
STUCK_DIFF = 2.0           # mean abs grey difference (0-255) that counts as "moving"
STUCK_PAUSE_S = 3.0

Box = Tuple[float, float, float, float]


@dataclass
class Output:
    left: float = 0.0
    right: float = 0.0
    pulse_s: float = 0.0
    state: str = "searching"
    detail: str = ""
    target: Optional[Box] = None
    obstacles: List[Box] = field(default_factory=list)

    @property
    def moving(self) -> bool:
        return self.left != 0.0 or self.right != 0.0


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def clothing_hist(img: np.ndarray, box: Box) -> Optional[np.ndarray]:
    """Hue/saturation histogram of the middle of the box (torso / legs)."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    xa, xb = int(x1 + 0.2 * w), int(x2 - 0.2 * w)
    ya, yb = int(y1 + 0.2 * h), int(y1 + 0.8 * h)
    patch = img[max(0, ya):max(0, yb), max(0, xa):max(0, xb)]
    if patch.size < 300:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    return cv2.normalize(hist, None).flatten()


def similarity(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    if a is None or b is None:
        return 0.0
    return float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))


class Follower:
    def __init__(self, max_speed: float = 1.0):
        self.max_speed = max_speed
        self.reset()

    def reset(self) -> None:
        self.box: Optional[Box] = None
        self.hist: Optional[np.ndarray] = None
        self.last_seen = 0.0
        self.arrived = False
        self.forward_since: Optional[float] = None
        self.stuck_until = 0.0
        self.prev_floor: Optional[np.ndarray] = None
        self.floor_moved_at = 0.0
        self.off = None               # smoothed horizontal offset of the person

    # -- target tracking ---------------------------------------------------------
    def _track(self, img, people, now) -> Optional[Box]:
        if not people:
            return None
        if self.box is None:
            # First lock: the closest person (largest box).
            best = max(people, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            self.box, self.hist, self.last_seen = best, clothing_hist(img, best), now
            return best
        w = img.shape[1]
        recent = now - self.last_seen < 1.0
        scored = []
        for b in people:
            sim = similarity(self.hist, clothing_hist(img, b))
            overlap = iou(self.box, b)
            shift = abs((b[0] + b[2]) - (self.box[0] + self.box[2])) / 2 / w
            ok = (recent and (overlap > 0.25 or (shift < 0.15 and sim > 0.3))) or sim > MATCH_SIM
            if ok:
                scored.append((sim + overlap - shift, b, sim))
        if not scored:
            return None
        _, best, sim = max(scored, key=lambda s: s[0])
        h = clothing_hist(img, best)
        if h is not None and sim > 0.5:            # slowly adapt to lighting changes
            self.hist = h if self.hist is None else 0.9 * self.hist + 0.1 * h
        self.box, self.last_seen = best, now
        return best

    # -- obstacles ----------------------------------------------------------------
    @staticmethod
    def _obstacles(dets, target, w, h) -> Tuple[List[Box], float, float]:
        """Return (boxes, block, side): block 0..1 = how much of the path ahead is
        covered by something close; side = -1..1 which way to steer to avoid it."""
        lane_l, lane_r = 0.3 * w, 0.7 * w
        boxes, block, push = [], 0.0, 0.0
        for d in dets:
            b = d.box
            if d.score < OBSTACLE_SCORE or (target is not None and iou(b, target) > 0.5):
                continue
            x1, y1, x2, y2 = b
            close = y2 > 0.8 * h or (x2 - x1) * (y2 - y1) > 0.12 * w * h
            if not close:
                continue
            boxes.append(b)
            cover = max(0.0, min(x2, lane_r) - max(x1, lane_l)) / (lane_r - lane_l)
            block = max(block, cover)
            cx = (x1 + x2) / 2 / w - 0.5               # -0.5 (left) .. 0.5 (right)
            if cover > 0 or abs(cx) < 0.35:
                push += -1.0 if cx > 0 else 1.0        # obstacle right -> steer left
        return boxes, block, max(-1.0, min(1.0, push))

    def _floor_changed(self, img, target, now) -> bool:
        """Did the lower part of the picture (minus the person) change since last frame?"""
        h, w = img.shape[:2]
        small = cv2.cvtColor(cv2.resize(img, (80, 45), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        floor = small[27:].astype(np.int16)
        mask = np.ones_like(floor, dtype=bool)
        if target is not None:
            xa, xb = int(target[0] / w * 80), int(np.ceil(target[2] / w * 80))
            mask[:, max(0, xa):min(80, xb)] = False
        prev, self.prev_floor = self.prev_floor, floor
        if prev is None or not mask.any():
            return True
        return float(np.abs(floor - prev)[mask].mean()) > STUCK_DIFF

    # -- main step ----------------------------------------------------------------
    def step(self, img: np.ndarray, dets, now: Optional[float] = None) -> Output:
        now = time.monotonic() if now is None else now
        h, w = img.shape[:2]
        people = [d.box for d in dets if d.cls == PERSON and d.score >= 0.4]
        target = self._track(img, people, now)
        others = [d for d in dets if not (target is not None and d.box == target)]
        obstacles, block, side = self._obstacles(others, target, w, h)
        floor_moved = self._floor_changed(img, target, now)
        out = Output(target=target, obstacles=obstacles)

        if target is None:
            self.forward_since = None
            if self.box is None:
                out.state, out.detail = "searching", "looking for a person"
            else:
                out.state = "lost"
                out.detail = "lost them %.0f s ago, waiting" % (now - self.last_seen)
            return out

        x1, y1, x2, y2 = target
        raw_off = ((x1 + x2) / 2 - w / 2) / (w / 2)    # -1 left .. +1 right
        # Smooth out detector jitter so the steering doesn't flick left/right every frame.
        off = raw_off if self.off is None else 0.5 * self.off + 0.5 * raw_off
        self.off = off
        width = (x2 - x1) / w
        if width >= ARRIVE_W:
            self.arrived = True
        elif width < GO_W:
            self.arrived = False

        turn = 1.0 if off > 0 else -1.0                 # +1 = person is to the right
        if TURN_INVERT:
            turn = -turn

        # 1) Far off-centre (or arrived but off-centre): stop and turn in place in a short pulse.
        if abs(off) > TURN_IN_PLACE or (self.arrived and abs(off) > DEADBAND):
            self.forward_since = None
            k = min(1.0, (abs(off) - DEADBAND) / (1 - DEADBAND))
            out.pulse_s = PULSE_MIN + (PULSE_MAX - PULSE_MIN) * k
            s = max(MIN_WHEEL, TURN_SPEED)   # turns always at full power; the speed slider only limits forward speed
            out.left, out.right = turn * s, -turn * s
            out.state, out.detail = "following", "turning toward them"
            return out

        if self.arrived:
            self.forward_since = None
            out.state, out.detail = "arrived", "close enough, waiting"
            return out

        # 2) Drive toward them, unless something is in the way.
        if now < self.stuck_until:
            out.state, out.detail = "stuck", "wheels seemed blocked, pausing"
            return out
        if block > 0.35:
            self.forward_since = None
            out.state, out.detail = "blocked", "something in the way"
            return out

        speed = self.max_speed * min(1.0, 0.8 + (ARRIVE_W - width) / ARRIVE_W)
        steer = STEER_GAIN * off * (1 if not TURN_INVERT else -1) + 0.35 * side * (1 if not TURN_INVERT else -1)
        if abs(off) < DEADBAND:
            steer = 0.35 * side * (1 if not TURN_INVERT else -1)
        left, right = speed * (1 + steer), speed * (1 - steer)
        top = max(abs(left), abs(right), 1e-6)
        if top > self.max_speed:
            left, right = left * self.max_speed / top, right * self.max_speed / top
        # Keep both wheels above stall speed and never reverse while following.
        left = max(MIN_WHEEL, min(1.0, left))
        right = max(MIN_WHEEL, min(1.0, right))

        # 3) Stuck check: forward for a while but the floor view isn't changing.
        if floor_moved:
            self.floor_moved_at = now
        if self.forward_since is None:
            self.forward_since = now
            self.floor_moved_at = now
        elif now - self.forward_since > STUCK_AFTER_S and now - self.floor_moved_at > STUCK_AFTER_S:
            self.stuck_until = now + STUCK_PAUSE_S
            self.forward_since = None
            out.state, out.detail = "stuck", "wheels seemed blocked, pausing"
            return out

        out.left, out.right = round(left, 3), round(right, 3)
        out.state = "following"
        out.detail = "steering around something" if side and block > 0 else "following"
        return out
