"""Shared message-source selection for the T7.1 CLI views.

Every view works identically on three sources (plan section 1: "one
replay abstraction"):

- ``--itch-file F``: replay an ITCH Binary Data File (seq = 1-based
  position in the file);
- ``--pcap F``: replay the official MoldUDP64 UDP sample pcap (seq =
  the Mold sequence numbers from the capture);
- live: ``--host/--port/--user/--pass`` (+ optional
  ``--glimpse-host/--glimpse-port`` for snapshot bootstrap, ``--seq N``
  for the replay start) driven by FeedHandler on a Reactor.

:func:`run` decodes every message, applies it to the caller's Market,
and invokes ``on_message(seq, decoded_msg)`` after each apply; returning
``False`` from the callback stops the source early (used by --limit).
``interval_cb(handler)`` (live only) runs every ``interval`` seconds
with the live FeedHandler, for refreshing displays; file/pcap replays
are synchronous and render once at the end instead.
"""
from jnxfeed.book.market import Market
from jnxfeed.handler import FeedHandler
from jnxfeed.itch import codec
from jnxfeed.net.reactor import Reactor


class SourceError(Exception):
    """Bad/missing source arguments or a live-session failure."""


def add_source_arguments(parser):
    group = parser.add_argument_group(
        "message source (exactly one of --itch-file / --pcap / --host)")
    group.add_argument("--itch-file", metavar="F",
                       help="replay an ITCH Binary Data File (.itch)")
    group.add_argument("--pcap", metavar="F",
                       help="replay a MoldUDP64 UDP sample pcap")
    group.add_argument("--host", help="live: ITCH server host")
    group.add_argument("--port", type=int, help="live: ITCH server port")
    group.add_argument("--user", help="live: SoupBinTCP username")
    group.add_argument("--pass", dest="password",
                       help="live: SoupBinTCP password")
    group.add_argument("--glimpse-host",
                       help="live: GLIMPSE host for snapshot bootstrap "
                            "(default: bootstrap by replay from --seq)")
    group.add_argument("--glimpse-port", type=int,
                       help="live: GLIMPSE port for snapshot bootstrap")
    group.add_argument("--seq", type=int, default=1,
                       help="live: requested start sequence "
                            "(default 1 = full-session replay)")
    return group


def source_mode(args):
    """'file' | 'pcap' | 'live'; raises SourceError if ambiguous/missing."""
    chosen = [name for name, given in (
        ("file", args.itch_file), ("pcap", args.pcap), ("live", args.host),
    ) if given]
    if len(chosen) != 1:
        raise SourceError(
            "choose exactly one source: --itch-file F, --pcap F, or --host "
            "(got {})".format(", ".join(chosen) if chosen else "none"))
    mode = chosen[0]
    if mode == "live":
        missing = [flag for flag, value in (
            ("--port", args.port), ("--user", args.user),
            ("--pass", args.password),
        ) if value is None]
        if missing:
            raise SourceError("live source needs {}".format(" ".join(missing)))
        if (args.glimpse_host is None) != (args.glimpse_port is None):
            raise SourceError(
                "--glimpse-host and --glimpse-port must be given together")
    return mode


class SourceResult(object):
    """Outcome of one :func:`run`. ``ok`` is False only for a live-session
    failure (login reject, retries exhausted, snapshot failure)."""

    __slots__ = ("mode", "message_count", "handler", "failure")

    def __init__(self, mode, message_count, handler=None, failure=None):
        self.mode = mode
        self.message_count = message_count
        self.handler = handler
        self.failure = failure

    @property
    def ok(self):
        return self.failure is None


def _iter_file(path):
    from jnxfeed import itchfile
    seq = 0
    for raw in itchfile.read_file(path):
        seq += 1
        yield seq, raw


def _iter_pcap(path):
    from jnxfeed.cli.fixtures import iter_udp_sample
    for _session, seq, raw in iter_udp_sample(path):
        yield seq, raw


def run(args, market=None, on_message=None, interval_cb=None, interval=0.5):
    """Drive the selected source into ``market`` (a fresh Market is
    created when None). Returns a :class:`SourceResult`."""
    mode = source_mode(args)
    if market is None:
        market = Market()

    if mode in ("file", "pcap"):
        iterator = (_iter_file(args.itch_file) if mode == "file"
                    else _iter_pcap(args.pcap))
        count = 0
        for seq, raw in iterator:
            msg = codec.decode(raw)
            market.apply(msg)
            count += 1
            if on_message is not None and on_message(seq, msg) is False:
                break
        return SourceResult(mode, count)

    return _run_live(args, market, on_message, interval_cb, interval)


def _run_live(args, market, on_message, interval_cb, interval):
    reactor = Reactor()
    state = {"count": 0, "failure": None}

    def handler_message(seq, msg):
        state["count"] += 1
        if on_message is not None and on_message(seq, msg) is False:
            handler.stop()

    def on_ended(reason):
        reactor.stop()

    def on_failed(reason):
        state["failure"] = reason
        reactor.stop()

    handler = FeedHandler(
        reactor, market, args.host, args.port, args.user, args.password,
        glimpse_host=args.glimpse_host, glimpse_port=args.glimpse_port,
        requested_seq=args.seq,
        on_message=handler_message,
        on_ended=on_ended,
        on_failed=on_failed,
    )

    if interval_cb is not None:
        def tick():
            interval_cb(handler)
            state["tick_handle"] = reactor.call_later(interval, tick)
        state["tick_handle"] = reactor.call_later(interval, tick)

    handler.start()
    try:
        reactor.run()
    except KeyboardInterrupt:
        handler.stop()
    finally:
        reactor.close()
    return SourceResult("live", state["count"], handler=handler,
                        failure=state["failure"])
