"""Entry point: python -m jnxfeed <subcommand>.

Connectivity kit (T3.3):
  probe    — connect + SoupBinTCP login diagnostic (ITCH or GLIMPSE)
  capture  — stream a live ITCH session to a .itch file

Views (T7.1) — each works on --itch-file / --pcap / live --host sources:
  static   — static data table (directory, states, limits, ref prices)
  tail     — one decoded line per message (filters: --types, --book)
  book     — top-N levels + last trades for one SICC (live: ANSI refresh)
  stats    — message rates, session/seq, book/order/orphan counters

The exchange simulator runs separately: python -m jnxfeed.sim --help.
"""
import sys

_USAGE = """\
usage: python -m jnxfeed <subcommand> [options]   (--help per subcommand)

subcommands:
  probe    connect + SoupBinTCP login diagnostic
  capture  stream a live ITCH session to a .itch file
  static   static data table from a file/pcap/live source
  tail     one decoded line per message
  book     top-N levels + last trades for one order book
  stats    message rates and state counters

simulator: python -m jnxfeed.sim --itch-file F [--speed realtime] ...
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(_USAGE)
        return 0 if argv else 2

    cmd = argv[0]
    rest = argv[1:]
    if cmd == "probe":
        from jnxfeed.cli import probe
        return probe.main(rest)
    if cmd == "capture":
        from jnxfeed.cli import capture
        return capture.main(rest)
    if cmd == "static":
        from jnxfeed.cli import views
        return views.main_static(rest)
    if cmd == "tail":
        from jnxfeed.cli import views
        return views.main_tail(rest)
    if cmd == "book":
        from jnxfeed.cli import views
        return views.main_book(rest)
    if cmd == "stats":
        from jnxfeed.cli import views
        return views.main_stats(rest)
    sys.stderr.write("jnxfeed: unknown subcommand {!r}\n\n{}".format(cmd, _USAGE))
    return 2


if __name__ == "__main__":
    sys.exit(main())
