"""Standalone exchange simulator: ``python -m jnxfeed.sim`` (T6.1).

Serves a ``.itch`` fixture as a SoupBinTCP ITCH server plus a GLIMPSE
snapshot server until interrupted (Ctrl-C).
"""
import argparse
import logging
import sys
import time

from jnxfeed.sim.exchange import SPEED_MAX, SPEED_REALTIME, ExchangeSimulator


def parse_speed(text):
    """--speed value: 'max' (default), 'realtime', or messages/sec."""
    if text == "max":
        return SPEED_MAX
    if text == "realtime":
        return SPEED_REALTIME
    return float(text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m jnxfeed.sim",
        description="Japannext exchange simulator: replay a .itch fixture "
                    "as SoupBinTCP ITCH + GLIMPSE servers.",
    )
    parser.add_argument("--itch-file", required=True,
                        help=".itch fixture to replay")
    parser.add_argument("--itch-port", type=int, default=15001,
                        help="ITCH server port (default: %(default)s)")
    parser.add_argument("--glimpse-port", type=int, default=15002,
                        help="GLIMPSE server port (default: %(default)s)")
    parser.add_argument("--user", default="TEST", help="expected username")
    parser.add_argument("--pass", dest="password", default="SECRET",
                        help="expected password")
    parser.add_argument("--session", default="SIM0000001",
                        help="Soup session id (default: %(default)s)")
    parser.add_argument("--speed", type=parse_speed, default="max",
                        help="'max', 'realtime', or messages/sec "
                             "(default: max)")
    parser.add_argument("--glimpse-cut", type=float, default=0.5,
                        help="snapshot cut point: fraction <1 or message "
                             "count (default: %(default)s)")
    parser.add_argument("--drop-after", type=int, default=None,
                        help="scripted disconnect after N sequenced packets "
                             "(once)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sim = ExchangeSimulator(
        itch_file=args.itch_file,
        username=args.user, password=args.password,
        itch_port=args.itch_port, glimpse_port=args.glimpse_port,
        session_id=args.session, speed=args.speed,
        drop_after=args.drop_after, glimpse_cut=args.glimpse_cut,
    )
    sim.start()
    print("ITCH on :{}, GLIMPSE on :{} -- Ctrl-C to stop".format(
        sim.itch_port, sim.glimpse_port))
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
