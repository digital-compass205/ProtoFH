"""CLI views (JNX_PLAN.md T7.1): static / tail / book / stats.

All four subcommands work identically on the three sources handled by
jnxfeed.cli.sources (--itch-file, --pcap, or a live session). With a
file/pcap source the view renders once over the final Market state; with
a live source `tail` streams and `book`/`stats` refresh every
--interval seconds (ANSI clear-screen in-place redraw on a TTY,
plain repeated blocks otherwise -- no curses).
"""
import argparse
import csv
import sys
import time

from jnxfeed import types
from jnxfeed.book.market import Market
from jnxfeed.cli import sources
from jnxfeed.cli.describe import describe_msg
from jnxfeed.itch import messages as itch_messages

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SOURCE = 3

_CLEAR = "\x1b[2J\x1b[H"

#: Message class -> ITCH type char, for --types filtering.
_TYPE_CHARS = dict(
    (cls, char) for char, cls in itch_messages.MESSAGE_CLASSES.items()
)


def _fmt(value):
    return "-" if value is None else str(value)


def _fmt_price(value):
    return "-" if value is None else types.price_to_str(value)


def _fmt_ts(ns):
    seconds, rem = divmod(ns, 10 ** 9)
    return "{:02d}:{:02d}:{:02d}.{:03d}".format(
        seconds // 3600, (seconds // 60) % 60, seconds % 60, rem // 10 ** 6)


def _is_tty(out):
    isatty = getattr(out, "isatty", None)
    return bool(isatty and isatty())


def _parse_or_usage(parser, argv):
    try:
        return parser.parse_args(argv), None
    except SystemExit as exc:
        return None, (exc.code if isinstance(exc.code, int) else EXIT_USAGE)


def _run_source(args, out, **kwargs):
    """sources.run with CLI error reporting; returns (result, exit_code)."""
    try:
        result = sources.run(args, **kwargs)
    except sources.SourceError as exc:
        out.write("error: {}\n".format(exc))
        return None, EXIT_USAGE
    except (OSError, ValueError) as exc:
        out.write("error: {}\n".format(exc))
        return None, EXIT_SOURCE
    if not result.ok:
        out.write("live session failed: {}\n".format(result.failure))
        return result, EXIT_SOURCE
    return result, EXIT_OK


# --- static -------------------------------------------------------------------

_STATIC_HEADER = ("SICC", "ISIN", "Group", "Lot", "TickTbl", "PriceDec",
                  "Lower", "Upper", "State", "SSRestr", "RefPrice")


def static_rows(market):
    rows = []
    for sicc in sorted(market.refdata.instruments):
        inst = market.refdata.instruments[sicc]
        rows.append((
            sicc, _fmt(inst.isin), _fmt(inst.group), _fmt(inst.round_lot),
            _fmt(inst.tick_table_id), _fmt(inst.price_decimals),
            _fmt_price(inst.lower_limit), _fmt_price(inst.upper_limit),
            inst.trading_state, inst.short_sell_state,
            _fmt_price(inst.reference_price),
        ))
    return rows


def render_table(header, rows, out):
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join("{:<%d}" % w for w in widths)
    out.write(fmt.format(*header).rstrip() + "\n")
    out.write("  ".join("-" * w for w in widths) + "\n")
    for row in rows:
        out.write(fmt.format(*row).rstrip() + "\n")


def main_static(argv=None, out=None):
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="jnxfeed static",
        description="Static data table (directory, states, limits, "
                    "reference prices) from any source.",
    )
    sources.add_source_arguments(parser)
    parser.add_argument("--csv", action="store_true",
                        help="emit CSV instead of a fixed-width table")
    parser.add_argument("--master", metavar="JNX_ST_MASTER.csv",
                        help="enrich with the Stock Master daily CSV "
                             "(NOT implemented in the prototype -- Data "
                             "File Formats CSV parsing is out of scope)")
    args, code = _parse_or_usage(parser, argv)
    if args is None:
        return code
    if args.master:
        out.write("note: --master enrichment is not implemented in the "
                  "prototype (Stock Master CSV parsing is out of scope)\n")

    market = Market()
    _result, code = _run_source(args, out, market=market)
    if code != EXIT_OK:
        return code

    rows = static_rows(market)
    if args.csv:
        writer = csv.writer(out)
        writer.writerow(_STATIC_HEADER)
        writer.writerows(rows)
    else:
        render_table(_STATIC_HEADER, rows, out)
        out.write("{} instrument(s)\n".format(len(rows)))
    return EXIT_OK


# --- tail ---------------------------------------------------------------------

def main_tail(argv=None, out=None):
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="jnxfeed tail",
        description="One human-readable line per decoded message.",
    )
    sources.add_source_arguments(parser)
    parser.add_argument("--types", metavar="A,E,...",
                        help="only show these message types")
    parser.add_argument("--book", metavar="SICC",
                        help="only show messages carrying this orderbook id "
                             "(note: E/D/U carry no orderbook id and are "
                             "filtered out by this option)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="stop after N printed lines")
    args, code = _parse_or_usage(parser, argv)
    if args is None:
        return code

    wanted_types = None
    if args.types:
        wanted_types = frozenset(
            t.strip().upper() for t in args.types.split(",") if t.strip())
    printed = [0]

    def on_message(seq, msg):
        # The limit check runs BEFORE printing as well: a live source can
        # deliver several messages in one network read, and the whole
        # batch is dispatched even after the stop request.
        if args.limit is not None and printed[0] >= args.limit:
            return False
        if wanted_types is not None and _TYPE_CHARS.get(type(msg)) not in wanted_types:
            return True
        if args.book is not None and getattr(msg, "orderbook_id", None) != args.book:
            return True
        out.write("{:>10}  {}\n".format(seq, describe_msg(msg)))
        printed[0] += 1
        if args.limit is not None and printed[0] >= args.limit:
            return False
        return True

    _result, code = _run_source(args, out, on_message=on_message)
    return code


# --- book ---------------------------------------------------------------------

def render_book(market, sicc, depth, trades):
    lines = []
    inst = market.refdata.instruments.get(sicc)
    state = inst.trading_state if inst is not None else "?"
    lines.append("book {}  state={}  clock={}".format(
        sicc, state, _fmt_ts(market.seconds * 10 ** 9)))

    book = market.books.books.get(sicc)
    bids = book.bid_levels(depth) if book is not None else []
    asks = book.ask_levels(depth) if book is not None else []
    best_bid = bids[0] if bids else None
    best_ask = asks[0] if asks else None
    if best_bid and best_ask:
        spread = types.price_to_str(best_ask[0] - best_bid[0])
    else:
        spread = "-"
    total_bid = sum(q for _p, q in book.bids.levels_ascending()) if book else 0
    total_ask = sum(q for _p, q in book.asks.levels_ascending()) if book else 0
    lines.append("spread={}  bid_qty_total={}  ask_qty_total={}".format(
        spread, total_bid, total_ask))

    lines.append("{:>12} {:>12} | {:<12} {:<12}".format(
        "bid_qty", "bid", "ask", "ask_qty"))
    for i in range(max(len(bids), len(asks), 1)):
        bid_price = types.price_to_str(bids[i][0]) if i < len(bids) else ""
        bid_qty = str(bids[i][1]) if i < len(bids) else ""
        ask_price = types.price_to_str(asks[i][0]) if i < len(asks) else ""
        ask_qty = str(asks[i][1]) if i < len(asks) else ""
        lines.append("{:>12} {:>12} | {:<12} {:<12}".format(
            bid_qty, bid_price, ask_price, ask_qty).rstrip())

    stats = market.tape.book_stats(sicc)
    if stats is not None:
        lines.append("traded: {} fills, volume={}, vwap={}, last={} x {}".format(
            stats.trade_count, stats.volume,
            "-" if stats.vwap() is None else "{:.1f}".format(stats.vwap() / 10.0),
            _fmt_price(stats.last_price), _fmt(stats.last_qty)))
    else:
        lines.append("traded: nothing yet")
    for entry in reversed(market.tape.recent(n=trades, orderbook_id=sicc)):
        lines.append("  {}  {:>10} x {:<8} match={}".format(
            _fmt_ts(entry.timestamp), types.price_to_str(entry.price),
            entry.qty, entry.match_number))
    return "\n".join(lines) + "\n"


def main_book(argv=None, out=None):
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="jnxfeed book",
        description="Top-N price levels + last trades for one order book. "
                    "Live sources refresh in place (ANSI, no curses); "
                    "file/pcap sources print the final state once.",
    )
    parser.add_argument("sicc", help="orderbook id (SICC code, e.g. 1570)")
    sources.add_source_arguments(parser)
    parser.add_argument("--depth", type=int, default=5,
                        help="levels per side (default 5)")
    parser.add_argument("--trades", type=int, default=5,
                        help="recent trades to show (default 5)")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="live refresh interval seconds (default 0.5)")
    args, code = _parse_or_usage(parser, argv)
    if args is None:
        return code

    market = Market()
    clear = _CLEAR if _is_tty(out) else ""

    def refresh(_handler):
        out.write(clear + render_book(market, args.sicc, args.depth,
                                      args.trades))
        if not clear:
            out.write("\n")
        _flush(out)

    result, code = _run_source(args, out, market=market,
                               interval_cb=refresh, interval=args.interval)
    if code != EXIT_OK:
        return code
    if result.mode != "live":
        out.write(render_book(market, args.sicc, args.depth, args.trades))
    return EXIT_OK


# --- stats ---------------------------------------------------------------------

def render_stats(market, rates=None, handler=None, elapsed=None):
    counters = market.counters()
    lines = []
    if handler is not None:
        lines.append("session={}  next_seq={}  state={}  reconnects={}".format(
            _fmt(handler.session_id), _fmt(handler.next_seq),
            handler.state, handler.reconnects))
    total_line = "messages={}".format(counters["messages"])
    if elapsed:
        total_line += "  ({:.0f} msgs/s over {:.2f}s)".format(
            counters["messages"] / elapsed, elapsed)
    lines.append(total_line)
    by_type = counters["by_type"]
    lines.append("by type: " + (" ".join(
        "{}={}".format(k, v) for k, v in sorted(by_type.items())) or "(none)"))
    if rates is not None:
        lines.append("rates/s: " + (" ".join(
            "{}={:.0f}".format(k, v) for k, v in sorted(rates.items()) if v)
            or "(idle)"))
    lines.append(
        "instruments={}  live_orders={}  books={}".format(
            counters["instruments"], counters["live_orders"],
            counters["books"]))
    lines.append(
        "orphans: E={} D={} U={}  collisions={}  unknown={}".format(
            counters["orphan_executes"], counters["orphan_deletes"],
            counters["orphan_replaces"], counters["collisions"],
            counters["unknown"]))
    lines.append("trades={}  volume={}  on_tape={}".format(
        counters["executions"], counters["executed_volume"],
        counters["trades_on_tape"]))
    return "\n".join(lines) + "\n"


def main_stats(argv=None, out=None):
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(
        prog="jnxfeed stats",
        description="Message rates, session/seq, book/order/orphan counts. "
                    "Live sources refresh every --interval; file/pcap "
                    "sources print a final summary.",
    )
    sources.add_source_arguments(parser)
    parser.add_argument("--interval", type=float, default=1.0,
                        help="live refresh interval seconds (default 1.0)")
    args, code = _parse_or_usage(parser, argv)
    if args is None:
        return code

    market = Market()
    clear = _CLEAR if _is_tty(out) else ""
    prev = {"counts": {}, "time": time.monotonic()}

    def refresh(handler):
        now = time.monotonic()
        counts = dict(market.counters()["by_type"])
        dt = max(now - prev["time"], 1e-9)
        rates = dict(
            (k, (v - prev["counts"].get(k, 0)) / dt) for k, v in counts.items())
        prev["counts"] = counts
        prev["time"] = now
        out.write(clear + render_stats(market, rates=rates, handler=handler))
        if not clear:
            out.write("\n")
        _flush(out)

    start = time.monotonic()
    result, code = _run_source(args, out, market=market,
                               interval_cb=refresh, interval=args.interval)
    if code != EXIT_OK:
        return code
    if result.mode != "live":
        out.write(render_stats(market, elapsed=time.monotonic() - start))
    else:
        out.write(render_stats(market, handler=result.handler))
    return EXIT_OK


def _flush(out):
    flush = getattr(out, "flush", None)
    if flush is not None:
        flush()
