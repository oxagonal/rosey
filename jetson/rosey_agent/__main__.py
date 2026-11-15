"""python -m rosey_agent  -- connect the Jetson to the Rosey control server.

Settings come from flags or environment variables (flags win):
    ROSEY_SERVER   e.g. https://rosey-control.onrender.com
    ROBOT_TOKEN    the ROBOT_TOKEN value from the Render dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from .agent import RobotAgent
from .motors import Pins, make_motors


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="python -m rosey_agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=env("ROSEY_SERVER"), help="control server URL")
    ap.add_argument("--token", default=env("ROBOT_TOKEN"), help="robot token (prefer the env var)")
    ap.add_argument("--name", default=env("ROSEY_NAME", "rosey"))
    ap.add_argument("--mock", action="store_true", default=env("ROSEY_MOCK") == "1",
                    help="no GPIO: log motor commands instead (for testing on a laptop)")
    ap.add_argument("--no-pwm", action="store_true", help="on/off motor control (if PWM isn't set up)")
    ap.add_argument("--pwm-hz", type=int, default=int(env("ROSEY_PWM_HZ", 1000)))
    ap.add_argument("--min-duty", type=float, default=float(env("ROSEY_MIN_DUTY", 35)),
                    help="lowest PWM duty %% that still turns the wheels")
    ap.add_argument("--max-speed", type=float, default=float(env("ROSEY_MAX_SPEED", 1.0)),
                    help="scale all speeds by this (0-1)")
    ap.add_argument("--invert-left", action="store_true", default=env("ROSEY_INVERT_LEFT") == "1")
    ap.add_argument("--invert-right", action="store_true", default=env("ROSEY_INVERT_RIGHT") == "1")
    ap.add_argument("--max-ttl-ms", type=int, default=2000, help="longest a drive command may last")
    ap.add_argument("--myo-udp", type=int, default=int(env("ROSEY_MYO_UDP", 0)) or None, metavar="PORT",
                    help="forward Myo fall alerts received on this UDP port")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


async def amain(args) -> None:
    motors = make_motors(mock=args.mock, pins=Pins(), use_pwm=not args.no_pwm, pwm_hz=args.pwm_hz,
                         min_duty=args.min_duty, max_speed=max(0.0, min(1.0, args.max_speed)),
                         invert_left=args.invert_left, invert_right=args.invert_right)
    # Created inside the running loop (matters on Python 3.8 / JetPack 5).
    agent = RobotAgent(args.server, args.token, motors, name=args.name, max_ttl_ms=args.max_ttl_ms)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, agent.stop)
        except (NotImplementedError, RuntimeError):
            pass
    if args.myo_udp:
        from .myo_bridge import start_myo_bridge
        await start_myo_bridge(agent, args.myo_udp)
    try:
        await agent.run()
    finally:
        motors.close()


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.INFO if args.verbose else logging.WARNING)
    if not args.server or not args.token:
        raise SystemExit("Set --server and --token (or ROSEY_SERVER and ROBOT_TOKEN).")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
