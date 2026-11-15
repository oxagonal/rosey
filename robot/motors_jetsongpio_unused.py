"""Motor driver for the Rosey car: Jetson Orin Nano + L293D, two TT motors.

Wiring (Jetson physical BOARD pin -> L293D pin):
  15 -> EN1,2 (1)   Motor A (left) enable
  29 -> 1A    (2)   Motor A input 1 (1Y -> red)
  31 -> 2A    (7)   Motor A input 2 (2Y -> black)
  33 -> EN3,4 (9)   Motor B (right) enable
  35 -> 3A   (10)   Motor B input 1 (3Y -> red)
  37 -> 4A   (15)   Motor B input 2 (4Y -> black)
  Pin 2 (5V) -> VCC1 (16), pin 6 GND -> common ground,
  battery 12V -> buck 6V -> VCC2 (8).
"""
import os
import time

# The stock Jetson.GPIO 2.1.7 does not recognise the "Super" dev kit's
# device-tree string, so tell it the model explicitly.
os.environ.setdefault("JETSON_MODEL_NAME", "JETSON_ORIN_NANO")
import Jetson.GPIO as GPIO  # noqa: E402

LEFT = {"en": 15, "a": 29, "b": 31}
RIGHT = {"en": 33, "a": 35, "b": 37}
ALL_PINS = [p for m in (LEFT, RIGHT) for p in m.values()]


class Car:
    def __init__(self, invert_left=False, invert_right=False):
        self.invert = {"left": invert_left, "right": invert_right}
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(ALL_PINS, GPIO.OUT, initial=GPIO.LOW)

    def _motor(self, m, direction, inverted):
        """direction: 1 forward, -1 reverse, 0 coast."""
        if inverted:
            direction = -direction
        if direction == 0:
            GPIO.output([m["en"], m["a"], m["b"]], GPIO.LOW)
            return
        GPIO.output(m["a"], GPIO.HIGH if direction > 0 else GPIO.LOW)
        GPIO.output(m["b"], GPIO.LOW if direction > 0 else GPIO.HIGH)
        GPIO.output(m["en"], GPIO.HIGH)

    def drive(self, left, right):
        self._motor(LEFT, left, self.invert["left"])
        self._motor(RIGHT, right, self.invert["right"])

    def forward(self):  self.drive(1, 1)
    def backward(self): self.drive(-1, -1)
    def left(self):     self.drive(-1, 1)   # spin left in place
    def right(self):    self.drive(1, -1)   # spin right in place
    def stop(self):     self.drive(0, 0)

    def brake(self):
        """Active brake: enable on, both inputs equal."""
        GPIO.output([LEFT["a"], LEFT["b"], RIGHT["a"], RIGHT["b"]], GPIO.LOW)
        GPIO.output([LEFT["en"], RIGHT["en"]], GPIO.HIGH)

    def cleanup(self):
        self.stop()
        GPIO.cleanup(ALL_PINS)


if __name__ == "__main__":
    car = Car()
    try:
        for name, action in [("forward", car.forward), ("backward", car.backward),
                             ("spin left", car.left), ("spin right", car.right)]:
            print(name, flush=True)
            action()
            time.sleep(1.0)
            car.stop()
            time.sleep(0.5)
        print("done")
    finally:
        car.cleanup()
