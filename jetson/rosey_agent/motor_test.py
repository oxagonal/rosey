"""Check the motor wiring before driving remotely:  python -m rosey_agent.motor_test

Put Rosey on a box with the wheels off the ground first. Each step prints what
should happen; if a wheel spins backwards, re-run the agent with --invert-left
or --invert-right (or swap that motor's two wires at the L293D).
"""

from __future__ import annotations

import argparse
import logging
import time

from .motors import Pins, make_motors

STEPS = [
    ("LEFT wheel FORWARD", 1, 0),
    ("LEFT wheel BACKWARD", -1, 0),
    ("RIGHT wheel FORWARD", 0, 1),
    ("RIGHT wheel BACKWARD", 0, -1),
    ("BOTH FORWARD (robot drives straight ahead)", 1, 1),
    ("SPIN LEFT (left back, right forward)", -1, 1),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speed", type=float, default=0.6)
    ap.add_argument("--seconds", type=float, default=1.5)
    ap.add_argument("--no-pwm", action="store_true")
    ap.add_argument("--min-duty", type=float, default=35)
    ap.add_argument("--invert-left", action="store_true")
    ap.add_argument("--invert-right", action="store_true")
    ap.add_argument("--mock", action="store_true", help="dry run without GPIO")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    motors = make_motors(mock=args.mock, pins=Pins(), use_pwm=not args.no_pwm, min_duty=args.min_duty,
                         invert_left=args.invert_left, invert_right=args.invert_right)
    try:
        for label, left, right in STEPS:
            print("\n>>> %s" % label)
            motors.set(left * args.speed, right * args.speed)
            time.sleep(args.seconds)
            motors.stop()
            time.sleep(0.7)
        print("\nRamp test: both wheels from 0 to full. Note the lowest speed where they start turning;")
        print("if they only start late, lower --min-duty; if they hum without moving at the start, raise it.")
        for pct in range(0, 101, 10):
            print("  speed %3d%%" % pct)
            motors.set(pct / 100, pct / 100)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        motors.close()
        print("Motors stopped.")


if __name__ == "__main__":
    main()
