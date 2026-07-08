"""Declarative ITCH message field tables.

This is a direct transcription of JNX_PLAN.md section 3.2 (itself
byte-verified against the official Japannext UDP sample for
A/E/D/U/T/S/H/Y). Do not consult the spec PDFs — this module and the
plan's cheat sheet are the source of truth for field layout.

Each schema entry is a tuple of ``(name, size, ftype)`` triples describing
the wire layout *after* the 1-byte message-type character (offsets are
sequential from byte 0 = the type byte, per plan section 3.2). ``size`` is
in bytes; ``ftype`` is one of the field-type constants below.

Field types (plan section 3.1):
- ``NUM``   — unsigned big-endian integer, size 1/2/4/8.
- ``ALPHA`` — ASCII, left-justified, space-padded to ``size`` bytes.
- ``PRICE`` — unsigned big-endian 4-byte integer, 1 implied decimal;
  ``jnxfeed.types.NO_PRICE`` (0x7FFFFFFF) is the "no reference price"
  sentinel, valid only in reference-price `A` messages. Wire encoding is
  identical to NUM; the distinct tag exists for documentation and future
  validation, not because the codec treats it differently.

``codec.py`` (T2.2/T2.3) compiles these tables into ``struct.Struct``
instances; ``messages.py`` (T2.1) defines a matching
``typing.NamedTuple`` per type, fields in the same order as here.
"""
from collections import OrderedDict

# --- Field type tags ----------------------------------------------------

NUM = "num"
ALPHA = "alpha"
PRICE = "price"

FIELD_TYPES = (NUM, ALPHA, PRICE)


# --- Message schemas (plan section 3.2) ---------------------------------
#
# Order matches the table in JNX_PLAN.md section 3.2. `G` (End of
# Snapshot) is GLIMPSE-only but lives in the same table — the decoder is
# transport-agnostic and GLIMPSE reuses the ITCH message formats (plan
# section 3.5).

SCHEMAS = OrderedDict([
    ("T", (  # Timestamp - Seconds                                [len 5]
        ("seconds", 4, NUM),
    )),
    ("S", (  # System Event                                       [len 10]
        ("ns", 4, NUM),
        ("group", 4, ALPHA),
        ("event", 1, ALPHA),
    )),
    ("L", (  # Price Tick Size                                    [len 17]
        ("ns", 4, NUM),
        ("tick_table_id", 4, NUM),
        ("tick_size", 4, NUM),
        ("price_start", 4, PRICE),
    )),
    ("R", (  # Orderbook Directory                                [len 45]
        ("ns", 4, NUM),
        ("orderbook_id", 4, ALPHA),
        ("isin", 12, ALPHA),
        ("group", 4, ALPHA),
        ("round_lot", 4, NUM),
        ("tick_table_id", 4, NUM),
        ("price_decimals", 4, NUM),
        ("upper_limit", 4, PRICE),
        ("lower_limit", 4, PRICE),
    )),
    ("H", (  # Trading State                                      [len 14]
        ("ns", 4, NUM),
        ("orderbook_id", 4, ALPHA),
        ("group", 4, ALPHA),
        ("state", 1, ALPHA),
    )),
    ("Y", (  # Short Selling Price Restriction                    [len 14]
        ("ns", 4, NUM),
        ("orderbook_id", 4, ALPHA),
        ("group", 4, ALPHA),
        ("state", 1, ALPHA),
    )),
    ("A", (  # Order Added                                        [len 30]
        ("ns", 4, NUM),
        ("order_number", 8, NUM),
        ("side", 1, ALPHA),
        ("qty", 4, NUM),
        ("orderbook_id", 4, ALPHA),
        ("group", 4, ALPHA),
        ("price", 4, PRICE),
    )),
    ("F", (  # Order Added w/ Attributes                          [len 35]
        ("ns", 4, NUM),
        ("order_number", 8, NUM),
        ("side", 1, ALPHA),
        ("qty", 4, NUM),
        ("orderbook_id", 4, ALPHA),
        ("group", 4, ALPHA),
        ("price", 4, PRICE),
        ("attribution", 4, ALPHA),
        ("order_type", 1, ALPHA),
    )),
    ("E", (  # Order Executed                                     [len 25]
        ("ns", 4, NUM),
        ("order_number", 8, NUM),
        ("executed_qty", 4, NUM),
        ("match_number", 8, NUM),
    )),
    ("D", (  # Order Deleted                                      [len 13]
        ("ns", 4, NUM),
        ("order_number", 8, NUM),
    )),
    ("U", (  # Order Replaced                                     [len 29]
        ("ns", 4, NUM),
        ("orig_order_number", 8, NUM),
        ("new_order_number", 8, NUM),
        ("qty", 4, NUM),
        ("price", 4, PRICE),
    )),
    ("G", (  # End of Snapshot (GLIMPSE only)                     [len 9]
        ("sequence_number", 8, NUM),
    )),
])

#: Message type characters, in the order they appear in plan section 3.2.
MESSAGE_TYPES = tuple(SCHEMAS.keys())

#: Total wire length (plan section 3.2 `[len]` column), including the
#: 1-byte message type, verified by tests/unit/test_itch_schema.py.
LENGTHS = {
    "T": 5, "S": 10, "L": 17, "R": 45, "H": 14, "Y": 14,
    "A": 30, "F": 35, "E": 25, "D": 13, "U": 29, "G": 9,
}


def payload_length(msg_type):
    """Byte length of a message's fields, excluding the type byte."""
    return sum(size for _, size, _ in SCHEMAS[msg_type])


def total_length(msg_type):
    """Byte length of a whole wire message, including the type byte."""
    return 1 + payload_length(msg_type)


def field_names(msg_type):
    """Field names for a message type, in wire order."""
    return tuple(name for name, _, _ in SCHEMAS[msg_type])
