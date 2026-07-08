"""Hand-crafted ITCH wire-format byte vectors, shared by the decode
tests (T2.2) and the encode round-trip tests (T2.3).

Each entry is `(msg_type, wire_bytes, expected_namedtuple)`. Byte
layouts follow JNX_PLAN.md section 3.2 exactly; values are arbitrary but
internally consistent (e.g. price fields include the 1-implied-decimal
scaling, alpha fields are pre-padded with trailing spaces as they'd
appear on the wire).
"""
import struct

from jnxfeed import types
from jnxfeed.itch import messages

VECTORS = []


def _vec(msg_type, parts, expected):
    wire = msg_type.encode("ascii") + b"".join(parts)
    VECTORS.append((msg_type, wire, expected))


def _u(fmt, value):
    return struct.pack(">" + fmt, value)


def _a(text, size):
    data = text.encode("ascii")
    assert len(data) <= size
    return data + b" " * (size - len(data))


# T — Timestamp - Seconds [len 5]
_vec(
    "T",
    [_u("I", 34200)],
    messages.TimestampSeconds(seconds=34200),
)

# S — System Event [len 10], group present
_vec(
    "S",
    [_u("I", 123456789), _a("DAY", 4), _a("O", 1)],
    messages.SystemEvent(ns=123456789, group="DAY", event="O"),
)

# S — System Event [len 10], blank group (system-wide, plan 3.2)
_vec(
    "S",
    [_u("I", 5), _a("", 4), _a("C", 1)],
    messages.SystemEvent(ns=5, group="", event="C"),
)

# L — Price Tick Size [len 17]
_vec(
    "L",
    [_u("I", 1000), _u("I", 7), _u("I", 1), _u("I", 12345)],
    messages.PriceTickSize(ns=1000, tick_table_id=7, tick_size=1, price_start=12345),
)

# R — Orderbook Directory [len 45]
_vec(
    "R",
    [
        _u("I", 2000),
        _a("8306", 4),
        _a("JP3435000009", 12),
        _a("DAY", 4),
        _u("I", 100),
        _u("I", 7),
        _u("I", 1),
        _u("I", 999999),
        _u("I", 1),
    ],
    messages.OrderbookDirectory(
        ns=2000,
        orderbook_id="8306",
        isin="JP3435000009",
        group="DAY",
        round_lot=100,
        tick_table_id=7,
        price_decimals=1,
        upper_limit=999999,
        lower_limit=1,
    ),
)

# H — Trading State [len 14]
_vec(
    "H",
    [_u("I", 3000), _a("8306", 4), _a("DAY", 4), _a("T", 1)],
    messages.TradingState(ns=3000, orderbook_id="8306", group="DAY", state="T"),
)

# Y — Short Selling Price Restriction [len 14]
_vec(
    "Y",
    [_u("I", 3100), _a("8306", 4), _a("DAY", 4), _a("0", 1)],
    messages.ShortSellRestriction(
        ns=3100, orderbook_id="8306", group="DAY", state="0"
    ),
)

# A — Order Added [len 30], ordinary order
_vec(
    "A",
    [
        _u("I", 555),
        _u("Q", 1001),
        _a("B", 1),
        _u("I", 500),
        _a("8306", 4),
        _a("DAY", 4),
        _u("I", 12345),
    ],
    messages.OrderAdded(
        ns=555, order_number=1001, side="B", qty=500,
        orderbook_id="8306", group="DAY", price=12345,
    ),
)

# A — Order Added [len 30], reference-price update: order_number == 0,
# price == NO_PRICE sentinel (plan 3.3(1), 3.1).
_vec(
    "A",
    [
        _u("I", 556),
        _u("Q", 0),
        _a("B", 1),
        _u("I", 0),
        _a("8306", 4),
        _a("DAY", 4),
        _u("I", types.NO_PRICE),
    ],
    messages.OrderAdded(
        ns=556, order_number=0, side="B", qty=0,
        orderbook_id="8306", group="DAY", price=types.NO_PRICE,
    ),
)

# F — Order Added w/ Attributes [len 35]
_vec(
    "F",
    [
        _u("I", 557),
        _u("Q", 1002),
        _a("S", 1),
        _u("I", 200),
        _a("8306", 4),
        _a("DAY", 4),
        _u("I", 12300),
        _a("", 4),
        _a("Q", 1),
    ],
    messages.OrderAddedWithAttributes(
        ns=557, order_number=1002, side="S", qty=200,
        orderbook_id="8306", group="DAY", price=12300,
        attribution="", order_type="Q",
    ),
)

# E — Order Executed [len 25]
_vec(
    "E",
    [_u("I", 600), _u("Q", 1001), _u("I", 100), _u("Q", 99999)],
    messages.OrderExecuted(
        ns=600, order_number=1001, executed_qty=100, match_number=99999
    ),
)

# D — Order Deleted [len 13]
_vec(
    "D",
    [_u("I", 700), _u("Q", 1001)],
    messages.OrderDeleted(ns=700, order_number=1001),
)

# U — Order Replaced [len 29]
_vec(
    "U",
    [_u("I", 800), _u("Q", 1001), _u("Q", 1050), _u("I", 300), _u("I", 12400)],
    messages.OrderReplaced(
        ns=800, orig_order_number=1001, new_order_number=1050,
        qty=300, price=12400,
    ),
)

# G — End of Snapshot (GLIMPSE only) [len 9], binary seq (NOT ASCII)
_vec(
    "G",
    [_u("Q", 234752)],
    messages.EndOfSnapshot(sequence_number=234752),
)
