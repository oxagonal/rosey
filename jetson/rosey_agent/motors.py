"""L293D dual H-bridge driver for Rosey's two TT motors.

Pin map (Jetson 40-pin header, BOARD numbering), from the wiring schematic:

    L293D pin        Jetson pin   Function
    1  EN1,2    <--  15 (PWM)     left motor speed
    2  1A       <--  29           left motor direction
    7  2A       <--  31           left motor direction
    9  EN3,4    <--  33 (PWM)     right motor speed
    10 3A       <--  35           right motor direction
    15 4A       <--  37           right motor direction
    3/6  1Y/2Y  -->  Motor A (left):  red on 1Y, black on 2Y
    11/14 3Y/4Y -->  Motor B (right): red on 3Y, black on 4Y
    16 VCC1     <--  Jetson pin 2 (5 V logic)    8 VCC2 <-- 6 V from the buck converter

Pins 15 and 33 are the Orin Nano's hardware-PWM pins, but they only output PWM
once the pinmux is configured (sudo /opt/nvidia/jetson-io/jetson-io.py, enable
PWM on pins 15 and 33, reboot). If PWM can't be set up the driver falls back
to on/off control (full speed only).

Speed convention: +1 = full forward, -1 = full reverse, 0 = stop (coast).
If a wheel spins the wrong way, use invert_left / invert_right (TT motors on
opposite sides of the chassis are mirror images).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger("rosey.motors")


@dataclass(frozen=True)
class Pins:
    left_en: int = 15
    left_a: int = 29  # 1A
    left_b: int = 31  # 2A
    right_en: int = 33
    right_a: int = 35  # 3A
    right_b: int = 37  # 4A

    def all(self) -> List[int]:
        return [self.left_en, self.left_a, self.left_b, self.right_en, self.right_a, self.right_b]


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def mix(linear: float, angular: float) -> Tuple[float, float]:
    """Forward speed + turn rate (positive = left) -> (left, right) wheel speeds."""
    left, right = linear - angular, linear + angular
    scale = max(1.0, abs(left), abs(right))
    return left / scale, right / scale


def speed_to_duty(speed: float, min_duty: float, max_duty: float, deadband: float = 0.03) -> float:
    """Map |speed| in 0..1 to a PWM duty in %, skipping the range where TT motors just hum."""
    s = abs(speed)
    if s < deadband:
        return 0.0
    return min_duty + (max_duty - min_duty) * min(s, 1.0)


class MockMotors:
    """Stands in for the hardware (for development on a laptop, and tests)."""

    def __init__(self, max_speed: float = 1.0, **_ignored):
        self.max_speed = max_speed
        self.left = self.right = 0.0
        self.history: List[Tuple[float, float]] = []

    def set(self, left: float, right: float) -> None:
        left, right = clamp(left) * self.max_speed, clamp(right) * self.max_speed
        if (left, right) != (self.left, self.right):
            log.info("motors (mock): left=%+.2f right=%+.2f", left, right)
        self.left, self.right = left, right
        self.history.append((left, right))

    def stop(self) -> None:
        self.set(0.0, 0.0)

    @property
    def moving(self) -> bool:
        return self.left != 0.0 or self.right != 0.0

    def close(self) -> None:
        self.stop()


class L293DMotors:
    def __init__(self, pins: Pins = Pins(), use_pwm: bool = True, pwm_hz: int = 1000,
                 min_duty: float = 35.0, max_duty: float = 100.0, max_speed: float = 1.0,
                 invert_left: bool = False, invert_right: bool = False, gpio=None):
        if gpio is None:
            import Jetson.GPIO as gpio  # ships with JetPack; `pip install Jetson.GPIO` otherwise
        self.GPIO = gpio
        self.pins = pins
        self.min_duty, self.max_duty = min_duty, max_duty
        self.max_speed = max_speed
        self.invert_left, self.invert_right = invert_left, invert_right
        self.left = self.right = 0.0

        gpio.setwarnings(False)
        gpio.setmode(gpio.BOARD)
        # Everything low first: both enables off = motors coast.
        gpio.setup(pins.all(), gpio.OUT, initial=gpio.LOW)

        self._dir = {pins.left_en: 0, pins.right_en: 0}
        self._pwm = {}
        if use_pwm:
            try:
                for pin in (pins.left_en, pins.right_en):
                    pwm = gpio.PWM(pin, pwm_hz)
                    pwm.start(0)
                    self._pwm[pin] = pwm
            except Exception as exc:
                for pwm in self._pwm.values():
                    pwm.stop()
                self._pwm = {}
                log.error("Hardware PWM unavailable on pins %d/%d (%s). Falling back to on/off "
                          "control (full speed only). Enable PWM with jetson-io and reboot.",
                          pins.left_en, pins.right_en, exc)
        log.info("L293D ready (pwm=%s, min_duty=%.0f%%, max_speed=%.2f)", bool(self._pwm), min_duty, max_speed)

    def _enable(self, en: int, duty: float) -> None:
        if self._pwm:
            self._pwm[en].ChangeDutyCycle(duty)
        else:
            self.GPIO.output(en, self.GPIO.HIGH if duty > 0 else self.GPIO.LOW)

    def _apply(self, en: int, a: int, b: int, speed: float) -> None:
        G = self.GPIO
        duty = speed_to_duty(speed, self.min_duty, self.max_duty)
        direction = 0 if duty == 0.0 else (1 if speed > 0 else -1)
        if direction == self._dir.get(en):
            self._enable(en, duty)  # same direction: just change speed
            return
        # Direction change: enable off, set direction, enable on (never drive the old direction briefly).
        self._enable(en, 0.0)
        G.output(a, G.HIGH if direction > 0 else G.LOW)
        G.output(b, G.HIGH if direction < 0 else G.LOW)
        if direction:
            self._enable(en, duty)
        self._dir[en] = direction

    def set(self, left: float, right: float) -> None:
        left, right = clamp(left) * self.max_speed, clamp(right) * self.max_speed
        if (left, right) == (self.left, self.right):
            return
        p = self.pins
        self._apply(p.left_en, p.left_a, p.left_b, -left if self.invert_left else left)
        self._apply(p.right_en, p.right_a, p.right_b, -right if self.invert_right else right)
        self.left, self.right = left, right

    def stop(self) -> None:
        self.set(0.0, 0.0)

    @property
    def moving(self) -> bool:
        return self.left != 0.0 or self.right != 0.0

    def close(self) -> None:
        try:
            self.stop()
            for pwm in self._pwm.values():
                pwm.stop()
        finally:
            self.GPIO.cleanup(self.pins.all())


def make_motors(mock: bool = False, **kwargs):
    if mock:
        return MockMotors(**kwargs)
    return L293DMotors(**kwargs)
