"""Trade tape (JNX_PLAN.md T5.1c).

Consumes the :class:`jnxfeed.book.orderbook.Execution` events produced by
the order store (never raw `E` messages -- the store already resolved the
passive order's book/side/price) and maintains:

- a rolling, bounded tape of the most recent trades as
  :class:`TapeEntry` records (timestamp in nanoseconds past midnight of
  the session start day, i.e. the `T` seconds clock * 1e9 + the message
  ns field -- supplied by the caller, normally Market);
- per-book cumulative statistics: trade count, total volume, notional,
  VWAP and last price/qty.

Note on multi-fill orders: each `E` (one match) yields exactly one tape
entry; several fills of one resting order arrive as separate `E` messages
with distinct match numbers and are counted separately.
"""
import typing
from collections import deque

from jnxfeed import types

#: Default rolling-tape capacity (entries), overridable per instance.
DEFAULT_MAX_ENTRIES = 10000

_NS_PER_SECOND = 1000000000


class TapeEntry(typing.NamedTuple):
    """One trade on the tape. ``timestamp`` is int nanoseconds past
    midnight of the session start day."""
    timestamp: int
    orderbook_id: str
    price: int
    qty: int
    match_number: int


class BookStats(object):
    """Cumulative per-book trade statistics."""

    __slots__ = ("orderbook_id", "trade_count", "volume", "notional",
                 "last_price", "last_qty", "uptick")

    def __init__(self, orderbook_id):
        self.orderbook_id = orderbook_id
        self.trade_count = 0
        self.volume = 0
        self.notional = 0   # sum(price * qty), raw price units
        self.last_price = None
        self.last_qty = None
        # Short-sell uptick-rule classification (JNX_Short_Selling_Rules_
        # 2.00): a "zero/plus/minus tick" test, not a per-trade comparison.
        # Only a trade whose price actually differs from ``last_price``
        # changes this -- True on a plus tick (new price higher), False on
        # a minus tick (lower); a repeat print at the same price ("zero
        # tick") leaves it exactly as-is. Defaults False: "beginning of
        # trading day" is itself a flat/non-uptick state per the rule.
        self.uptick = False

    def vwap(self):
        """Volume-weighted average price in raw price units (1 implied
        decimal), or None before the first trade."""
        if self.volume == 0:
            return None
        return self.notional / self.volume


def make_timestamp(seconds, ns):
    """Combine the `T` clock (seconds past midnight) with a message's ns
    field into one int nanosecond timestamp."""
    return seconds * _NS_PER_SECOND + ns


class TradeTape(object):
    """Rolling tape + per-book cumulative stats."""

    def __init__(self, max_entries=DEFAULT_MAX_ENTRIES):
        self.entries = deque(maxlen=max_entries)
        self.stats = {}          # orderbook_id -> BookStats
        self.trade_count = 0     # total, unaffected by the rolling bound
        self.total_volume = 0

    def record(self, execution, timestamp, base_price=None):
        """Record one Execution at ``timestamp`` (int ns past midnight).

        ``base_price`` is the book's reference/base price (raw int, or
        None/NO_PRICE if unknown) -- used only as the "assumed last traded
        price" for the short-sell uptick-rule tick test when this is the
        book's first trade of the day; ignored once a trade has already
        been recorded for this book. Returns the TapeEntry appended.
        """
        entry = TapeEntry(
            timestamp=timestamp,
            orderbook_id=execution.orderbook_id,
            price=execution.price,
            qty=execution.qty,
            match_number=execution.match_number,
        )
        self.entries.append(entry)
        self.trade_count += 1
        self.total_volume += execution.qty

        stats = self.stats.get(execution.orderbook_id)
        if stats is None:
            stats = BookStats(execution.orderbook_id)
            self.stats[execution.orderbook_id] = stats
        stats.trade_count += 1
        stats.volume += execution.qty
        stats.notional += execution.price * execution.qty

        # Short-sell uptick-rule zero/plus/minus tick test (see
        # BookStats.uptick doc above): compare against the effective last
        # traded price -- the real last_price once this book has traded
        # today, else the assumed base price -- before overwriting it.
        effective_ltp = stats.last_price
        if effective_ltp is None:
            effective_ltp = base_price
        if effective_ltp is not None and effective_ltp != types.NO_PRICE:
            if execution.price > effective_ltp:
                stats.uptick = True
            elif execution.price < effective_ltp:
                stats.uptick = False
            # execution.price == effective_ltp (zero tick): unchanged.
        # else: no trade yet today AND no base price known -- classification
        # cannot be determined; leave stats.uptick at its current value.

        stats.last_price = execution.price
        stats.last_qty = execution.qty
        return entry

    def book_stats(self, orderbook_id):
        """BookStats for ``orderbook_id`` or None if it never traded."""
        return self.stats.get(orderbook_id)

    def recent(self, n=None, orderbook_id=None):
        """Most recent ``n`` tape entries (newest last), optionally
        filtered to one book. ``n=None`` returns everything retained."""
        if orderbook_id is None:
            entries = list(self.entries)
        else:
            entries = [e for e in self.entries if e.orderbook_id == orderbook_id]
        if n is not None:
            entries = entries[-n:]
        return entries
