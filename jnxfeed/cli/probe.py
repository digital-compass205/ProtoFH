"""``jnxfeed probe`` — connectivity diagnostic for ITCH/GLIMPSE endpoints
(T3.3).

Connects, logs in over SoupBinTCP, reports what happened (connect
latency, accept/reject with the code's meaning, session id, server
sequence number, the first N decoded ITCH messages as human-readable
one-liners, heartbeat health) and logs out cleanly. With ``--glimpse``
it requests a blank session, reads the whole snapshot until the `G`
End-of-Snapshot message and reports per-type message counts plus the
next-live sequence number carried by `G`.

Everything is printed to stdout; ``--report FILE`` additionally writes
a timestamped JSON report suitable for attaching to a support e-mail.

Exit codes (also shown in ``--help``):
  0  success
  2  bad command line
  3  TCP connect failed
  4  login rejected (see the reported reject code)
  5  protocol error or timeout after a successful connect
"""
import argparse
import json
import sys
import time
from collections import OrderedDict

from jnxfeed import types
from jnxfeed.cli import soupclient
from jnxfeed.itch import codec as itch_codec
from jnxfeed.itch import schema as itch_schema
from jnxfeed.soup import packets as sp

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONNECT = 3
EXIT_REJECTED = 4
EXIT_PROTOCOL = 5

_EPILOG = """\
exit codes:
  0  success
  2  bad command line
  3  TCP connect failed
  4  login rejected (report shows the code: A = not authorized /
     wrong username-port pairing, S = session unavailable)
  5  protocol error or timeout after a successful connect
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jnxfeed probe",
        description="Connect + SoupBinTCP login diagnostic for ITCH/GLIMPSE endpoints.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="server hostname or IP")
    parser.add_argument("--port", required=True, type=int, help="server TCP port")
    parser.add_argument("--user", required=True, help="SoupBinTCP username (max 6 chars)")
    parser.add_argument("--pass", dest="password", required=True,
                        help="SoupBinTCP password (max 10 chars)")
    parser.add_argument("--seq", type=int, default=1,
                        help="requested sequence number (default 1 = full replay; "
                             "0 = most recent)")
    parser.add_argument("--session", default="",
                        help="requested session (default blank = current session)")
    parser.add_argument("--glimpse", action="store_true",
                        help="GLIMPSE mode: force blank requested session, read the "
                             "snapshot until the G End-of-Snapshot message")
    parser.add_argument("--messages", type=int, default=10, metavar="N",
                        help="how many sequenced messages to decode and show "
                             "(default 10; ignored in --glimpse mode)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="overall wait window in seconds (default 10)")
    parser.add_argument("--report", metavar="FILE",
                        help="also write a timestamped JSON report to FILE")
    return parser


# --- human-readable ITCH one-liners ------------------------------------------

_PRICE_FIELDS = frozenset(
    name
    for fields in itch_schema.SCHEMAS.values()
    for (name, _size, ftype) in fields
    if ftype == itch_schema.PRICE
)


def describe_itch(payload):
    """One human-readable line for one raw ITCH message."""
    try:
        msg = itch_codec.decode(payload)
    except itch_codec.DecodeError as exc:
        return "?? undecodable ({}): {}".format(exc, payload.hex())
    parts = []
    for name, value in zip(msg._fields, msg):
        if name in _PRICE_FIELDS:
            value = types.price_to_str(value)
        parts.append("{}={}".format(name, value))
    return "{} {} {}".format(chr(payload[0]), type(msg).__name__, " ".join(parts))


# --- report helpers ------------------------------------------------------------

def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit(report, lines, key, value, line=None):
    report[key] = value
    lines.append(line if line is not None else "{}: {}".format(key, value))


def _write_report(path, report):
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


# --- main ------------------------------------------------------------------------

def main(argv=None, out=None):
    out = out if out is not None else sys.stdout
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    report = OrderedDict()
    report["tool"] = "jnxfeed probe"
    report["timestamp_utc"] = _utcnow()
    report["target"] = OrderedDict(
        [("host", args.host), ("port", args.port), ("user", args.user),
         ("glimpse", bool(args.glimpse)),
         ("requested_session", "" if args.glimpse else args.session),
         ("requested_seq", args.seq)]
    )
    lines = []
    exit_code = EXIT_OK
    try:
        exit_code = _run(args, report, lines)
    except KeyboardInterrupt:
        report["result"] = "interrupted"
        lines.append("interrupted")
        exit_code = EXIT_PROTOCOL
    report["exit_code"] = exit_code
    out.write("\n".join(lines) + "\n")
    if args.report:
        _write_report(args.report, report)
        out.write("JSON report written to {}\n".format(args.report))
    return exit_code


def _run(args, report, lines):
    requested_session = "" if args.glimpse else args.session
    client = soupclient.SoupClient(args.host, args.port)

    # connect
    try:
        latency = client.connect(timeout=args.timeout)
    except soupclient.ConnectFailed as exc:
        _emit(report, lines, "connect", "failed", "connect: FAILED — {}".format(exc))
        report["error"] = str(exc)
        return EXIT_CONNECT
    _emit(report, lines, "connect_latency_ms", round(latency * 1000.0, 3),
          "connect: ok ({:.1f} ms) to {}:{}".format(latency * 1000.0, args.host, args.port))

    try:
        # login
        try:
            accepted = client.login(
                args.user, args.password,
                requested_session=requested_session,
                requested_seq=args.seq,
                timeout=args.timeout,
            )
        except soupclient.LoginRejected as exc:
            _emit(report, lines, "login", "rejected",
                  "login: REJECTED code {!r} — {}".format(exc.code, exc.meaning))
            report["reject_code"] = exc.code
            report["reject_meaning"] = exc.meaning
            return EXIT_REJECTED
        except soupclient.SoupClientError as exc:
            _emit(report, lines, "login", "error", "login: ERROR — {}".format(exc))
            report["error"] = str(exc)
            return EXIT_PROTOCOL

        _emit(report, lines, "login", "accepted",
              "login: ACCEPTED session={!r} next_seq={}".format(
                  accepted.session, accepted.sequence))
        report["session"] = accepted.session
        report["server_next_seq"] = accepted.sequence

        if args.glimpse:
            code = _run_glimpse(args, client, accepted, report, lines)
        else:
            code = _run_itch(args, client, accepted, report, lines)
        if code == EXIT_OK:
            client.logout()
            _emit(report, lines, "logout", "clean", "logout: clean")
        return code
    finally:
        client.close()


def _run_itch(args, client, accepted, report, lines):
    """Collect the first N sequenced messages + heartbeat health."""
    wanted = max(args.messages, 0)
    messages = []
    heartbeat_seen = False
    end_of_session = False
    deadline = time.monotonic() + args.timeout
    seq = accepted.sequence
    error = None

    while True:
        # Stop once the requested number of messages is in hand; on a busy
        # feed the server never idles long enough to send heartbeats, and
        # flowing sequenced data proves the link just as well.
        if len(messages) >= wanted:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            pkt = client.next_packet(timeout=remaining)
        except soupclient.WaitTimeout:
            break
        except soupclient.SoupClientError as exc:
            error = str(exc)
            break
        if isinstance(pkt, sp.SequencedData):
            if len(messages) < wanted:
                messages.append((seq, pkt.message))
            seq += 1
        elif isinstance(pkt, sp.ServerHeartbeat):
            heartbeat_seen = True
        elif isinstance(pkt, sp.EndOfSession):
            end_of_session = True
            break
        # Debug/other packets are ignored.

    lines.append("messages: {} sequenced message(s) received".format(len(messages)))
    report["messages_received"] = len(messages)
    decoded = []
    for msg_seq, payload in messages:
        line = describe_itch(payload)
        decoded.append(OrderedDict([("seq", msg_seq), ("text", line)]))
        lines.append("  seq {:>10}  {}".format(msg_seq, line))
    report["messages"] = decoded

    if heartbeat_seen:
        hb = "observed"
    elif messages:
        hb = "not observed (sequenced data flowing, link is healthy)"
    else:
        hb = "NOT observed"
    _emit(report, lines, "server_heartbeat", hb, "server heartbeat: {}".format(hb))
    if end_of_session:
        _emit(report, lines, "end_of_session", True,
              "end of session (Z) received from server")
    if error is not None:
        report["error"] = error
        lines.append("error: {}".format(error))
        return EXIT_PROTOCOL
    if not messages and not heartbeat_seen and not end_of_session:
        lines.append("error: nothing received within {:.1f}s window".format(args.timeout))
        report["error"] = "nothing received within timeout window"
        return EXIT_PROTOCOL
    return EXIT_OK


def _run_glimpse(args, client, accepted, report, lines):
    """Read a whole GLIMPSE snapshot until the `G` End-of-Snapshot."""
    counts = OrderedDict()
    total = 0
    next_live_seq = None
    deadline = time.monotonic() + args.timeout

    while next_live_seq is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            pkt = client.next_packet(timeout=remaining)
        except soupclient.WaitTimeout:
            break
        except soupclient.SoupClientError as exc:
            report["error"] = str(exc)
            lines.append("error: {}".format(exc))
            return EXIT_PROTOCOL
        if isinstance(pkt, sp.SequencedData):
            total += 1
            payload = pkt.message
            msg_type = chr(payload[0]) if payload else "?"
            counts[msg_type] = counts.get(msg_type, 0) + 1
            if msg_type == "G":
                try:
                    next_live_seq = itch_codec.decode(payload).sequence_number
                except itch_codec.DecodeError as exc:
                    report["error"] = "bad G message: {}".format(exc)
                    lines.append("error: bad G message: {}".format(exc))
                    return EXIT_PROTOCOL
        elif isinstance(pkt, sp.EndOfSession):
            break

    lines.append("snapshot: {} message(s), counts: {}".format(
        total, " ".join("{}={}".format(k, v) for k, v in counts.items()) or "(none)"))
    report["snapshot_message_count"] = total
    report["snapshot_counts"] = counts
    if next_live_seq is None:
        report["error"] = "snapshot did not complete (no G) within timeout"
        lines.append("error: snapshot did not complete (no G End-of-Snapshot) "
                     "within {:.1f}s".format(args.timeout))
        return EXIT_PROTOCOL
    _emit(report, lines, "next_live_seq", next_live_seq,
          "end of snapshot: G received, next live ITCH seq = {}".format(next_live_seq))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
