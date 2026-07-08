"""T2.3 acceptance tests: ITCH binary encoder.

Shares byte vectors with the T2.2 decode tests (itch_samples.py). The
core acceptance criterion is `decode(encode(m)) == m` for every message
type; we also check `encode(decode(wire)) == wire` (exact byte
round-trip, including alpha re-padding) against the hand-crafted wire
vectors.
"""
import pytest

from jnxfeed.itch import codec, schema

from itch_samples import VECTORS


def test_decode_encode_round_trip_every_type():
    seen_types = set()
    for msg_type, wire, expected in VECTORS:
        msg = codec.decode(wire)
        assert codec.decode(codec.encode(msg)) == msg
        seen_types.add(msg_type)
    assert seen_types == set(schema.MESSAGE_TYPES)


def test_encode_decode_round_trip_from_expected_namedtuple():
    for msg_type, wire, expected in VECTORS:
        assert codec.decode(codec.encode(expected)) == expected


def test_encode_reproduces_exact_wire_bytes():
    # Our hand-crafted vectors already use canonical space-padding, so
    # encode(decode(wire)) must reproduce them byte-for-byte.
    for msg_type, wire, expected in VECTORS:
        assert codec.encode(expected) == wire


def test_encode_pads_short_alpha_with_trailing_spaces():
    from jnxfeed.itch import messages

    msg = messages.OrderbookDirectory(
        ns=1, orderbook_id="99", isin="JP1", group="DAY",
        round_lot=100, tick_table_id=1, price_decimals=1,
        upper_limit=100, lower_limit=1,
    )
    wire = codec.encode(msg)
    assert wire[5:9] == b"99  "          # orderbook_id, 4 bytes
    assert wire[9:21] == b"JP1         "  # isin, 12 bytes
    assert codec.decode(wire) == msg


def test_encode_rejects_alpha_value_too_wide():
    from jnxfeed.itch import messages

    msg = messages.TradingState(
        ns=1, orderbook_id="TOOLONG", group="DAY", state="T"
    )
    with pytest.raises(codec.AlphaFieldTooLong):
        codec.encode(msg)
    with pytest.raises(codec.EncodeError):
        codec.encode(msg)


def test_encode_rejects_unknown_message_class():
    with pytest.raises(codec.UnknownMessageClass):
        codec.encode(("not", "a", "message"))
    with pytest.raises(codec.EncodeError):
        codec.encode(object())


def test_encode_preserves_no_price_sentinel():
    from jnxfeed import types
    from jnxfeed.itch import messages

    msg = messages.OrderAdded(
        ns=1, order_number=0, side="B", qty=0,
        orderbook_id="8306", group="DAY", price=types.NO_PRICE,
    )
    round_tripped = codec.decode(codec.encode(msg))
    assert round_tripped.price == types.NO_PRICE
    assert round_tripped == msg
