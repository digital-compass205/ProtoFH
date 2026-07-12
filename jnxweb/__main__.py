"""jnxweb entry point: ``python3 -m jnxweb``.

Wires the multicast (or --test-feed) record source, the in-memory
state cache, the WebSocket hub and the HTTP server onto one shared
``selectors`` reactor (JNX_PLAN.md §0: no asyncio, no threads doing
I/O -- see ``jnxfeed/net/reactor.py`` for the established pattern this
mirrors).
"""
import argparse
import logging
import signal
import sys
import time

from jnxfeed.net.reactor import Reactor
from jnxweb import httpd, mcast, state as state_mod, wsock
from jnxweb.static_page import PAGE_HTML

log = logging.getLogger("jnxweb")

_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

STATS_INTERVAL = 5.0


def parse_mcast_arg(text):
    """--mcast GROUP:PORT -> (group, port)."""
    group, _, port_str = text.rpartition(":")
    if not group or not port_str:
        raise argparse.ArgumentTypeError(
            "expected GROUP:PORT, got {!r}".format(text))
    try:
        port = int(port_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "bad port in {!r}".format(text))
    return group, port


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m jnxweb",
        description="Japannext feed web GUI client (Python 3.6, stdlib-only)")
    parser.add_argument("--mcast", type=parse_mcast_arg,
                        default=("239.192.1.1", 26400),
                        help="multicast GROUP:PORT (default: %(default)s)")
    parser.add_argument("--http-port", type=int, default=8080,
                        help="HTTP/WebSocket listen port (default: %(default)s)")
    parser.add_argument("--http-host", default="0.0.0.0",
                        help="HTTP listen address (default: %(default)s)")
    parser.add_argument("--mcast-if", default="127.0.0.1",
                        help="local interface address to join the multicast "
                             "group on (default: %(default)s)")
    parser.add_argument("--test-feed", default=None, metavar="UDS_PATH",
                        help="use an AF_UNIX datagram socket at UDS_PATH as "
                             "the record source instead of joining the real "
                             "multicast group -- lets tests/tools inject "
                             "canned UPDATE records with "
                             "socket.sendto(bytes, UDS_PATH); see "
                             "jnxweb/mcast.py")
    parser.add_argument("--db-query-host", default="127.0.0.1",
                        help="jnxdb query-port host, for the 'all orders' "
                             "on-demand lookup (default: %(default)s -- "
                             "jnxdb binds that port to localhost only, so "
                             "this only works when jnxweb runs on the same "
                             "host as jnxdb)")
    parser.add_argument("--db-query-port", type=int, default=26401,
                        help="jnxdb query-port TCP port (default: "
                             "%(default)s, matching etc/jnxdb.cfg's "
                             "default); pass --db-query-port=0 to disable "
                             "the 'all orders' lookup entirely")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT,
                        datefmt=_LOG_DATEFMT)

    reactor = Reactor()
    hub_holder = {}

    def on_ticker_update(ticker):
        hub_holder["hub"].on_ticker_update(ticker)

    def on_restart(epoch):
        log.warning("feed restarted: epoch=%s (state cleared)", epoch)
        hub_holder["hub"].on_restart(epoch)

    state = state_mod.State(on_ticker_update=on_ticker_update,
                             on_restart=on_restart)
    hub = wsock.WsHub(reactor, state)
    hub_holder["hub"] = hub

    if args.test_feed:
        log.info("record source: test-feed uds=%s", args.test_feed)
        sock = mcast.open_test_feed_socket(args.test_feed)
    else:
        group, port = args.mcast
        log.info("record source: multicast group=%s port=%s iface=%s",
                 group, port, args.mcast_if)
        sock = mcast.open_mcast_socket(group, port, args.mcast_if)
    receiver = mcast.McastReceiver(reactor, sock, state)

    db_query_addr = (None if args.db_query_port == 0
                     else (args.db_query_host, args.db_query_port))
    server = httpd.HttpServer(reactor, state, hub, PAGE_HTML,
                              host=args.http_host, port=args.http_port,
                              db_query_addr=db_query_addr)
    log.info("http listening on %s:%s (db_query=%s)",
            args.http_host, server.port, db_query_addr)
    # Machine-readable readiness line for tests/tools that bind :0 and
    # need to discover the ephemeral port (also works for a fixed port).
    print("jnxweb listening on {}:{}".format(args.http_host, server.port))
    sys.stdout.flush()

    def log_stats():
        s = state.stats()
        log.info(
            "STATS updates=%s bad=%s gaps=%s restarts=%s epoch=%s "
            "tickers=%s ws_clients=%s",
            s["updates"], s["bad"], s["gaps"], s["restarts"],
            s["last_epoch"], s["tickers"], len(hub.clients))
        reactor.call_later(STATS_INTERVAL, log_stats)

    reactor.call_later(STATS_INTERVAL, log_stats)

    def handle_sigint(signum, frame):
        log.info("SIGINT received, shutting down")
        reactor.stop()

    old_handler = signal.signal(signal.SIGINT, handle_sigint)
    try:
        reactor.run()
    finally:
        signal.signal(signal.SIGINT, old_handler)
        receiver.close()
        server.close()
        for client in list(hub.clients):
            client.close()
        reactor.close()
    log.info("shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
