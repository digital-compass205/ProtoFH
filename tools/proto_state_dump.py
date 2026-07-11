"""Canonical state dump of the PROTOTYPE Market after replaying a .itch file.

Usage:  python3 tools/proto_state_dump.py <file.itch> <outdir>

This is the F2 parity reference: cpp/tools/book_dump.cpp writes the SAME
files byte-for-byte from the C++ port. Dev-only tool (runs on the dev box's
python3); imports the prototype package with the repo root on sys.path.

CANONICAL FORM (mirrored exactly by the C++ dumper — keep in sync):
- Every file has a header row; LF line endings; no timestamps/paths.
- All prices/quantities are raw integers (no floats, no division).
- None/unknown values render as the empty string; booleans as 0/1.
- Sort orders are explicit below; nothing relies on dict iteration order.

refdata.csv   one row per instrument, sorted by orderbook_id:
    orderbook_id,isin,group,round_lot,tick_table_id,price_decimals,
    upper_limit,lower_limit,trading_state,short_sell_state,
    reference_price,directory_missing
books.csv     per book (sorted by orderbook_id): bid levels best-first
              (price desc), then ask levels best-first (price asc), then
              one total row per side (B first). order_count is derived
              from the live-order store (the prototype's levels track qty
              only); group comes from refdata (may be empty):
    orderbook_id,group,kind,side,price,qty,order_count
      kind=level rows: price,qty,order_count of that level
      kind=total rows: price empty, qty=side total qty,
                       order_count=side live-order count
orders.csv    every live order sorted by order_number:
    order_number,orderbook_id,group,side,price,remaining_qty
trades.csv    one row per book that traded, sorted by orderbook_id
              (group from refdata; last_match_number tracked from the
              Executions returned by Market.apply — the prototype's
              BookStats does not retain it):
    orderbook_id,group,trade_count,cum_qty,cum_turnover,last_price,
    last_qty,last_match_number
stats.csv     key,value rows in the fixed order emitted below.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jnxfeed import itchfile
from jnxfeed.book.market import Market
from jnxfeed.itch import codec


def opt(value):
    """None -> empty string, everything else -> str."""
    return "" if value is None else str(value)


def dump(itch_path, outdir):
    market = Market()
    last_match = {}  # orderbook_id -> match_number of its last execution

    with open(itch_path, "rb") as f:
        for raw in itchfile.iter_messages(f):
            execution = market.apply(codec.decode(raw))
            if execution is not None:
                last_match[execution.orderbook_id] = execution.match_number

    os.makedirs(outdir, exist_ok=True)

    def group_of(orderbook_id):
        inst = market.refdata.instruments.get(orderbook_id)
        if inst is None or inst.group is None:
            return ""
        return inst.group

    # --- refdata.csv ------------------------------------------------------
    with open(os.path.join(outdir, "refdata.csv"), "w", newline="\n") as f:
        f.write("orderbook_id,isin,group,round_lot,tick_table_id,"
                "price_decimals,upper_limit,lower_limit,trading_state,"
                "short_sell_state,reference_price,directory_missing\n")
        for oid in sorted(market.refdata.instruments):
            inst = market.refdata.instruments[oid]
            f.write(",".join([
                inst.orderbook_id,
                opt(inst.isin),
                opt(inst.group),
                opt(inst.round_lot),
                opt(inst.tick_table_id),
                opt(inst.price_decimals),
                opt(inst.upper_limit),
                opt(inst.lower_limit),
                inst.trading_state,
                inst.short_sell_state,
                opt(inst.reference_price),
                "1" if inst.directory_missing else "0",
            ]) + "\n")

    # Per-(book, side, price) live order counts and per-(book, side)
    # totals, derived from the order store.
    level_orders = {}
    side_orders = {}
    for order in market.books.orders.values():
        lk = (order.orderbook_id, order.side, order.price)
        level_orders[lk] = level_orders.get(lk, 0) + 1
        sk = (order.orderbook_id, order.side)
        side_orders[sk] = side_orders.get(sk, 0) + 1

    # --- books.csv --------------------------------------------------------
    with open(os.path.join(outdir, "books.csv"), "w", newline="\n") as f:
        f.write("orderbook_id,group,kind,side,price,qty,order_count\n")
        for oid in sorted(market.books.books):
            book = market.books.books[oid]
            grp = group_of(oid)
            for side, levels in (("B", book.bid_levels()),
                                 ("S", book.ask_levels())):
                for price, qty in levels:
                    f.write("{},{},level,{},{},{},{}\n".format(
                        oid, grp, side, price, qty,
                        level_orders.get((oid, side, price), 0)))
            for side, sl in (("B", book.bids), ("S", book.asks)):
                f.write("{},{},total,{},,{},{}\n".format(
                    oid, grp, side, sl.total_qty(),
                    side_orders.get((oid, side), 0)))

    # --- orders.csv -------------------------------------------------------
    with open(os.path.join(outdir, "orders.csv"), "w", newline="\n") as f:
        f.write("order_number,orderbook_id,group,side,price,remaining_qty\n")
        for number in sorted(market.books.orders):
            o = market.books.orders[number]
            f.write("{},{},{},{},{},{}\n".format(
                o.order_number, o.orderbook_id, o.group, o.side, o.price,
                o.remaining_qty))

    # --- trades.csv -------------------------------------------------------
    with open(os.path.join(outdir, "trades.csv"), "w", newline="\n") as f:
        f.write("orderbook_id,group,trade_count,cum_qty,cum_turnover,"
                "last_price,last_qty,last_match_number\n")
        for oid in sorted(market.tape.stats):
            s = market.tape.stats[oid]
            f.write("{},{},{},{},{},{},{},{}\n".format(
                oid, group_of(oid), s.trade_count, s.volume, s.notional,
                opt(s.last_price), opt(s.last_qty),
                opt(last_match.get(oid))))

    # --- stats.csv --------------------------------------------------------
    auto_created = sum(1 for inst in market.refdata.instruments.values()
                       if inst.directory_missing)
    with open(os.path.join(outdir, "stats.csv"), "w", newline="\n") as f:
        f.write("key,value\n")
        rows = [("messages_applied", sum(market.message_counts.values())),
                ("unknown", market.unknown_count)]
        for char in sorted(market.message_counts):
            rows.append(("msg_" + char, market.message_counts[char]))
        rows += [
            ("instruments", len(market.refdata.instruments)),
            ("auto_created_books", auto_created),
            ("books", len(market.books.books)),
            ("live_orders", len(market.books.orders)),
            ("collisions", market.books.collisions),
            ("orphan_executes", market.books.orphan_executes),
            ("orphan_deletes", market.books.orphan_deletes),
            ("orphan_replaces", market.books.orphan_replaces),
            ("ref_price_ignored", market.books.ref_price_ignored),
            ("execution_count", market.books.execution_count),
            ("executed_volume", market.books.executed_volume),
            ("trade_count", market.tape.trade_count),
            ("total_volume", market.tape.total_volume),
        ]
        for key, value in rows:
            f.write("{},{}\n".format(key, value))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(
            "usage: python3 tools/proto_state_dump.py <file.itch> <outdir>\n")
        return 2
    dump(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
