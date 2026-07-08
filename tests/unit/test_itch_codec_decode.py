"""T2.2 acceptance tests: ITCH binary decoder.

Byte vectors for all 12 message types (incl. GLIMPSE-only `G`) live in
itch_samples.py and are shared with the T2.3 encode round-trip tests.
"""
import pytest

from jnxfeed import types
from jnxfeed.itch import codec, schema

from itch_samples import VECTORS


def test_decode_all_vectors():
    assert {v[0] for v in VECTORS} == set(schema.MESSAGE_TYPES)
    for msg_type, wire, expected in VECTORS:
        assert codec.decode(wire) == expected


def test_decode_returns_exact_expected_length_per_type():
    for msg_type, wire, _ in VECTORS:
        assert len(wire) == schema.total_length(msg_type)


def test_no_price_sentinel_decodes_as_no_price():
    # The reference-price A vector: order_number == 0, price == NO_PRICE.
    ref_price_vectors = [
        (t, w, m) for t, w, m in VECTORS if t == "A" and m.order_number == 0
    ]
    assert ref_price_vectors, "expected a reference-price A vector"
    _, wire, expected = ref_price_vectors[0]
    msg = codec.decode(wire)
    assert msg.price == types.NO_PRICE
    assert types.is_no_price(msg.price)
    assert msg == expected


def test_ordinary_price_is_not_mistaken_for_sentinel():
    ordinary = [
        (t, w, m) for t, w, m in VECTORS if t == "A" and m.order_number != 0
    ]
    assert ordinary
    _, wire, expected = ordinary[0]
    msg = codec.decode(wire)
    assert not types.is_no_price(msg.price)
    assert msg.price == expected.price


def test_truncated_input_raises_invalid_length():
    for msg_type, wire, _ in VECTORS:
        with pytest.raises(codec.InvalidMessageLength):
            codec.decode(wire[:-1])
        with pytest.raises(codec.DecodeError):
            codec.decode(wire[:-1])


def test_overlong_input_raises_invalid_length():
    for msg_type, wire, _ in VECTORS:
        with pytest.raises(codec.InvalidMessageLength):
            codec.decode(wire + b"\x00")


def test_empty_buffer_raises():
    with pytest.raises(codec.InvalidMessageLength):
        codec.decode(b"")


def test_unknown_message_type_raises_dedicated_exception():
    # 'Z' is not one of the 12 ITCH message types.
    with pytest.raises(codec.UnknownMessageType):
        codec.decode(b"Z\x00\x00\x00\x00")
    with pytest.raises(codec.DecodeError):
        codec.decode(b"Z\x00\x00\x00\x00")


def test_decode_accepts_bytearray_and_memoryview():
    _, wire, expected = VECTORS[0]
    assert codec.decode(bytearray(wire)) == expected
    assert codec.decode(memoryview(wire)) == expected


def test_alpha_field_strips_trailing_spaces_only():
    # H message with a 1-char state field padded... state fields here are
    # exactly 1 byte so there's nothing to strip; use S's 4-byte group
    # instead, and confirm a value with internal content but padding is
    # stripped only on the right.
    wire = b"H" + b"\x00\x00\x0b\xb8" + b"12  " + b"DAY " + b"T"
    msg = codec.decode(wire)
    assert msg.orderbook_id == "12"
    assert msg.group == "DAY"
