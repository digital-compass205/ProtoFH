"""Order store + per-book price-level book builder (JNX_PLAN.md T5.1b).

Implements the plan section 3.3 semantics:

- `A`/`F` insert an order into the store and its book's price levels.
  A reference-price `A` (order_number == 0) is NOT an order (3.3(1)) and
  is ignored here (counted, so misrouting is visible) -- it belongs to
  refdata.
- `E`/`D`/`U` carry no orderbook id, side, or price (3.3(2)): the store
  keyed by order number holds (orderbook_id, group, side, price,
  remaining_qty). `E` reduces remaining qty cumulatively and removes the
  order at zero; the trade price is the PASSIVE order's stored price. `U`
  removes the original order and inserts the new one inheriting
  book/side/group from the original, with price/qty from the message.
- Order numbers are unique per day per order book group (3.3(3)) but
  `E`/`D`/`U` don't carry the group, so a combined feed could collide:
  we key by order number alone, keep the group in the record, and count +
  WARN on any collision.
- `E`/`D`/`U` referencing an unknown order number (e.g. orders created
  before a mid-session join) are counted as orphans per type and
  otherwise ignored.

Executions are surfaced as :class:`Execution` events returned from
:meth:`OrderBookStore.apply` (and also via an optional ``on_execution``
callback) carrying everything the trade tape (T5.1c) needs, so no
consumer has to recompute passive-order attributes.

Sans-I/O; imports only jnxfeed.itch and stdlib (plan section 1 layering).
"""
import logging
import typing
from bisect import bisect_left, insort

from jnxfeed.itch import messages as m

logger = logging.getLogger(__name__)

BUY = "B"
SELL = "S"


class Execution(typing.NamedTuple):
    """One fill, derived from an `E` against the stored passive order.

    ``side`` is the PASSIVE order's side; ``price`` its stored price
    (plan 3.3(2): "the trade price is the passive order's stored price").
    """
    orderbook_id: str
    group: str
    side: str
    price: int
    qty: int
    match_number: int


class Order(object):
    """Mutable stored order record."""

    __slots__ = ("order_number", "orderbook_id", "group", "side", "price",
                 "remaining_qty")

    def __init__(self, order_number, orderbook_id, group, side, price, qty):
        self.order_number = order_number
        self.orderbook_id = orderbook_id
        self.group = group
        self.side = side
        self.price = price
        self.remaining_qty = qty

    def __repr__(self):
        return "Order(#{} {} {} {}@{} rem={})".format(
            self.order_number, self.orderbook_id, self.group, self.side,
            self.price, self.remaining_qty
        )


class _SideLevels(object):
    """Aggregated qty per price for one side of one book.

    Prices are kept in an ascending sorted list (insort/bisect); the
    qty aggregation lives in a parallel dict. Top-N views reverse for
    bids so callers always see best-first.
    """

    __slots__ = ("_prices", "_qty")

    def __init__(self):
        self._prices = []  # ascending
        self._qty = {}     # price -> aggregate qty

    def add(self, price, qty):
        cur = self._qty.get(price)
        if cur is None:
            self._qty[price] = qty
            insort(self._prices, price)
        else:
            self._qty[price] = cur + qty

    def remove(self, price, qty):
        cur = self._qty[price] - qty
        if cur < 0:
            raise ValueError(
                "level {} would go negative ({} - {})".format(price, cur + qty, qty)
            )
        if cur == 0:
            del self._qty[price]
            i = bisect_left(self._prices, price)
            del self._prices[i]
        else:
            self._qty[price] = cur

    def levels_ascending(self):
        qty = self._qty
        return [(p, qty[p]) for p in self._prices]

    def qty_at(self, price):
        return self._qty.get(price, 0)

    def total_qty(self):
        return sum(self._qty.values())

    def __len__(self):
        return len(self._prices)


class Book(object):
    """Price-aggregated levels for one order book (both sides)."""

    __slots__ = ("orderbook_id", "bids", "asks")

    def __init__(self, orderbook_id):
        self.orderbook_id = orderbook_id
        self.bids = _SideLevels()
        self.asks = _SideLevels()

    def _side(self, side):
        return self.bids if side == BUY else self.asks

    def add(self, side, price, qty):
        self._side(side).add(price, qty)

    def remove(self, side, price, qty):
        self._side(side).remove(price, qty)

    def bid_levels(self, depth=None):
        """[(price, qty)] best (highest) first."""
        levels = self.bids.levels_ascending()
        levels.reverse()
        return levels if depth is None else levels[:depth]

    def ask_levels(self, depth=None):
        """[(price, qty)] best (lowest) first."""
        levels = self.asks.levels_ascending()
        return levels if depth is None else levels[:depth]

    def top(self, depth):
        """(bids best-first, asks best-first), each up to ``depth`` levels."""
        return self.bid_levels(depth), self.ask_levels(depth)

    def best_bid(self):
        levels = self.bid_levels(1)
        return levels[0] if levels else None

    def best_ask(self):
        levels = self.ask_levels(1)
        return levels[0] if levels else None


class OrderBookStore(object):
    """Order-number-keyed store plus per-book aggregated price levels.

    ``apply(msg)`` consumes `A`/`F`/`E`/`D`/`U`; returns an
    :class:`Execution` for an `E` matched against a known order, else
    None. Everything else returns None untouched (returns-False dispatch
    is Market's job; passing a non-book message here is a no-op).
    """

    def __init__(self, on_execution=None):
        self.orders = {}   # order_number -> Order
        self.books = {}    # orderbook_id -> Book
        self.on_execution = on_execution

        # Diagnostics (plan 3.3(3)/(5) and mid-session joins).
        self.collisions = 0          # A/F/U(new) with an already-live number
        self.orphan_executes = 0     # E referencing an unknown order number
        self.orphan_deletes = 0      # D referencing an unknown order number
        self.orphan_replaces = 0     # U referencing an unknown orig number
        self.ref_price_ignored = 0   # ref-price A misrouted here
        self.executed_volume = 0     # total qty across all Executions
        self.execution_count = 0

    # -- lookup -----------------------------------------------------------

    def book(self, orderbook_id):
        book = self.books.get(orderbook_id)
        if book is None:
            book = Book(orderbook_id)
            self.books[orderbook_id] = book
        return book

    def orphans_total(self):
        return self.orphan_executes + self.orphan_deletes + self.orphan_replaces

    def live_order_count(self):
        return len(self.orders)

    # -- message application ---------------------------------------------------

    def apply(self, msg):
        cls = type(msg)
        if cls is m.OrderAdded or cls is m.OrderAddedWithAttributes:
            if msg.order_number == 0:
                # Reference-price pseudo-order (plan 3.3(1)) -- refdata's
                # business, never the book's. Count so misrouting shows up.
                self.ref_price_ignored += 1
                return None
            self._insert(msg.order_number, msg.orderbook_id, msg.group,
                         msg.side, msg.price, msg.qty)
            return None
        if cls is m.OrderExecuted:
            return self._execute(msg)
        if cls is m.OrderDeleted:
            self._delete(msg)
            return None
        if cls is m.OrderReplaced:
            self._replace(msg)
            return None
        return None

    # -- internals -----------------------------------------------------------------

    def _insert(self, order_number, orderbook_id, group, side, price, qty):
        existing = self.orders.get(order_number)
        if existing is not None:
            # Plan 3.3(3): theoretically possible cross-group collision on
            # a combined feed. Count + warn, and replace the stale record
            # (leaving both would corrupt the levels forever).
            self.collisions += 1
            logger.warning(
                "order number collision: #%d already live (%r), replacing "
                "with %s %s %s@%d", order_number, existing, orderbook_id,
                side, qty, price,
            )
            self._remove_order(existing)
        order = Order(order_number, orderbook_id, group, side, price, qty)
        self.orders[order_number] = order
        self.book(orderbook_id).add(side, price, qty)

    def _remove_order(self, order):
        del self.orders[order.order_number]
        self.book(order.orderbook_id).remove(order.side, order.price,
                                             order.remaining_qty)

    def _execute(self, msg):
        order = self.orders.get(msg.order_number)
        if order is None:
            self.orphan_executes += 1
            return None
        qty = msg.executed_qty
        if qty > order.remaining_qty:
            # Should not happen on a clean feed; clamp so levels never go
            # negative, and make it loud.
            logger.warning(
                "execution of %d exceeds remaining %d on %r; clamping",
                qty, order.remaining_qty, order,
            )
            qty = order.remaining_qty
        order.remaining_qty -= qty
        self.book(order.orderbook_id).remove(order.side, order.price, qty)
        if order.remaining_qty == 0:
            del self.orders[order.order_number]
        execution = Execution(
            orderbook_id=order.orderbook_id,
            group=order.group,
            side=order.side,
            price=order.price,
            qty=qty,
            match_number=msg.match_number,
        )
        self.executed_volume += qty
        self.execution_count += 1
        if self.on_execution is not None:
            self.on_execution(execution)
        return execution

    def _delete(self, msg):
        order = self.orders.get(msg.order_number)
        if order is None:
            self.orphan_deletes += 1
            return
        self._remove_order(order)

    def _replace(self, msg):
        orig = self.orders.get(msg.orig_order_number)
        if orig is None:
            # Without the original we know neither book nor side, so the
            # new order cannot be placed either (plan 3.3(2)).
            self.orphan_replaces += 1
            return
        self._remove_order(orig)
        self._insert(msg.new_order_number, orig.orderbook_id, orig.group,
                     orig.side, msg.price, msg.qty)
