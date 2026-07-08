"""``jnxfeed capture`` — stream a live ITCH session to a ``.itch`` file
(T3.3): the "gather more sample data" tool.

Logs in over SoupBinTCP (default requested seq 1 = full-session replay),
appends every sequenced ITCH message to the output file in the ITCH
Binary Data File format (jnxfeed.itchfile), and maintains a sidecar
``FILE.itch.meta.json`` recording session id, first/next sequence
numbers, per-type message counts and timestamps. Client heartbeats are
maintained while idle.

Resilience:
- On disconnect/silence the tool reconnects (bounded retries) and
  re-logs-in with the same session and the next expected sequence
  number, discarding any replayed duplicates, so the file has no gaps
  and no duplicates.
- Re-running with the same ``--out`` resumes where the sidecar left
  off (append mode) unless ``--seq`` is given explicitly.
- Ctrl-C (SIGINT) or a server `Z` End-of-Session stop the capture
  cleanly with the sidecar finalized.

Exit codes (also in ``--help``):
  0  clean stop (end of session, --max-messages reached, or Ctrl-C)
  2  bad command line
  3  TCP connect failed (retries exhausted)
  4  login rejected
  5  protocol error (e.g. server sequence gap that cannot be filled)
"""
import argparse
import json
import os
import sys
import time
from collections import OrderedDict

from jnxfeed import itchfile
from jnxfeed.cli import soupclient
from jnxfeed.soup import packets as sp

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONNECT = 3
EXIT_REJECTED = 4
EXIT_PROTOCOL = 5

#: Update the sidecar at least every this many captured messages.
_SIDECAR_EVERY = 500

_EPILOG = """\
exit codes:
  0  clean stop (server end-of-session, --max-messages reached, or Ctrl-C)
  2  bad command line
  3  TCP connect failed (retries exhausted)
  4  login rejected (A = not authorized / wrong username-port pairing,
     S = session unavailable)
  5  protocol error (e.g. unfillable server sequence gap)
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jnxfeed capture",
        description="Capture a live SoupBinTCP ITCH session to a .itch file.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="server hostname or IP")
    parser.add_argument("--port", required=True, type=int, help="server TCP port")
    parser.add_argument("--user", required=True, help="SoupBinTCP username (max 6 chars)")
    parser.add_argument("--pass", dest="password", required=True,
                        help="SoupBinTCP password (max 10 chars)")
    parser.add_argument("--out", required=True, metavar="FILE.itch",
                        help="output file (ITCH Binary Data File format); a sidecar "
                             "FILE.itch.meta.json is maintained next to it")
    parser.add_argument("--seq", type=int, default=None,
                        help="requested sequence number (default: resume from the "
                             "sidecar if FILE exists, else 1 = full-session replay)")
    parser.add_argument("--session", default="",
                        help="requested session (default blank = current session)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="connect/login timeout in seconds (default 10)")
    parser.add_argument("--idle-timeout", type=float,
                        default=soupclient.DEFAULT_SILENCE_TIMEOUT,
                        help="declare the connection dead after this many seconds of "
                             "silence and reconnect (default 15, per spec)")
    parser.add_argument("--retries", type=int, default=5,
                        help="max reconnect attempts after a disconnect (default 5); "
                             "the counter resets whenever data flows again")
    parser.add_argument("--retry-delay", type=float, default=1.0,
                        help="seconds to wait before a reconnect attempt (default 1)")
    parser.add_argument("--max-messages", type=int, default=None, metavar="N",
                        help="stop cleanly after capturing N messages (default: run "
                             "until end of session / Ctrl-C)")
    return parser


def sidecar_path(out_path):
    return out_path + ".meta.json"


def _load_sidecar(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_sidecar(path, meta):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _Capture(object):
    """State for one capture run (kept linear and explicit on purpose)."""

    def __init__(self, args, log):
        self.args = args
        self.log = log
        self.meta_path = sidecar_path(args.out)
        self.session = args.session
        self.first_seq = None      # seq of the first message in the file
        self.next_seq = None       # seq of the next message we still need
        self.counts = OrderedDict()
        self.message_count = 0
        self.reconnects = 0
        self.started_at = _utcnow()
        self.end_reason = None
        self._since_sidecar = 0
        self._resume()

    # -- sidecar / resume ---------------------------------------------------

    def _resume(self):
        args = self.args
        meta = None
        if os.path.exists(args.out):
            meta = _load_sidecar(self.meta_path)
        if args.seq is not None:
            self.next_seq = args.seq
            self.append = False
            if meta is not None:
                self.log("--seq given explicitly: overwriting existing {} "
                         "(previous sidecar ignored)".format(args.out))
        elif meta is not None and isinstance(meta.get("next_seq"), int):
            self.next_seq = meta["next_seq"]
            self.append = True
            self.first_seq = meta.get("first_seq")
            self.message_count = int(meta.get("message_count", 0))
            self.counts = OrderedDict(
                sorted((meta.get("message_type_counts") or {}).items())
            )
            self.started_at = meta.get("started_at", self.started_at)
            if not self.session:
                self.session = meta.get("session") or ""
            self.log("resuming {}: {} message(s) already captured, "
                     "next seq {}".format(args.out, self.message_count, self.next_seq))
        else:
            self.next_seq = 1
            self.append = False

    def write_sidecar(self, final=False):
        meta = OrderedDict()
        meta["session"] = self.session
        meta["first_seq"] = self.first_seq
        meta["next_seq"] = self.next_seq
        meta["message_count"] = self.message_count
        meta["message_type_counts"] = self.counts
        meta["reconnects"] = self.reconnects
        meta["started_at"] = self.started_at
        meta["updated_at"] = _utcnow()
        if final:
            meta["ended_at"] = meta["updated_at"]
            meta["end_reason"] = self.end_reason
        _write_sidecar(self.meta_path, meta)
        self._since_sidecar = 0

    # -- the capture loop ----------------------------------------------------

    def run(self):
        args = self.args
        writer = itchfile.ItchFileWriter(args.out, append=self.append)
        retries_left = args.retries
        try:
            while True:
                client = soupclient.SoupClient(
                    args.host, args.port, silence_timeout=args.idle_timeout
                )
                try:
                    client.connect(timeout=args.timeout)
                except soupclient.ConnectFailed as exc:
                    self.log("connect failed: {}".format(exc))
                    if retries_left <= 0:
                        self.end_reason = "connect_failed"
                        return EXIT_CONNECT
                    retries_left -= 1
                    time.sleep(args.retry_delay)
                    continue

                try:
                    accepted = client.login(
                        args.user, args.password,
                        requested_session=self.session,
                        requested_seq=self.next_seq,
                        timeout=args.timeout,
                    )
                except soupclient.LoginRejected as exc:
                    self.log("login rejected: code {!r} — {}".format(
                        exc.code, exc.meaning))
                    self.end_reason = "login_rejected_{}".format(exc.code)
                    client.close()
                    return EXIT_REJECTED
                except soupclient.SoupClientError as exc:
                    self.log("login failed: {}".format(exc))
                    client.close()
                    if retries_left <= 0:
                        self.end_reason = "login_failed"
                        return EXIT_CONNECT
                    retries_left -= 1
                    time.sleep(args.retry_delay)
                    continue

                self.session = accepted.session
                self.log("logged in: session={!r} server_next_seq={} "
                         "(we need seq {})".format(
                             accepted.session, accepted.sequence, self.next_seq))
                if accepted.sequence > self.next_seq:
                    # The server can no longer serve the messages we are
                    # missing — continuing would put a gap in the file.
                    self.log("FATAL: server's next seq {} is past our next "
                             "needed seq {} — capture would have a gap".format(
                                 accepted.sequence, self.next_seq))
                    self.end_reason = "sequence_gap"
                    client.logout()
                    return EXIT_PROTOCOL

                status = self._pump(client, writer, accepted.sequence)
                if status == "made-progress":
                    retries_left = self.args.retries  # data flowed: reset budget
                    status = "disconnected"
                if status == "done":
                    return EXIT_OK
                # disconnected: reconnect and resume
                if retries_left <= 0:
                    self.log("giving up after {} reconnect attempt(s)".format(
                        args.retries))
                    self.end_reason = "retries_exhausted"
                    return EXIT_CONNECT
                retries_left -= 1
                self.reconnects += 1
                self.write_sidecar()
                self.log("reconnecting in {:.1f}s (resume at seq {})".format(
                    args.retry_delay, self.next_seq))
                time.sleep(args.retry_delay)
        except KeyboardInterrupt:
            self.log("interrupted — stopping cleanly")
            self.end_reason = "interrupted"
            return EXIT_OK
        finally:
            writer.flush()
            writer.close()
            if self.end_reason is None:
                self.end_reason = "unknown"
            self.write_sidecar(final=True)

    def _pump(self, client, writer, server_seq):
        """Receive until end/disconnect. Returns 'done', 'disconnected' or
        'made-progress' (disconnected, but after capturing new data)."""
        args = self.args
        wrote_any = False
        seq = server_seq  # seq of the next SequencedData packet to arrive
        try:
            while True:
                if (args.max_messages is not None
                        and self.message_count >= args.max_messages):
                    self.log("captured {} message(s) — --max-messages reached".format(
                        self.message_count))
                    self.end_reason = "max_messages"
                    client.logout()
                    return "done"
                try:
                    pkt = client.next_packet(timeout=None)
                except (soupclient.ConnectionLost, soupclient.PeerSilent) as exc:
                    self.log("connection lost: {}".format(exc))
                    return "made-progress" if wrote_any else "disconnected"

                if isinstance(pkt, sp.SequencedData):
                    if seq == self.next_seq:
                        self._write(writer, pkt.message)
                        wrote_any = True
                        self.next_seq = seq + 1
                    # seq < next_seq: replayed duplicate after resume — skip.
                    seq += 1
                elif isinstance(pkt, sp.EndOfSession):
                    self.log("end of session (Z) received — capture complete")
                    self.end_reason = "end_of_session"
                    client.close()
                    return "done"
                # Heartbeats/debug packets need no handling here.
        finally:
            client.close()

    def _write(self, writer, message):
        writer.write(message)
        if self.first_seq is None:
            self.first_seq = self.next_seq
        self.message_count += 1
        msg_type = chr(message[0]) if message else "?"
        self.counts[msg_type] = self.counts.get(msg_type, 0) + 1
        self._since_sidecar += 1
        if self._since_sidecar >= _SIDECAR_EVERY:
            writer.flush()
            self.write_sidecar()


def main(argv=None, out=None):
    out = out if out is not None else sys.stdout
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    def log(text):
        out.write("capture: {}\n".format(text))

    capture = _Capture(args, log)
    code = capture.run()
    log("stopped ({}): {} message(s) in {}, next seq {} — sidecar {}".format(
        capture.end_reason, capture.message_count, args.out,
        capture.next_seq, sidecar_path(args.out)))
    return code


if __name__ == "__main__":
    sys.exit(main())
