"""Synthetic forearm IMU traces for exercising the detector without hardware.

These are hand-built approximations (pendulum arm swing, free fall as reduced
gravity, impacts as short spikes). They are good for checking that the state
machine behaves as intended and for trying the pipeline end to end. They say
nothing about real-world accuracy; only recordings of real people do that.

Sensor frame (approximate Myo convention): x runs along the forearm toward the
hand, so with the arm hanging at the side the accelerometer reads (-1, 0, 0) g.
``theta`` is the arm's pitch away from hanging and ``roll`` its twist.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .myo_ble import ImuSample

G = 9.81
ARM_RADIUS_M = 0.45  # shoulder to sensor


def _smooth(u: float) -> float:
    """Cosine ease from 0 to 1."""
    return 0.5 - 0.5 * math.cos(math.pi * min(max(u, 0.0), 1.0))


class Trace:
    def __init__(self, rate_hz: float = 50.0, seed: int = 0, t0: float = 1_700_000_000.0):
        self.dt = 1.0 / rate_hz
        self.rng = random.Random(seed)
        self.t = t0
        self.theta = 0.0
        self.roll = 0.0
        self.samples: List[ImuSample] = []

    # -- primitive -----------------------------------------------------------
    def _emit(self, theta: float, roll: float, dyn: Sequence[float] = (0.0, 0.0, 0.0), gscale: float = 1.0,
              spin: Sequence[float] = (0.0, 0.0, 0.0), noise_g: float = 0.012, noise_dps: float = 1.5) -> None:
        rng = self.rng
        dtheta = (theta - self.theta) / self.dt
        droll = (roll - self.roll) / self.dt
        self.theta, self.roll = theta, roll
        s = math.sin(theta)
        g = (-math.cos(theta), s * math.sin(roll), s * math.cos(roll))
        accel = tuple(gscale * g[k] + dyn[k] + rng.gauss(0.0, noise_g) for k in range(3))
        gyro = (math.degrees(droll) + spin[0] + rng.gauss(0.0, noise_dps),
                math.degrees(dtheta) + spin[1] + rng.gauss(0.0, noise_dps),
                spin[2] + rng.gauss(0.0, noise_dps))
        quat = (math.cos(theta / 2), 0.0, math.sin(theta / 2), 0.0)
        self.samples.append(ImuSample(self.t, quat, accel, gyro))  # type: ignore[arg-type]
        self.t += self.dt

    def _n(self, seconds: float) -> int:
        return max(1, int(round(seconds / self.dt)))

    # -- activities ----------------------------------------------------------
    def still(self, seconds: float, theta: float = None, roll: float = None, tremor_dps: float = 2.0) -> "Trace":
        theta = self.theta if theta is None else theta
        roll = self.roll if roll is None else roll
        for _ in range(self._n(seconds)):
            self._emit(theta, roll, noise_dps=tremor_dps)
        return self

    def walk(self, seconds: float, swing_deg: float = 25.0, stride_hz: float = 0.9, heel_g: float = 0.3) -> "Trace":
        base, A, w = self.theta, math.radians(swing_deg), 2 * math.pi * stride_hz
        for k in range(self._n(seconds)):
            tt = k * self.dt
            th = base + A * math.sin(w * tt)
            th_d = A * w * math.cos(w * tt)
            th_dd = -A * w * w * math.sin(w * tt)
            heel = heel_g * max(0.0, math.sin(2 * w * tt)) ** 8
            dyn = (-ARM_RADIUS_M * th_d ** 2 / G - heel, 0.0, ARM_RADIUS_M * th_dd / G)
            self._emit(th, self.roll, dyn=dyn)
        return self

    def move_to(self, theta: float, seconds: float, roll: float = None, gscale: float = 1.0,
                flail_dps: float = 0.0, flail_g: float = 0.0) -> "Trace":
        th0, r0 = self.theta, self.roll
        roll = r0 if roll is None else roll
        n = self._n(seconds)
        for k in range(1, n + 1):
            u = _smooth(k / n)
            wob = math.sin(math.pi * k / n)
            spin = (0.0, 0.0, flail_dps * wob * math.sin(7.0 * k / n))
            dyn = (0.0, flail_g * wob * math.sin(5.0 * k / n), flail_g * wob * math.cos(3.0 * k / n))
            self._emit(th0 + (theta - th0) * u, r0 + (roll - r0) * u, dyn=dyn, gscale=gscale, spin=spin)
        return self

    def freefall(self, seconds: float, theta: float, roll: float = None, residual_g: float = 0.25,
                 spin_dps: float = 200.0) -> "Trace":
        return self.move_to(theta, seconds, roll=roll, gscale=residual_g, flail_dps=spin_dps)

    def impact(self, peak_g: float, direction: Optional[Tuple[float, float, float]] = None,
               spin_dps: float = 0.0, bounce_s: float = 0.3) -> "Trace":
        """Short spike. By default it points "up" (the floor pushes back against gravity)."""
        if direction is None:
            s = math.sin(self.theta)
            direction = (-math.cos(self.theta) + 0.2, s * math.sin(self.roll) + 0.2, s * math.cos(self.roll))
        n = math.sqrt(sum(d * d for d in direction))
        d = [x / n for x in direction]
        for f in (0.45, 1.0, 0.5):
            self._emit(self.theta, self.roll, dyn=[peak_g * f * x for x in d], spin=(0.0, 0.0, spin_dps * f))
        steps = self._n(bounce_s)
        for k in range(steps):
            decay = math.exp(-5.0 * k / steps)
            osc = 0.4 * decay * math.sin(2 * math.pi * 6 * k * self.dt)
            self._emit(self.theta, self.roll, dyn=[osc * x for x in d], spin=(0.0, 0.0, spin_dps * 0.3 * decay))
        return self


# -- scenarios: name -> (builder, contains_a_fall) ---------------------------

def fall_forward_trip(seed: int = 1) -> List[ImuSample]:
    tr = Trace(seed=seed).still(3).walk(6)
    tr.move_to(1.0, 0.15, roll=0.3, flail_dps=150)  # stumble, arm thrown forward
    tr.freefall(0.3, theta=1.5, roll=0.6, spin_dps=250)
    tr.impact(5.5, spin_dps=200)
    return tr.still(10, tremor_dps=3).samples


def fall_backward_slip(seed: int = 2) -> List[ImuSample]:
    # No clean free fall: the arm swings back and hits the floor.
    tr = Trace(seed=seed).still(3).walk(6)
    tr.move_to(2.0, 0.4, roll=-0.5, flail_dps=120, flail_g=0.3)
    tr.impact(3.2, spin_dps=150)
    return tr.still(10, tremor_dps=3).samples


def fall_sideways(seed: int = 3) -> List[ImuSample]:
    tr = Trace(seed=seed).still(5)
    tr.move_to(0.4, 0.3, roll=0.8)
    tr.freefall(0.25, theta=1.4, roll=1.4, spin_dps=180)
    tr.impact(4.0, direction=(0.1, 0.9, 0.3), spin_dps=100)
    return tr.still(10, tremor_dps=2).samples


def fall_from_chair(seed: int = 4) -> List[ImuSample]:
    tr = Trace(seed=seed)
    tr.theta = 1.3  # forearm resting on lap
    tr.still(5)
    tr.freefall(0.2, theta=0.7, residual_g=0.5, spin_dps=60)
    tr.move_to(0.4, 0.3)
    tr.impact(2.8, direction=(-0.6, 0.2, 0.7))
    return tr.still(10, tremor_dps=2).samples


def adl_walking(seed: int = 10) -> List[ImuSample]:
    return Trace(seed=seed).still(2).walk(60).still(3).samples


def adl_sit_down(seed: int = 11) -> List[ImuSample]:
    tr = Trace(seed=seed).still(2).walk(5)
    tr.move_to(1.3, 1.2, flail_g=0.1)
    tr.impact(0.8)  # landing in the chair: ~1.8 g total at the wrist
    return tr.still(10).samples


def adl_lie_down(seed: int = 12) -> List[ImuSample]:
    tr = Trace(seed=seed).still(2).walk(5)
    tr.move_to(1.2, 1.0).impact(0.6).still(2)  # sit on the bed
    tr.move_to(1.6, 2.0, roll=1.2)  # lie back
    return tr.still(15).samples


def adl_table_slap(seed: int = 13) -> List[ImuSample]:
    tr = Trace(seed=seed)
    tr.theta = 1.4  # forearm on a table
    tr.still(6)
    tr.move_to(2.2, 0.6)  # raise the hand
    tr.move_to(1.4, 0.15, gscale=0.75)  # slam it down
    tr.impact(6.0, direction=(0.0, 0.1, 1.0), spin_dps=100)
    return tr.still(10).samples


def adl_clapping(seed: int = 14) -> List[ImuSample]:
    tr = Trace(seed=seed).still(3)
    tr.move_to(1.4, 1.0).still(2)  # raise hands in front
    for _ in range(6):
        tr.move_to(1.6, 0.2).move_to(1.4, 0.1)
        tr.impact(3.5, direction=(0.0, 1.0, 0.1), bounce_s=0.1)
    tr.move_to(0.0, 0.8)  # lower arms
    return tr.still(10).samples


def adl_jump(seed: int = 15) -> List[ImuSample]:
    tr = Trace(seed=seed).still(5)
    tr.move_to(0.3, 0.4, gscale=1.3)  # crouch / push off
    tr.freefall(0.35, theta=0.1, residual_g=0.05, spin_dps=40)
    tr.impact(4.5, direction=(-1.0, 0.0, 0.1))
    return tr.still(10).samples


def fall_then_gets_up(seed: int = 20) -> List[ImuSample]:
    """A real fall, but the wearer is moving again within a second (not reported by design)."""
    tr = Trace(seed=seed).still(3).walk(6)
    tr.freefall(0.3, theta=1.5, roll=0.6, spin_dps=250)
    tr.impact(5.0, spin_dps=200)
    tr.move_to(0.8, 0.8, flail_g=0.4, flail_dps=150)
    return tr.walk(10).samples


SCENARIOS: Dict[str, Tuple[Callable[[], List[ImuSample]], bool]] = {
    "fall_forward_trip": (fall_forward_trip, True),
    "fall_backward_slip": (fall_backward_slip, True),
    "fall_sideways": (fall_sideways, True),
    "fall_from_chair": (fall_from_chair, True),
    "adl_walking": (adl_walking, False),
    "adl_sit_down": (adl_sit_down, False),
    "adl_lie_down": (adl_lie_down, False),
    "adl_table_slap": (adl_table_slap, False),
    "adl_clapping": (adl_clapping, False),
    "adl_jump": (adl_jump, False),
}
