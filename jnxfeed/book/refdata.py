"""Reference data store (JNX_PLAN.md T5.1a).

Consumes the decoded static/administrative ITCH messages -- `R` directory,
`L` tick sizes, `H` trading state, `Y` short-sell restriction, reference-
price `A` (order_number == 0, plan section 3.3(1)) and `S` system events --
and maintains the per-instrument static data table plus session events.

Absence semantics (plan section 3.3(4)): before the pre-open spins a book
absent from the `H` spin is SUSPENDED and a book absent from the `Y` spin
is unrestricted -- so those are the defaults for every instrument record
(jnxfeed.types.DEFAULT_TRADING_STATE / DEFAULT_SHORT_SELL_STATE).

Auto-create (plan section 3.3(5)): a capture/session joined mid-stream may
never deliver `R`/`L`; the first reference to an unknown orderbook id
creates a record flagged ``directory_missing`` so downstream consumers can
tell "known but sparse" from "properly announced".

Sans-I/O, zero policy beyond the semantics above; dispatch is on the
NamedTuple types from jnxfeed.itch.messages. Layering: this module may
import jnxfeed.types and jnxfeed.itch only (plan section 1).
"""
import bisect
from collections import OrderedDict

from jnxfeed import types
from jnxfeed.itch import messages as m


class Instrument(object):
    """Mutable static-data record for one order book."""

    __slots__ = (
        "orderbook_id", "isin", "group", "round_lot", "tick_table_id",
        "price_decimals", "upper_limit", "lower_limit",
        "trading_state", "short_sell_state", "reference_price",
        "directory_missing",
    )

    def __init__(self, orderbook_id):
        self.orderbook_id = orderbook_id
        self.isin = None
        self.group = None
        self.round_lot = None
        self.tick_table_id = None
        self.price_decimals = None
        self.upper_limit = None
        self.lower_limit = None
        # Absence semantics (plan section 3.3(4)).
        self.trading_state = types.DEFAULT_TRADING_STATE
        self.short_sell_state = types.DEFAULT_SHORT_SELL_STATE
        # None = no reference-price A seen yet; types.NO_PRICE = an A
        # explicitly said "no reference price".
        self.reference_price = None
        # True until an `R` directory message describes this book.
        self.directory_missing = True

    def __repr__(self):
        return "Instrument({!r}, group={!r}, directory_missing={})".format(
            self.orderbook_id, self.group, self.directory_missing
        )


class TickTable(object):
    """One tick-size table assembled from `L` rows.

    Each `L` carries (tick_table_id, tick_size, price_start): the tick
    size applies from ``price_start`` (inclusive) up to the next row's
    price_start (exclusive). Rows may arrive in any order.
    """

    __slots__ = ("tick_table_id", "_starts", "_sizes")

    def __init__(self, tick_table_id):
        self.tick_table_id = tick_table_id
        self._starts = []  # sorted price_start values
        self._sizes = []   # tick_size parallel to _starts

    def add(self, price_start, tick_size):
        i = bisect.bisect_left(self._starts, price_start)
        if i < len(self._starts) and self._starts[i] == price_start:
            self._sizes[i] = tick_size  # replace on duplicate start
        else:
            self._starts.insert(i, price_start)
            self._sizes.insert(i, tick_size)

    def tick_size(self, price):
        """Tick size in effect at ``price`` (raw int, 1 implied decimal).
        None if the price is below every row's start (or table empty)."""
        i = bisect.bisect_right(self._starts, price) - 1
        if i < 0:
            return None
        return self._sizes[i]

    def rows(self):
        """List of (price_start, tick_size) sorted by price_start."""
        return list(zip(self._starts, self._sizes))

    def __len__(self):
        return len(self._starts)


class RefData(object):
    """The static data table plus session events."""

    def __init__(self):
        self.instruments = OrderedDict()  # orderbook_id -> Instrument
        self.tick_tables = {}             # tick_table_id -> TickTable
        self.system_events = []           # (ns, group, event) in arrival order

    # -- lookup --------------------------------------------------------------

    def get(self, orderbook_id):
        """Return the Instrument for ``orderbook_id``, auto-creating a
        ``directory_missing`` record on first reference (plan 3.3(5))."""
        inst = self.instruments.get(orderbook_id)
        if inst is None:
            inst = Instrument(orderbook_id)
            self.instruments[orderbook_id] = inst
        return inst

    def tick_table(self, tick_table_id):
        table = self.tick_tables.get(tick_table_id)
        if table is None:
            table = TickTable(tick_table_id)
            self.tick_tables[tick_table_id] = table
        return table

    def tick_size(self, orderbook_id, price):
        """Tick size for ``orderbook_id`` at ``price``; None if unknown."""
        inst = self.instruments.get(orderbook_id)
        if inst is None or inst.tick_table_id is None:
            return None
        table = self.tick_tables.get(inst.tick_table_id)
        if table is None:
            return None
        return table.tick_size(price)

    # -- message application ---------------------------------------------------

    def apply(self, msg):
        """Apply one decoded message. Returns True if it was consumed,
        False if the type is not a refdata concern (caller routes it
        elsewhere). A book-order `A`/`F` is never consumed here -- only a
        reference-price `A` (order_number == 0)."""
        cls = type(msg)
        if cls is m.OrderbookDirectory:
            inst = self.get(msg.orderbook_id)
            inst.isin = msg.isin
            inst.group = msg.group
            inst.round_lot = msg.round_lot
            inst.tick_table_id = msg.tick_table_id
            inst.price_decimals = msg.price_decimals
            inst.upper_limit = msg.upper_limit
            inst.lower_limit = msg.lower_limit
            inst.directory_missing = False
            return True
        if cls is m.PriceTickSize:
            self.tick_table(msg.tick_table_id).add(msg.price_start, msg.tick_size)
            return True
        if cls is m.TradingState:
            inst = self.get(msg.orderbook_id)
            if inst.group is None:
                inst.group = msg.group
            inst.trading_state = msg.state
            return True
        if cls is m.ShortSellRestriction:
            inst = self.get(msg.orderbook_id)
            if inst.group is None:
                inst.group = msg.group
            inst.short_sell_state = msg.state
            return True
        if cls is m.SystemEvent:
            self.system_events.append((msg.ns, msg.group, msg.event))
            return True
        if cls is m.OrderAdded and msg.order_number == 0:
            # Reference-price update (plan 3.3(1)): NOT an order; price may
            # be the NO_PRICE sentinel; side/qty are meaningless.
            inst = self.get(msg.orderbook_id)
            if inst.group is None:
                inst.group = msg.group
            inst.reference_price = msg.price
            return True
        return False

    @staticmethod
    def is_reference_price(msg):
        """True if ``msg`` is a reference-price `A` (order_number == 0)."""
        return type(msg) is m.OrderAdded and msg.order_number == 0
