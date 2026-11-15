#!/usr/bin/env python3
"""Drive with the keyboard. Hold a key to move, release to stop.

  W  both motors forward
  S  both motors backward
  A  left motor only (forward)
  D  right motor only (forward)
  space  stop      Q  quit

Run in a real terminal:  python3 ~/motors/wasd.py
(After a reboot, run  sudo python3 ~/motors/pinmux_fix.py  first.)
"""
import select, sys, termios, time, tty
from motors import Motors

SPEED = 1.0
ACTIONS = {
    "w": (SPEED, SPEED),
    "s": (-SPEED, -SPEED),
    "a": (SPEED, 0),
    "d": (0, SPEED),
}
# Terminals report a held key as: press, ~0.5 s pause, then repeats every ~30 ms.
# So allow a long gap before the first repeat, a short one after.
FIRST_GAP = 0.6
REPEAT_GAP = 0.15


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    print(__doc__)
    with Motors() as m:
        try:
            tty.setcbreak(fd)
            current, last, repeating = None, 0.0, False
            while True:
                gap = REPEAT_GAP if repeating else FIRST_GAP
                r, _, _ = select.select([sys.stdin], [], [], 0.02)
                now = time.monotonic()
                if r:
                    k = sys.stdin.read(1).lower()
                    if k == "q":
                        break
                    if k in ACTIONS:
                        repeating = (k == current)
                        if k != current:
                            m.drive(*ACTIONS[k])
                            current = k
                            print(f"\r{k.upper()}   ", end="", flush=True)
                        last = now
                    elif k == " ":
                        m.stop(); current = None
                        print("\rstop", end="", flush=True)
                elif current and now - last > gap:
                    m.stop(); current = None; repeating = False
                    print("\r    ", end="", flush=True)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            print("\nstopped")


if __name__ == "__main__":
    main()
