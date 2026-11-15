#!/usr/bin/env python3
"""How hard do real wrist impacts hit? Checks the detector's impact trigger (impact_g).

Uses the BITS-2 fall dataset: 41 volunteers, left-wrist IMU, 8 fall types and
16 daily activities, 5 trials each. Licence CC BY 4.0. Creators: Purab Nandi,
Anupama K.R., Himanish Agarwal. https://doi.org/10.5281/zenodo.10013090

    python3 tools/bits2_peaks.py            # downloads ~26 MB to ./bits2/ on first run
    python3 tools/bits2_peaks.py --data DIR # use an already-extracted copy

Why only peaks: the dataset's timestamps were saved rounded (e.g. 2.92E+12) and
each file holds ~5 back-to-back trials of ~5 s with no markers. That's too
short and unaligned to replay through the full detector (it needs ~3 s of
stillness after an impact). Per-file peaks overstate per-trial peaks, so real
per-trial trigger rates are somewhat lower than printed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import re
import statistics
import urllib.request
import zipfile
from collections import defaultdict

URL = "https://zenodo.org/records/10013090/files/full_dataset.zip?download=1"
MD5 = "65024cad5bd1c20be22c03ec78e50b79"
G = 9.81  # the "acg" stream is m/s^2 including gravity (median at rest = 9.8)


def fetch(dest: str) -> str:
    root = os.path.join(dest, "Dataset")
    if os.path.isdir(root):
        return root
    os.makedirs(dest, exist_ok=True)
    zpath = os.path.join(dest, "full_dataset.zip")
    if not os.path.exists(zpath):
        print("Downloading BITS-2 (~26 MB) ...")
        urllib.request.urlretrieve(URL, zpath)
    with open(zpath, "rb") as f:
        if hashlib.md5(f.read()).hexdigest() != MD5:
            raise SystemExit("checksum mismatch: delete %s and retry" % zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    return root


def peak_g(path: str) -> float:
    mags = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) > 5 and row[5] == "acg":
                try:
                    mags.append(math.sqrt(sum(float(v) ** 2 for v in row[1:4])))
                except ValueError:
                    pass
    return max(mags) / G if len(mags) >= 10 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="bits2", help="download/extract directory")
    ap.add_argument("--thresholds", default="1.8,2.0,2.2,2.5,3.0")
    args = ap.parse_args()
    root = fetch(args.data)

    by_activity = defaultdict(list)
    for kind in ("fall", "adl"):
        for dirpath, _, files in os.walk(os.path.join(root, kind)):
            for name in files:
                m = re.search(r"(fall|adl)(\d+)\.csv$", name)
                p = peak_g(os.path.join(dirpath, name)) if m else float("nan")
                if m and not math.isnan(p):
                    by_activity[(m.group(1), int(m.group(2)))].append(p)

    print("\nPeak |acceleration| per file, by activity")
    for key in sorted(by_activity):
        v = by_activity[key]
        print("  %-4s %2d   median %4.1f g   files >= 2.0 g: %3.0f%%   >= 2.5 g: %3.0f%%" % (
            key[0], key[1], statistics.median(v), 100 * sum(x >= 2.0 for x in v) / len(v),
            100 * sum(x >= 2.5 for x in v) / len(v)))

    falls = [p for (k, _), v in by_activity.items() if k == "fall" for p in v]
    adls = [p for (k, _), v in by_activity.items() if k == "adl" for p in v]
    print("\nTrigger level vs. what reaches the detector's later checks")
    for thr in (float(t) for t in args.thresholds.split(",")):
        print("  impact_g=%.1f   falls triggered: %3.0f%%   daily-activity files triggered: %3.0f%%" % (
            thr, 100 * sum(x >= thr for x in falls) / len(falls), 100 * sum(x >= thr for x in adls) / len(adls)))


if __name__ == "__main__":
    main()
