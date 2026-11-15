#!/usr/bin/env python3
"""Drive two TT motors through an L293D from a Jetson Orin Nano header.

No dependencies: talks to /dev/gpiochip0 via the kernel GPIO v1 ioctl API.
Speed is software PWM on the enable pins (EN1,2 / EN3,4).

NOTE: on JetPack 6 the header pins boot tristated. Run `sudo python3
pinmux_fix.py` once per boot first (or install the systemd service).

CLI:
  python3 motors.py forward [speed] [seconds]
  python3 motors.py back    [speed] [seconds]
  python3 motors.py left    [speed] [seconds]   # spin in place
  python3 motors.py right   [speed] [seconds]
  python3 motors.py drive <left> <right> [seconds]   # -1.0 .. 1.0 each
  python3 motors.py stop
  python3 motors.py test

Library:
  from motors import Motors
  with Motors() as m:
      m.drive(0.6, 0.6); time.sleep(1); m.stop()
"""
import fcntl, os, struct, sys, threading, time

CHIP = "/dev/gpiochip0"

# gpiochip0 line offsets (BOARD pin in comment)
EN_A, IN1, IN2 = 85, 105, 106   # pin 15 PN.01, pin 29 PQ.05, pin 31 PQ.06  -> Motor A (left)
EN_B, IN3, IN4 = 43, 53, 124    # pin 33 PH.00, pin 35 PI.02, pin 37 PY.02  -> Motor B (right)
LINES = [EN_A, IN1, IN2, EN_B, IN3, IN4]

# Flip these if a wheel spins the wrong way for "forward".
LEFT_INVERT = False
RIGHT_INVERT = False
# Motor A is physically on the RIGHT side of the robot, so swap sides.
SWAP_SIDES = True

PWM_HZ = 100
MIN_DUTY = 0.0   # raise (e.g. 0.3) if motors stall at low speeds

# --- kernel GPIO v1 uAPI ---
_REQ_FMT = "64I I 64B 32s I i"          # struct gpiohandle_request
_REQ_SIZE = struct.calcsize(_REQ_FMT)
def _iowr(nr, size): return (3 << 30) | (size << 16) | (0xB4 << 8) | nr
GPIO_GET_LINEHANDLE_IOCTL = _iowr(0x03, _REQ_SIZE)
GPIOHANDLE_SET_LINE_VALUES_IOCTL = _iowr(0x09, 64)
GPIOHANDLE_REQUEST_OUTPUT = 1 << 1


class Motors:
    def __init__(self):
        offs = LINES + [0] * (64 - len(LINES))
        req = struct.pack(_REQ_FMT, *offs, GPIOHANDLE_REQUEST_OUTPUT,
                          *([0] * 64), b"motors", len(LINES), 0)
        chip = os.open(CHIP, os.O_RDWR)
        try:
            res = bytearray(req)
            fcntl.ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, res)
        finally:
            os.close(chip)
        self._fd = struct.unpack(_REQ_FMT, res)[-1]
        self._lock = threading.Lock()
        self._dir = [0, 0]      # -1/0/1 for A, B
        self._duty = [0.0, 0.0]
        self._running = True
        self._write(0, 0)
        self._t = threading.Thread(target=self._pwm_loop, daemon=True)
        self._t.start()

    def _write(self, en_a, en_b):
        a, b = self._dir
        vals = [en_a, int(a > 0), int(a < 0), en_b, int(b > 0), int(b < 0)]
        fcntl.ioctl(self._fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL,
                    bytearray(vals + [0] * (64 - len(vals))))

    def _pwm_loop(self):
        period = 1.0 / PWM_HZ
        while self._running:
            with self._lock:
                da, db = self._duty
            start = time.monotonic()
            if da in (0.0, 1.0) and db in (0.0, 1.0):
                with self._lock:
                    self._write(int(da == 1.0), int(db == 1.0))
                time.sleep(period)
                continue
            # on phase: both on, then turn off each at its duty point
            events = sorted([(da, "a"), (db, "b")])
            on = {"a": da > 0, "b": db > 0}
            with self._lock:
                self._write(int(on["a"]), int(on["b"]))
            for d, ch in events:
                if 0 < d < 1:
                    time.sleep(max(0, start + d * period - time.monotonic()))
                    on[ch] = False
                    with self._lock:
                        self._write(int(on["a"]), int(on["b"]))
            time.sleep(max(0, start + period - time.monotonic()))

    @staticmethod
    def _split(v, invert):
        v = max(-1.0, min(1.0, float(v)))
        if invert: v = -v
        d = 0 if v == 0 else (1 if v > 0 else -1)
        duty = abs(v)
        if duty > 0: duty = MIN_DUTY + (1 - MIN_DUTY) * duty
        return d, duty

    def drive(self, left, right):
        """left/right in -1.0 .. 1.0 (negative = reverse)."""
        if SWAP_SIDES:
            left, right = right, left
        da, sa = self._split(left, LEFT_INVERT)
        db, sb = self._split(right, RIGHT_INVERT)
        with self._lock:
            self._dir = [da, db]
            self._duty = [sa, sb]

    def forward(self, s=1.0): self.drive(s, s)
    def back(self, s=1.0):    self.drive(-s, -s)
    def left(self, s=1.0):    self.drive(-s, s)
    def right(self, s=1.0):   self.drive(s, -s)
    def stop(self):           self.drive(0, 0)

    def close(self):
        self.stop()
        time.sleep(2.0 / PWM_HZ)
        self._running = False
        self._t.join()
        with self._lock:
            self._dir = [0, 0]
            self._write(0, 0)
        os.close(self._fd)

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _main(argv):
    if not argv:
        print(__doc__); return 1
    cmd, args = argv[0], [float(x) for x in argv[1:]]
    with Motors() as m:
        if cmd == "stop":
            return 0
        if cmd == "test":
            for name, (l, r) in [("forward", (1, 1)), ("back", (-1, -1)),
                                 ("left", (-1, 1)), ("right", (1, -1)),
                                 ("half speed", (0.5, 0.5))]:
                print(name); m.drive(l, r); time.sleep(1); m.stop(); time.sleep(0.5)
            return 0
        if cmd == "drive":
            l, r = args[0], args[1]; secs = args[2] if len(args) > 2 else 1.0
            m.drive(l, r)
        elif cmd in ("forward", "back", "left", "right"):
            s = args[0] if args else 1.0; secs = args[1] if len(args) > 1 else 1.0
            getattr(m, cmd)(s)
        else:
            print("unknown command", cmd); return 1
        try:
            time.sleep(secs)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
