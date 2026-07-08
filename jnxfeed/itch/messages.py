"""ITCH message NamedTuples — one per type in schema.SCHEMAS.

Field names and order match schema.py exactly (asserted by
tests/unit/test_itch_schema.py). Plain `typing.NamedTuple` class syntax
(PEP 526 variable annotations, 3.6.1+-safe) is used deliberately: it is
immutable and lightweight, unlike `dataclasses` which is forbidden on
this target (plan section 0).

All fields are decoded scalars: `int` for num/price fields (prices are
plain ints with 1 implied decimal — see jnxfeed.types), stripped `str`
for alpha fields. The codec (codec.py) is the only place wire bytes are
produced or consumed; these classes carry no behavior.
"""
import typing
from collections import OrderedDict


class TimestampSeconds(typing.NamedTuple):
    """`T` — Timestamp - Seconds."""
    seconds: int


class SystemEvent(typing.NamedTuple):
    """`S` — System Event."""
    ns: int
    group: str
    event: str


class PriceTickSize(typing.NamedTuple):
    """`L` — Price Tick Size."""
    ns: int
    tick_table_id: int
    tick_size: int
    price_start: int


class OrderbookDirectory(typing.NamedTuple):
    """`R` — Orderbook Directory."""
    ns: int
    orderbook_id: str
    isin: str
    group: str
    round_lot: int
    tick_table_id: int
    price_decimals: int
    upper_limit: int
    lower_limit: int


class TradingState(typing.NamedTuple):
    """`H` — Trading State."""
    ns: int
    orderbook_id: str
    group: str
    state: str


class ShortSellRestriction(typing.NamedTuple):
    """`Y` — Short Selling Price Restriction."""
    ns: int
    orderbook_id: str
    group: str
    state: str


class OrderAdded(typing.NamedTuple):
    """`A` — Order Added.

    A record with order_number == 0 is a reference-price update, not a
    real order (plan section 3.3(1)): side/qty are meaningless and
    `price` may be `jnxfeed.types.NO_PRICE`.
    """
    ns: int
    order_number: int
    side: str
    qty: int
    orderbook_id: str
    group: str
    price: int


class OrderAddedWithAttributes(typing.NamedTuple):
    """`F` — Order Added w/ Attributes (same book handling as `A`)."""
    ns: int
    order_number: int
    side: str
    qty: int
    orderbook_id: str
    group: str
    price: int
    attribution: str
    order_type: str


class OrderExecuted(typing.NamedTuple):
    """`E` — Order Executed."""
    ns: int
    order_number: int
    executed_qty: int
    match_number: int


class OrderDeleted(typing.NamedTuple):
    """`D` — Order Deleted."""
    ns: int
    order_number: int


class OrderReplaced(typing.NamedTuple):
    """`U` — Order Replaced."""
    ns: int
    orig_order_number: int
    new_order_number: int
    qty: int
    price: int


class EndOfSnapshot(typing.NamedTuple):
    """`G` — End of Snapshot (GLIMPSE only)."""
    sequence_number: int


#: Message type character -> NamedTuple class, same order as schema.SCHEMAS.
MESSAGE_CLASSES = OrderedDict([
    ("T", TimestampSeconds),
    ("S", SystemEvent),
    ("L", PriceTickSize),
    ("R", OrderbookDirectory),
    ("H", TradingState),
    ("Y", ShortSellRestriction),
    ("A", OrderAdded),
    ("F", OrderAddedWithAttributes),
    ("E", OrderExecuted),
    ("D", OrderDeleted),
    ("U", OrderReplaced),
    ("G", EndOfSnapshot),
])
