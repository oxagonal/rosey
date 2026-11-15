"""Command line: python -m myo_fall {scan,run,replay,simulate}."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from .detector import FallConfig


def _overrides(pairs) -> FallConfig:
    d = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit("--set expects key=value, got %r" % p)
        k, v = p.split("=", 1)
        d[k.strip()] = v.strip()
    try:
        return FallConfig.from_overrides(d)
    except ValueError as exc:
        raise SystemExit(str(exc))


def cmd_scan(args) -> None:
    from .myo_ble import scan

    found = asyncio.run(scan(args.timeout))
    if not found:
        print("No Myo found. Wake it by moving it, check it's charged, and make sure it isn't "
              "connected to another computer (or to Myo Connect).")
        return
    for address, name, rssi in found:
        print("%s  %-20s RSSI %d dBm" % (address, name or "?", rssi))


def cmd_run(args) -> None:
    from .events import UdpPublisher, parse_target
    from .service import FallService

    record = args.record
    if record == "auto":
        record = os.path.join("recordings", datetime.now().strftime("session_%Y%m%d_%H%M%S.csv"))
    targets = [parse_target(t) for t in (args.udp or ["127.0.0.1:5005"])]
    publisher = UdpPublisher(targets)
    logging.getLogger(__name__).info("Sending events to %s", ", ".join("%s:%d" % t for t in targets))
    if record:
        logging.getLogger(__name__).info("Recording IMU to %s", record)
    service = FallService(publisher, _overrides(args.set), backend=args.backend, address=args.address,
                          tty=args.tty, record_path=record, vibrate=not args.no_vibrate)
    asyncio.run(service.run())


def cmd_replay(args) -> None:
    from .recording import detect_in, expand_paths, is_fall_file, read_csv

    cfg = _overrides(args.set)
    paths = expand_paths(args.paths)
    if not paths:
        raise SystemExit("no CSV files found")
    tp = fn = fp = tn = 0
    for path in paths:
        events, det = detect_in(read_csv(path), cfg)
        labelled_fall = is_fall_file(path)
        hit = bool(events)
        tp += labelled_fall and hit
        fn += labelled_fall and not hit
        fp += (not labelled_fall) and hit
        tn += (not labelled_fall) and not hit
        verdict = "FALL" if hit else "-"
        flag = "" if hit == labelled_fall else "   <-- %s" % ("MISSED" if labelled_fall else "FALSE ALARM")
        print("%-40s %-5s %s%s" % (os.path.basename(path), verdict, len(events) or "", flag))
        for ev in det.evaluations:
            if args.show_rejected or ev.accepted:
                print("    t=%.2f %-48s %s" % (ev.t, ev.reason, ev.features))
    n_fall, n_adl = tp + fn, fp + tn
    print()
    if n_fall:
        print("Falls detected: %d/%d (%.0f%%)" % (tp, n_fall, 100.0 * tp / n_fall))
    if n_adl:
        print("False alarms:   %d/%d non-fall files" % (fp, n_adl))


def cmd_simulate(args) -> None:
    from .recording import write_csv
    from .sim import SCENARIOS

    os.makedirs(args.outdir, exist_ok=True)
    for name, (build, _) in SCENARIOS.items():
        path = os.path.join(args.outdir, name + ".csv")
        write_csv(path, build())
        print("wrote", path)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m myo_fall", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="list Myo armbands in range")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("run", help="connect to the Myo, detect falls, send events")
    p.add_argument("--udp", action="append", metavar="HOST:PORT",
                   help="where to send events (repeatable; default 127.0.0.1:5005)")
    p.add_argument("--address", help="Myo Bluetooth address (default: first Myo found)")
    p.add_argument("--backend", choices=["bleak", "pyomyo"], default="bleak",
                   help="bleak = Jetson/USB Bluetooth (default); pyomyo = Myo's BLED112 USB dongle")
    p.add_argument("--tty", help="serial port of the BLED112 dongle (pyomyo backend)")
    p.add_argument("--record", nargs="?", const="auto", metavar="FILE.csv",
                   help="also record the raw IMU stream (default name under recordings/)")
    p.add_argument("--no-vibrate", action="store_true", help="don't buzz the armband on connect/fall")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a detector setting")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("replay", help="run the detector over recorded CSVs (fall_*.csv = falls)")
    p.add_argument("paths", nargs="+", help="CSV files or directories")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a detector setting")
    p.add_argument("-r", "--show-rejected", action="store_true", help="also show impacts that were not falls")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("simulate", help="write synthetic scenario CSVs (for trying replay)")
    p.add_argument("outdir", nargs="?", default="sim_data")
    p.set_defaults(func=cmd_simulate)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
