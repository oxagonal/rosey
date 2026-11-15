"""Streaming fall detector for a forearm-worn IMU (Myo, 50 Hz).

A fall seen from the forearm usually looks like this:

    [maybe] free fall      the body drops; |a| dips well below 1 g
    impact                 a sharp spike in |a| (hand, elbow or body hits the floor)
    tumbling               high angular rate around the impact
    new orientation        the arm ends at a different angle to gravity
    lying still            little motion for a few seconds afterwards

Everyday arm motion (clapping, slapping a table, jumping, sitting down hard)
can produce *some* of these, so a single threshold makes a poor detector. This
one is a small state machine:

1. **Trigger** on an impact: |a| >= ``impact_g``.
2. **Wait** ``settle_s + still_window_s`` seconds (further impacts in that time
   extend the event, e.g. knee then hand then hip).
3. **Require** that the wearer is still during ``still_window_s``.
4. **Require** at least ``min_supporting`` of three supporting cues:
   free fall before the impact, fast rotation around it, and a large change in
   the arm's angle to gravity between before and after.

Every evaluated candidate (accepted or not) is kept in ``evaluations`` with
the measured features and the reason, so recorded sessions can be replayed to
tune the thresholds for a particular wearer.

Pure Python, no numpy: it runs at 50 Hz on anything.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from itertools import islice
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]


@dataclass
class FallConfig:
    sample_rate_hz: float = 50.0

    # 1. impact. 2.0 g rather than 2.5 g: in the BITS-2 wrist dataset (real volunteer falls,
    # see tools/bits2_peaks.py) the softer fall types often peak below 2.5 g, and the Myo sits
    # further up the forearm than a wrist sensor, so hand impacts arrive softened. The checks
    # after the trigger (stillness + supporting cues) are what reject everyday impacts.
    impact_g: float = 2.0
    # further impacts within this long of the first one extend the same event
    max_event_s: float = 4.0

    # supporting cue: free fall shortly before the impact
    freefall_g: float = 0.6
    freefall_min_s: float = 0.08
    freefall_lookback_s: float = 1.0

    # supporting cue: fast rotation around the impact
    rotation_dps: float = 250.0
    rotation_window_s: float = 0.5

    # supporting cue: arm angle to gravity changed (before vs. after)
    pre_window_start_s: float = 2.0  # mean gravity taken from 2.0 s ...
    pre_window_end_s: float = 0.6  # ... to 0.6 s before the impact
    orientation_change_deg: float = 45.0

    # required: lying (mostly) still after the impact
    settle_s: float = 1.0
    still_window_s: float = 2.0
    still_std_g: float = 0.10  # std-dev of |a| over the window
    still_gyro_dps: float = 40.0  # mean |gyro| over the window

    # decision
    min_supporting: int = 2  # of: freefall, rotation, orientation
    refractory_s: float = 15.0  # no new alarms this long after one

    @classmethod
    def from_overrides(cls, overrides: Dict[str, str]) -> "FallConfig":
        """Build from ``{"impact_g": "3.0", ...}`` (e.g. CLI ``--set`` flags)."""
        types = {f.name: f.type for f in fields(cls)}
        kwargs = {}
        for key, raw in overrides.items():
            if key not in types:
                raise ValueError("unknown detector setting %r (valid: %s)" % (key, ", ".join(sorted(types))))
            kwargs[key] = int(raw) if types[key] in (int, "int") else float(raw)
        return cls(**kwargs)

    def samples(self, seconds: float) -> int:
        return max(1, int(round(seconds * self.sample_rate_hz)))


@dataclass
class FallEvent:
    t: float  # timestamp of the (first) impact
    confidence: float  # 0..1, a heuristic score, not a probability
    peak_g: float
    peak_gyro_dps: float
    freefall_s: float
    orientation_change_deg: Optional[float]
    still_std_g: float
    still_gyro_dps: float
    cues: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 3)
        return d


@dataclass
class Evaluation:
    """Diagnostics for one candidate, accepted or rejected."""

    t: float
    accepted: bool
    reason: str
    features: dict


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _angle_deg(a: Vec3, b: Vec3) -> Optional[float]:
    na, nb = _norm(a), _norm(b)
    if na < 1e-6 or nb < 1e-6:
        return None
    c = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _mean_vec(rows) -> Optional[Vec3]:
    n = 0
    sx = sy = sz = 0.0
    for r in rows:
        sx += r[1][0]
        sy += r[1][1]
        sz += r[1][2]
        n += 1
    return (sx / n, sy / n, sz / n) if n else None


class FallDetector:
    IDLE = "idle"
    CANDIDATE = "candidate"

    def __init__(self, config: Optional[FallConfig] = None, keep_evaluations: int = 100,
                 on_evaluation: Optional[Callable[[Evaluation], None]] = None):
        self.cfg = config or FallConfig()
        self.on_evaluation = on_evaluation
        c = self.cfg
        history_s = c.pre_window_start_s + c.max_event_s + c.settle_s + c.still_window_s + 1.0
        # rows: (t, accel, |a|, |gyro|)
        self._buf: Deque[tuple] = deque(maxlen=c.samples(history_s))
        self._n = 0  # total samples seen; index of the next sample
        self.evaluations: Deque[Evaluation] = deque(maxlen=keep_evaluations)
        self.reset()

    def reset(self) -> None:
        """Forget all history (call after a reconnect: there is a gap in the data)."""
        self._buf.clear()
        self.state = self.IDLE
        self._first_impact = -1
        self._last_impact = -1
        self._quiet_until = -1

    # -- buffer helpers ----------------------------------------------------
    def _rows(self, start: int, end: int) -> List[tuple]:
        """Rows with global sample index in [start, end)."""
        first = self._n - len(self._buf)
        lo = max(start, first) - first
        hi = min(end, self._n) - first
        if hi <= lo:
            return []
        return list(islice(self._buf, lo, hi))

    # -- main entry point ----------------------------------------------------
    def update(self, accel: Vec3, gyro: Vec3, t: float) -> Optional[FallEvent]:
        """Feed one sample (accel in g, gyro in deg/s). Returns a FallEvent when a fall is confirmed."""
        c = self.cfg
        a_mag = _norm(accel)
        self._buf.append((t, accel, a_mag, _norm(gyro)))
        i = self._n
        self._n += 1

        is_impact = a_mag >= c.impact_g
        if self.state == self.IDLE:
            if is_impact and i >= self._quiet_until:
                self.state = self.CANDIDATE
                self._first_impact = self._last_impact = i
            return None

        # CANDIDATE: a later impact extends the event (or starts a fresh one if it's been too long)
        if is_impact:
            if i - self._first_impact <= c.samples(c.max_event_s):
                self._last_impact = i
            else:
                self._first_impact = self._last_impact = i

        if i < self._last_impact + c.samples(c.settle_s + c.still_window_s) - 1:
            return None

        self.state = self.IDLE
        event = self._evaluate()
        if event is not None:
            self._quiet_until = i + c.samples(c.refractory_s)
        return event

    def _evaluate(self) -> Optional[FallEvent]:
        c = self.cfg
        f0, f1 = self._first_impact, self._last_impact
        t0 = self._rows(f0, f0 + 1)[0][0]

        # peak impact
        impact_rows = self._rows(f0, f1 + c.samples(0.3))
        peak_g = max(r[2] for r in impact_rows)

        # free fall: longest run of |a| < freefall_g before the (last) impact
        longest = run = 0
        for r in self._rows(f0 - c.samples(c.freefall_lookback_s), f1):
            run = run + 1 if r[2] < c.freefall_g else 0
            longest = max(longest, run)
        freefall_s = longest / c.sample_rate_hz

        # rotation around the impact(s)
        rw = c.samples(c.rotation_window_s)
        peak_gyro = max(r[3] for r in self._rows(f0 - rw, f1 + rw + 1))

        # stillness after the last impact
        s0 = f1 + c.samples(c.settle_s)
        still = self._rows(s0, s0 + c.samples(c.still_window_s))
        mags = [r[2] for r in still]
        mean_mag = sum(mags) / len(mags)
        still_std = math.sqrt(sum((m - mean_mag) ** 2 for m in mags) / len(mags))
        still_gyro = sum(r[3] for r in still) / len(still)

        # orientation change: mean accel (≈ gravity) before vs. after
        pre = _mean_vec(self._rows(f0 - c.samples(c.pre_window_start_s), f0 - c.samples(c.pre_window_end_s)))
        post = _mean_vec(still)
        orientation_change = _angle_deg(pre, post) if pre and post else None

        cues = []
        if freefall_s >= c.freefall_min_s:
            cues.append("freefall")
        if peak_gyro >= c.rotation_dps:
            cues.append("rotation")
        if orientation_change is not None and orientation_change >= c.orientation_change_deg:
            cues.append("orientation")
        is_still = still_std <= c.still_std_g and still_gyro <= c.still_gyro_dps

        features = {
            "peak_g": round(peak_g, 2),
            "peak_gyro_dps": round(peak_gyro, 1),
            "freefall_s": round(freefall_s, 3),
            "orientation_change_deg": None if orientation_change is None else round(orientation_change, 1),
            "still_std_g": round(still_std, 3),
            "still_gyro_dps": round(still_gyro, 1),
            "cues": cues,
        }
        if not is_still:
            reason = "moving after impact"
        elif len(cues) < c.min_supporting:
            reason = "only %d supporting cue(s) %s, need %d" % (len(cues), cues, c.min_supporting)
        else:
            reason = "fall"
        accepted = reason == "fall"
        evaluation = Evaluation(t=t0, accepted=accepted, reason=reason, features=features)
        self.evaluations.append(evaluation)
        if self.on_evaluation is not None:
            self.on_evaluation(evaluation)
        if not accepted:
            return None

        confidence = 0.4 + 0.2 * len(cues)
        confidence *= min(1.0, peak_g / (c.impact_g * 1.5)) ** 0.25  # weak impacts score a bit lower
        return FallEvent(
            t=t0,
            confidence=min(1.0, confidence),
            peak_g=peak_g,
            peak_gyro_dps=peak_gyro,
            freefall_s=freefall_s,
            orientation_change_deg=orientation_change,
            still_std_g=still_std,
            still_gyro_dps=still_gyro,
            cues=cues,
        )
