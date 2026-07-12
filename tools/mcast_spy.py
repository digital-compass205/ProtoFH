#!/usr/bin/env python3
"""mcast_spy.py — join the jnxfh multicast group and decode UPDATE records.

Python 3.6-safe (target-box tool; must pass tools/py36check.py).

Usage:
    python3 tools/mcast_spy.py [--group G] [--port P] [--iface A]
                               [--stats] [--until-idle SECS]
                               [--max-wait SECS]

Default mode prints one line per UPDATE (pub_seq, trigger, ticker, best
bid/ask, last trade). --stats suppresses per-update lines and prints only
the final summary. --until-idle N exits N seconds after the last received
datagram (the idle clock starts once the FIRST datagram arrives; if none
ever arrives, exits after --max-wait). The summary line is always printed
on exit:

    updates=N gaps=G bad=B epochs=E first_pub_seq=... last_pub_seq=...
"""
import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jnxweb import records


def price_str(raw):
    if raw == 0:
        return "-"
    return "{}.{}".format(raw // 10, raw % 10)


#: Fast header+envelope slice for --stats mode: magic u16, version u8,
#: kind char, body_len u16, reserved u16, epoch u64, pub_seq u64.
_ENVELOPE = struct.Struct(">HBcHHQQ")


def open_socket(group, port, iface):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # As large a receive buffer as the kernel allows: the FH can burst
    # ~100k datagrams/s and a Python receiver needs the slack.
    for size in (67108864, 16777216, 4194304):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
            break
        except OSError:
            continue
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group),
                       socket.inet_aton(iface or "0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(0.2)
    return sock


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="decode + print the jnxfh UPDATE multicast")
    parser.add_argument("--group", default="239.192.1.1")
    parser.add_argument("--port", type=int, default=26400)
    parser.add_argument("--iface", default="127.0.0.1",
                        help="local interface address to join on")
    parser.add_argument("--stats", action="store_true",
                        help="summary only, no per-update lines")
    parser.add_argument("--until-idle", type=float, default=None,
                        metavar="SECS",
                        help="exit after SECS without a datagram "
                             "(default 3 when the flag value is omitted "
                             "via --until-idle 3; no idle exit if absent)")
    parser.add_argument("--max-wait", type=float, default=60.0,
                        help="give up if NO datagram arrives within SECS "
                             "(default %(default)s; only with --until-idle)")
    args = parser.parse_args(argv)

    sock = open_socket(args.group, args.port, args.iface)

    updates = 0
    gaps = 0
    bad = 0
    epochs = set()
    first_pub_seq = None
    last_pub_seq = None
    expected = None  # next expected pub_seq within the current epoch
    cur_epoch = None
    started = time.monotonic()
    last_rx = None

    try:
        while True:
            now = time.monotonic()
            if args.until_idle is not None:
                if last_rx is None:
                    if now - started > args.max_wait:
                        break
                elif now - last_rx > args.until_idle:
                    break
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            last_rx = time.monotonic()
            if args.stats:
                # Fast path: only the header + envelope matter for stats;
                # full decode cannot keep up with a max-speed replay.
                if len(data) < _ENVELOPE.size:
                    bad += 1
                    continue
                magic, version, kind_b, _blen, _res, epoch, seq = \
                    _ENVELOPE.unpack_from(data)
                if (magic != records.RECORD_MAGIC or
                        version != records.RECORD_VERSION or kind_b != b"U"):
                    bad += 1
                    continue
                rec = None
            else:
                try:
                    rec = records.decode_record(data)
                except records.RecordError:
                    bad += 1
                    continue
                if rec.get("kind") != "U":
                    bad += 1
                    continue
                seq = rec["pub_seq"]
                epoch = rec["epoch"]
            updates += 1
            epochs.add(epoch)
            if first_pub_seq is None:
                first_pub_seq = seq
            last_pub_seq = seq
            if epoch != cur_epoch:
                cur_epoch = epoch
                expected = seq + 1
            else:
                if seq > expected:
                    gaps += seq - expected
                expected = seq + 1

            if not args.stats:
                bid = "-"
                ask = "-"
                if rec["level_count_bid"] > 0:
                    lvl = rec["bids"][0]
                    bid = "{}x{}".format(price_str(lvl["price"]), lvl["qty"])
                if rec["level_count_ask"] > 0:
                    lvl = rec["asks"][0]
                    ask = "{}x{}".format(price_str(lvl["price"]), lvl["qty"])
                last = "-"
                if rec["trade_count"] > 0:
                    last = "{}x{}".format(price_str(rec["last_price"]),
                                          rec["last_qty"])
                print("{} {} {:4s} bid={} ask={} last={}".format(
                    seq, rec["trigger"], rec["ticker"] or "-", bid, ask,
                    last))
    except KeyboardInterrupt:
        pass

    print("updates={} gaps={} bad={} epochs={} first_pub_seq={} "
          "last_pub_seq={}".format(
              updates, gaps, bad, len(epochs),
              first_pub_seq if first_pub_seq is not None else "-",
              last_pub_seq if last_pub_seq is not None else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
