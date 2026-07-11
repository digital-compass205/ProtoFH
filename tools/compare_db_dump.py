#!/usr/bin/env python3
"""compare_db_dump.py — verify jnxdb state against a book_dump directory.

Dev-only tool (F5). Pulls jnxdb tables via its TCP query port and compares
the overlapping content against the canonical dump written by
cpp/build/book_dump (or tools/proto_state_dump.py):

  orders   full live-order table (number, ticker, side, price, remaining)
  books    per-ticker top-10 levels (price/qty/order_count) + side totals
  trades   cum_qty / cum_turnover / trade_count / last_price / last_qty /
           last_match_number for every traded ticker
  static   isin/round_lot/tick_table_id/price_decimals/limits/directory
  state    trading_state / short_sell_restriction / reference_price

Notes on the mapping (differences by design, not bugs):
- The dump keys books by ticker; the DB keys by (ticker, group). Samples
  carry one group per ticker, so DB rows are matched by ticker alone.
- DB rows exist for tickers the dump's refdata.csv does not know (books
  created purely by orders): those must show '?' states and zero statics.
- DB pseudo-rows with an empty ticker (system-wide S / L triggers) are
  skipped — the dump has no such concept.
- Dump "" (None) numerics correspond to DB zeros.

Usage: python3 tools/compare_db_dump.py [--host H] [--port P] <dump_dir>
Exit 0 only if every section matches.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dbquery import query  # tools/dbquery.py


def raw_price(text):
    """'1234.5' -> 12345; '-' -> None (no value)."""
    text = text.strip()
    if text == "-":
        return None
    if "." in text:
        whole, frac = text.split(".", 1)
        return int(whole) * 10 + int(frac)
    return int(text) * 10


def read_csv(path):
    with open(path) as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    header = lines[0].split(",")
    return [dict(zip(header, ln.split(","))) for ln in lines[1:]]


class Section(object):
    def __init__(self, name):
        self.name = name
        self.mismatches = []

    def check(self, cond, what):
        if not cond:
            self.mismatches.append(what)

    def report(self):
        if not self.mismatches:
            print("{:8s} OK".format(self.name))
            return True
        print("{:8s} MISMATCH ({} issues)".format(self.name,
                                                  len(self.mismatches)))
        for m in self.mismatches[:10]:
            print("    " + m)
        if len(self.mismatches) > 10:
            print("    ... and {} more".format(len(self.mismatches) - 10))
        return False


def db_table(host, port, name):
    lines = query(host, port, "TABLE " + name)
    header = lines[0].split(",")
    rows = {}
    for ln in lines[1:]:
        d = dict(zip(header, ln.split(",")))
        if not d.get("ticker"):
            continue  # pseudo-rows (system-wide triggers)
        rows[d["ticker"]] = d
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26401)
    parser.add_argument("dump_dir")
    args = parser.parse_args(argv)

    host, port = args.host, args.port
    ok = True

    dump_orders = read_csv(os.path.join(args.dump_dir, "orders.csv"))
    dump_books = read_csv(os.path.join(args.dump_dir, "books.csv"))
    dump_trades = read_csv(os.path.join(args.dump_dir, "trades.csv"))
    dump_refdata = read_csv(os.path.join(args.dump_dir, "refdata.csv"))

    db_static = db_table(host, port, "static")
    db_state = db_table(host, port, "state")
    db_trades = db_table(host, port, "trades")

    # ---- orders ------------------------------------------------------------
    sec = Section("orders")
    tickers = sorted(set(o["orderbook_id"] for o in dump_orders)
                     | set(db_static))
    want_by_ticker = {}
    for o in dump_orders:
        want_by_ticker.setdefault(o["orderbook_id"], {})[
            int(o["order_number"])] = (o["side"], int(o["price"]),
                                       int(o["remaining_qty"]))
    db_order_count = 0
    for ticker in tickers:
        lines = query(host, port, "ORDERS " + ticker)
        got = {}
        if lines and not lines[0].startswith("ERR"):
            for ln in lines[1:]:
                parts = ln.split()
                if len(parts) != 5:
                    continue
                got[int(parts[0])] = (parts[1], raw_price(parts[2]),
                                      int(parts[3]))
        db_order_count += len(got)
        want = want_by_ticker.get(ticker, {})
        if got != want:
            missing = sorted(set(want) - set(got))[:3]
            extra = sorted(set(got) - set(want))[:3]
            diff = [n for n in want if n in got and want[n] != got[n]][:3]
            sec.check(False, "{}: missing={} extra={} differing={}".format(
                ticker, missing, extra, diff))
    sec.check(db_order_count == len(dump_orders),
              "order count: db={} dump={}".format(db_order_count,
                                                  len(dump_orders)))
    ok = sec.report() and ok

    # ---- books (top-10 levels + totals) -------------------------------------
    sec = Section("books")
    # Expected: first 10 level rows per (ticker, side) + the total rows.
    want_levels = {}
    want_totals = {}
    for row in dump_books:
        key = (row["orderbook_id"], row["side"])
        if row["kind"] == "level":
            lst = want_levels.setdefault(key, [])
            if len(lst) < 10:
                lst.append((int(row["price"]), int(row["qty"]),
                            int(row["order_count"])))
        else:
            want_totals[key] = (int(row["qty"]), int(row["order_count"]))
    book_tickers = sorted(set(k[0] for k in want_totals))
    for ticker in book_tickers:
        lines = query(host, port, "BOOK " + ticker)
        if not lines or lines[0].startswith("ERR"):
            sec.check(False, "{}: no BOOK row in db".format(ticker))
            continue
        got_bids = []
        got_asks = []
        got_totals = {}
        for ln in lines:
            if ln.strip().startswith("totals:"):
                kv = dict(p.split("=") for p in ln.strip()[8:].split())
                got_totals[(ticker, "B")] = (int(kv["bid_qty"]),
                                             int(kv["bid_orders"]))
                got_totals[(ticker, "S")] = (int(kv["ask_qty"]),
                                             int(kv["ask_orders"]))
            elif "|" in ln and "bid_orders" not in ln:
                left, right = ln.split("|")
                lp = left.split()
                rp = right.split()
                if len(lp) == 3:
                    got_bids.append((raw_price(lp[2]), int(lp[1]),
                                     int(lp[0])))
                if len(rp) == 3:
                    got_asks.append((raw_price(rp[0]), int(rp[1]),
                                     int(rp[2])))
        sec.check(got_bids == want_levels.get((ticker, "B"), []),
                  "{}: bid levels differ".format(ticker))
        sec.check(got_asks == want_levels.get((ticker, "S"), []),
                  "{}: ask levels differ".format(ticker))
        for side in ("B", "S"):
            sec.check(
                got_totals.get((ticker, side)) == want_totals.get(
                    (ticker, side)),
                "{}: {} totals db={} dump={}".format(
                    ticker, side, got_totals.get((ticker, side)),
                    want_totals.get((ticker, side))))
    ok = sec.report() and ok

    # ---- trades --------------------------------------------------------------
    sec = Section("trades")
    dump_by_ticker = dict((r["orderbook_id"], r) for r in dump_trades)
    for ticker, r in sorted(dump_by_ticker.items()):
        d = db_trades.get(ticker)
        if d is None:
            sec.check(False, "{}: traded in dump, no db row".format(ticker))
            continue
        for dump_key, db_key in (("trade_count", "trade_count"),
                                 ("cum_qty", "cum_qty"),
                                 ("cum_turnover", "cum_turnover"),
                                 ("last_price", "last_price"),
                                 ("last_qty", "last_qty"),
                                 ("last_match_number", "last_match_number")):
            want_v = r[dump_key]
            want_v = int(want_v) if want_v else 0
            got_v = int(d[db_key])
            sec.check(got_v == want_v,
                      "{}: {} db={} dump={}".format(ticker, dump_key, got_v,
                                                    want_v))
    for ticker, d in sorted(db_trades.items()):
        if int(d["trade_count"]) > 0 and ticker not in dump_by_ticker:
            sec.check(False,
                      "{}: trades in db, none in dump".format(ticker))
    ok = sec.report() and ok

    # ---- static ---------------------------------------------------------------
    sec = Section("static")
    ref_by_ticker = dict((r["orderbook_id"], r) for r in dump_refdata)
    for ticker, d in sorted(db_static.items()):
        r = ref_by_ticker.get(ticker)
        if r is None:
            # Book known only through orders: statics must be absent.
            sec.check(d["directory_seen"] == "0" and d["isin"] == "" and
                      d["round_lot"] == "0",
                      "{}: db statics without dump refdata".format(ticker))
            continue
        sec.check(d["isin"] == r["isin"],
                  "{}: isin db={!r} dump={!r}".format(ticker, d["isin"],
                                                      r["isin"]))
        for k in ("round_lot", "tick_table_id", "price_decimals",
                  "upper_limit", "lower_limit"):
            want_v = int(r[k]) if r[k] else 0
            sec.check(int(d[k]) == want_v,
                      "{}: {} db={} dump={}".format(ticker, k, d[k], want_v))
        want_seen = "0" if r["directory_missing"] == "1" else "1"
        sec.check(d["directory_seen"] == want_seen,
                  "{}: directory_seen db={} dump-missing={}".format(
                      ticker, d["directory_seen"], r["directory_missing"]))
    ok = sec.report() and ok

    # ---- state ------------------------------------------------------------------
    sec = Section("state")
    for ticker, d in sorted(db_state.items()):
        r = ref_by_ticker.get(ticker)
        if r is None:
            sec.check(d["trading_state"] == "?" and
                      d["short_sell_restriction"] == "?" and
                      d["reference_price"] == "0",
                      "{}: expected unknown-yet states, got ts={} ssr={} "
                      "ref={}".format(ticker, d["trading_state"],
                                      d["short_sell_restriction"],
                                      d["reference_price"]))
            continue
        sec.check(d["trading_state"] == r["trading_state"],
                  "{}: trading_state db={} dump={}".format(
                      ticker, d["trading_state"], r["trading_state"]))
        sec.check(d["short_sell_restriction"] == r["short_sell_state"],
                  "{}: short_sell db={} dump={}".format(
                      ticker, d["short_sell_restriction"],
                      r["short_sell_state"]))
        want_ref = int(r["reference_price"]) if r["reference_price"] else 0
        sec.check(int(d["reference_price"]) == want_ref,
                  "{}: reference_price db={} dump={}".format(
                      ticker, d["reference_price"], want_ref))
    ok = sec.report() and ok

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
