"""Record IMU streams to CSV and replay them through the detector.

Recording real sessions (supervised falls onto a crash mat, plus a day of
normal activity) and replaying them is how you tune the thresholds. Name files
``fall_*.csv`` for trials that contain a fall and anything else for normal
activity, and ``replay`` will report detection rate and false alarms.
"""

from __future__ import annotations

import csv
import os
from typing import Iterable, Iterator, List, Optional, Tuple

from .detector import FallConfig, FallDetector, FallEvent
from .myo_ble import ImuSample

COLUMNS = ["t", "qw", "qx", "qy", "qz", "ax", "ay", "az", "gx", "gy", "gz"]


class CsvRecorder:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._f = open(path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(COLUMNS)

    def write(self, s: ImuSample) -> None:
        self._w.writerow(["%.4f" % s.t] + ["%.5f" % v for v in (*s.quat, *s.accel, *s.gyro)])

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def write_csv(path: str, samples: Iterable[ImuSample]) -> None:
    rec = CsvRecorder(path)
    for s in samples:
        rec.write(s)
    rec.close()


def read_csv(path: str) -> Iterator[ImuSample]:
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yield ImuSample(
                t=float(row["t"]),
                quat=(float(row["qw"]), float(row["qx"]), float(row["qy"]), float(row["qz"])),
                accel=(float(row["ax"]), float(row["ay"]), float(row["az"])),
                gyro=(float(row["gx"]), float(row["gy"]), float(row["gz"])),
            )


def detect_in(samples: Iterable[ImuSample], config: Optional[FallConfig] = None
              ) -> Tuple[List[FallEvent], FallDetector]:
    det = FallDetector(config, keep_evaluations=10_000)
    events = []
    for s in samples:
        ev = det.update(s.accel, s.gyro, s.t)
        if ev is not None:
            events.append(ev)
    return events, det


def expand_paths(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(sorted(os.path.join(p, f) for f in os.listdir(p) if f.endswith(".csv")))
        else:
            out.append(p)
    return out


def is_fall_file(path: str) -> bool:
    return os.path.basename(path).lower().startswith("fall")
