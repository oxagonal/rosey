#!/usr/bin/env python3
"""Turn the robot in place to keep a detected person centered in the webcam.

  python3 ~/motors/follow.py            # track (Ctrl+C to stop)
  python3 ~/motors/follow.py --show     # also open a preview window
  python3 ~/motors/follow.py --dry-run  # detect + print, motors stay off

Detection: YuNet face detector (models/, handles tilted faces), falling back to the
HOG full-body detector when no face is found. Turning is done in short pulses,
then the robot stops and looks again, so camera lag can't make it overshoot.

After a reboot run  sudo python3 ~/motors/pinmux_fix.py  first.
"""
import argparse, threading, time
import cv2

CAM = "/dev/video0"
W, H = 640, 360                # capture size (camera downscales; fast to process)
DEADBAND = 0.12                # |offset| below this (fraction of half-width) = centered
TURN_SPEED = 0.7               # TT motors stall at low duty; keep this fairly high
PULSE_MIN, PULSE_MAX = 0.04, 0.18   # seconds of turning per correction
SETTLE = 0.12                  # pause after a pulse so the next frame is post-motion
TURN_INVERT = True             # set True if it turns AWAY from the person
import os
YUNET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "face_detection_yunet_2023mar.onnx")


class Camera:
    """Grabs frames in a thread so we always process the newest one (no lag)."""

    def __init__(self, dev):
        self.cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.cap.isOpened():
            raise SystemExit(f"could not open {dev}")
        self.frame, self.stamp = None, 0.0
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            ok, f = self.cap.read()
            if ok:
                with self.lock:
                    self.frame, self.stamp = f, time.monotonic()

    def read_after(self, t):
        """Newest frame captured after time t."""
        while True:
            with self.lock:
                if self.frame is not None and self.stamp > t:
                    return self.frame
            time.sleep(0.005)

    def close(self):
        self.running = False
        time.sleep(0.1)
        self.cap.release()


class Detector:
    def __init__(self):
        self.yunet = cv2.FaceDetectorYN.create(YUNET, "", (320, 320), 0.7, 0.3, 50)
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def find(self, frame):
        """Return (x, y, w, h, kind) of the largest person, or None."""
        h, w = frame.shape[:2]
        k = 320 / w                                # run the face net at 320 px wide
        small = cv2.resize(frame, (320, int(h * k)))
        self.yunet.setInputSize((small.shape[1], small.shape[0]))
        _, faces = self.yunet.detect(small)
        if faces is not None and len(faces):
            fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4] / k
            return int(fx), int(fy), int(fw), int(fh), "face"
        bodies, weights = self.hog.detectMultiScale(frame, winStride=(8, 8), padding=(8, 8), scale=1.05)
        best = [(b, wt) for b, wt in zip(bodies, weights) if float(wt) > 0.5]
        if best:
            return (*max(best, key=lambda bw: bw[0][2] * bw[0][3])[0], "body")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seconds", type=float, default=0, help="quit after this long (0 = forever)")
    a = ap.parse_args()

    motors = None
    if not a.dry_run:
        from motors import Motors
        motors = Motors()
    cam, det = Camera(CAM), Detector()
    t_end = time.monotonic() + a.seconds if a.seconds else None
    last = time.monotonic()
    print("tracking... Ctrl+C to stop")
    try:
        while t_end is None or time.monotonic() < t_end:
            frame = cam.read_after(last)
            hit = det.find(frame)
            if hit is None:
                status, pulse, d = "no person", 0, 0
            else:
                x, y, w, h, kind = hit
                fw = frame.shape[1]
                off = ((x + w / 2) - fw / 2) / (fw / 2)      # -1 (left) .. +1 (right)
                if abs(off) < DEADBAND:
                    status, pulse, d = f"{kind} centered  off={off:+.2f}", 0, 0
                else:
                    d = 1 if off > 0 else -1               # +1 = turn right
                    if TURN_INVERT:
                        d = -d
                    pulse = PULSE_MIN + (PULSE_MAX - PULSE_MIN) * min(1, (abs(off) - DEADBAND) / (1 - DEADBAND))
                    status = f"{kind} off={off:+.2f} -> turn {'right' if d > 0 else 'left'} {pulse*1000:.0f}ms"
            print(f"\r{status:<50}", end="", flush=True)

            if a.show:
                if hit:
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                fh, fw = frame.shape[:2]
                cv2.line(frame, (fw // 2, 0), (fw // 2, fh), (255, 255, 0), 1)
                cv2.imshow("follow", frame)
                if cv2.waitKey(1) & 0xFF in (ord("x"), ord("q"), 27):
                    break

            if pulse and motors:
                motors.drive(d * TURN_SPEED, -d * TURN_SPEED)
                time.sleep(pulse)
                motors.stop()
                time.sleep(SETTLE)
            last = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        if motors:
            motors.close()
        cam.close()
        print("\nstopped")


if __name__ == "__main__":
    main()
