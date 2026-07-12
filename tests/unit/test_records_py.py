"""Cross-language golden test for the JNX record codec (Phase F3).

cpp/tools/gen_record_vectors.cpp (C++ encoder) wrote
cpp/test/vectors/records.bin with fixed, deterministic values; this test
decodes it with the pure-Python decoder (jnxweb/records.py) and asserts
every field of every record. The expected values below are hardcoded
mirrors of the generator source — change them only together.

Layout contract: docs/wire_spec.md version 2 (FROZEN).
"""
import os
import re

import pytest

from jnxweb import records

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
VECTORS_BIN = os.path.join(REPO_ROOT, "cpp", "test", "vectors", "records.bin")
WIRE_SPEC = os.path.join(REPO_ROOT, "docs", "wire_spec.md")

NO_PRICE = 0x7FFFFFFF


@pytest.fixture(scope="module")
def recs():
    with open(VECTORS_BIN, "rb") as f:
        data = f.read()
    return records.decode_stream(data)


def test_record_count_and_kind_order(recs):
    assert [r["kind"] for r in recs] == [
        "H", "G", "R", "B", "K", "O", "U", "U", "U", "E",
    ]


def test_hello(recs):
    assert recs[0] == {"kind": "H", "epoch": 7, "last_exch_seq": 12561}


def test_control_records_have_no_fields(recs):
    assert recs[1] == {"kind": "G"}
    assert recs[2] == {"kind": "R"}
    assert recs[3] == {"kind": "B"}


def test_tick(recs):
    assert recs[4] == {
        "kind": "K", "table_id": 1, "price_start": 30000, "tick_size": 5,
    }


def test_order(recs):
    assert recs[5] == {
        "kind": "O",
        "order_number": 990001,
        "ticker": "8306",
        "group": "DAY",
        "side": "B",
        "price": 15000,
        "qty_remaining": 400,
        "order_type": "Q",
    }


def test_update_full(recs):
    u = recs[6]
    assert u["kind"] == "U"
    # envelope
    assert u["epoch"] == 7
    assert u["pub_seq"] == 123456
    assert u["session"] == "1697659284"
    assert u["exch_seq"] == 234751
    assert u["exch_ns"] == 1234567890123456789
    assert u["trigger"] == "U"
    assert u["ticker"] == "8306"
    assert u["group"] == "DAY"
    # static
    assert u["isin"] == "JP3902400005"
    assert u["round_lot"] == 100
    assert u["tick_table_id"] == 1
    assert u["price_decimals"] == 1
    assert u["upper_limit"] == 200000
    assert u["lower_limit"] == 100000
    assert u["flags"] == (
        records.FLAG_DIRECTORY_SEEN | records.FLAG_ORDER_COLLISION_SEEN
    )
    # state
    assert u["trading_state"] == "T"
    assert u["short_sell_restriction"] == "0"
    assert u["reference_price"] == 15000
    assert u["last_system_event"] == "Q"
    assert u["short_sell_price"] == 0
    # book (values mirror gen_record_vectors.cpp's loops)
    assert u["level_count_bid"] == 10
    assert u["level_count_ask"] == 10
    expected_bids = [(15000 - i * 10, 100 * (i + 1), i + 1) for i in range(10)]
    expected_asks = [(15010 + i * 10, 200 * (i + 1), i + 2) for i in range(10)]
    assert u["bids"] == expected_bids
    assert u["asks"] == expected_asks
    assert u["total_bid_qty"] == 5500
    assert u["total_ask_qty"] == 11000
    assert u["total_bid_orders"] == 55
    assert u["total_ask_orders"] == 65
    # trades
    assert u["last_price"] == 15000
    assert u["last_qty"] == 300
    assert u["last_match_number"] == 987654321
    assert u["last_trade_ns"] == 1234567890000000000
    assert u["cum_qty"] == 400000
    assert u["cum_turnover"] == 6000000000
    assert u["trade_count"] == 4242
    # delta
    assert u["delta_op"] == "U"
    assert u["delta_order_number"] == 999002
    assert u["delta_orig_order_number"] == 999001
    assert u["delta_side"] == "B"
    assert u["delta_price"] == 14990
    assert u["delta_qty"] == 500
    assert u["delta_order_type"] == " "


def test_update_sync_empty_book(recs):
    u = recs[7]
    assert u["kind"] == "U"
    assert u["epoch"] == 7
    assert u["pub_seq"] == 1
    assert u["session"] == "1697659284"
    assert u["exch_seq"] == 12562
    assert u["exch_ns"] == 34200000000042
    assert u["trigger"] == "#"
    assert u["ticker"] == "9999"
    assert u["group"] == "NGHT"
    # auto-created book: zeroed static section, directory not seen
    assert u["isin"] == ""
    assert u["round_lot"] == 0
    assert u["tick_table_id"] == 0
    assert u["price_decimals"] == 0
    assert u["upper_limit"] == 0
    assert u["lower_limit"] == 0
    assert u["flags"] == 0
    # unknown-yet states + NO_PRICE reference price
    assert u["trading_state"] == "?"
    assert u["short_sell_restriction"] == "?"
    assert u["reference_price"] == NO_PRICE
    assert u["last_system_event"] == "\x00"
    assert u["short_sell_price"] == NO_PRICE
    # empty book: counts 0, every slot zero-filled
    assert u["level_count_bid"] == 0
    assert u["level_count_ask"] == 0
    assert u["bids"] == [(0, 0, 0)] * 10
    assert u["asks"] == [(0, 0, 0)] * 10
    assert u["total_bid_qty"] == 0
    assert u["total_ask_qty"] == 0
    assert u["total_bid_orders"] == 0
    assert u["total_ask_orders"] == 0
    # no trades
    assert u["last_price"] == 0
    assert u["last_qty"] == 0
    assert u["last_match_number"] == 0
    assert u["last_trade_ns"] == 0
    assert u["cum_qty"] == 0
    assert u["cum_turnover"] == 0
    assert u["trade_count"] == 0
    # '#' delta: no order fields
    assert u["delta_op"] == "#"
    assert u["delta_order_number"] == 0
    assert u["delta_orig_order_number"] == 0
    assert u["delta_side"] == "\x00"
    assert u["delta_price"] == 0
    assert u["delta_qty"] == 0
    assert u["delta_order_type"] == "\x00"


def test_update_trade_exec(recs):
    u = recs[8]
    assert u["kind"] == "U"
    assert u["epoch"] == 7
    assert u["pub_seq"] == 123457
    assert u["session"] == "1697659284"
    assert u["exch_seq"] == 234752
    assert u["exch_ns"] == 1234567890123456790
    assert u["trigger"] == "E"
    assert u["ticker"] == "7203"
    assert u["group"] == "DAY"
    assert u["isin"] == "JP3633400001"
    assert u["round_lot"] == 100
    assert u["tick_table_id"] == 2
    assert u["price_decimals"] == 1
    assert u["upper_limit"] == 999999
    assert u["lower_limit"] == 1
    assert u["flags"] == records.FLAG_DIRECTORY_SEEN
    assert u["trading_state"] == "T"
    assert u["short_sell_restriction"] == "1"
    assert u["reference_price"] == 25000
    assert u["last_system_event"] == "Q"
    assert u["short_sell_price"] == 25005
    assert u["level_count_bid"] == 1
    assert u["level_count_ask"] == 1
    assert u["bids"][0] == (24990, 1000, 3)
    assert u["asks"][0] == (25010, 4294967295, 1)  # max u32 qty
    assert u["bids"][1:] == [(0, 0, 0)] * 9
    assert u["asks"][1:] == [(0, 0, 0)] * 9
    assert u["total_bid_qty"] == 1000
    assert u["total_ask_qty"] == 4294967295
    assert u["total_bid_orders"] == 3
    assert u["total_ask_orders"] == 1
    assert u["last_price"] == 25000
    assert u["last_qty"] == 200
    assert u["last_match_number"] == 555001
    assert u["last_trade_ns"] == 1234567890123456790
    assert u["cum_qty"] == 200
    assert u["cum_turnover"] == 5000000
    assert u["trade_count"] == 1
    assert u["delta_op"] == "E"
    assert u["delta_order_number"] == 424242
    assert u["delta_orig_order_number"] == 0
    assert u["delta_side"] == "S"
    assert u["delta_price"] == 25000
    assert u["delta_qty"] == 0  # filled to zero -> row deleted
    assert u["delta_order_type"] == "Q"


def test_sync_end(recs):
    assert recs[9] == {
        "kind": "E",
        "session": "1697659284",
        "last_exch_seq": 234751,
        "epoch": 7,
    }


def test_update_size_matches_wire_spec_doc():
    """The decoder's frozen UPDATE size equals the number stated in
    docs/wire_spec.md ("total wire size NNN bytes (FROZEN)")."""
    with open(WIRE_SPEC, "r", encoding="utf-8") as f:
        spec = f.read()
    m = re.search(r"total wire size (\d+) bytes \(FROZEN\)", spec)
    assert m is not None, "frozen size sentence missing from docs/wire_spec.md"
    assert records.UPDATE_WIRE_SIZE == int(m.group(1)) == 437
    assert records.UPDATE_BODY_SIZE == 429


def test_header_validation_rejects_corruption():
    with open(VECTORS_BIN, "rb") as f:
        data = f.read()
    good = bytearray(data[:records.RECORD_HEADER_SIZE + 16])  # HELLO record

    bad = bytearray(good)
    bad[0] = 0x00  # magic
    with pytest.raises(records.RecordError):
        records.decode_record(bytes(bad))

    bad = bytearray(good)
    bad[2] = 99  # version
    with pytest.raises(records.RecordError):
        records.decode_record(bytes(bad))

    bad = bytearray(good)
    bad[3] = ord("x")  # kind
    with pytest.raises(records.RecordError):
        records.decode_record(bytes(bad))

    bad = bytearray(good)
    bad[5] += 1  # body_len mismatch for kind
    with pytest.raises(records.RecordError):
        records.decode_record(bytes(bad))

    # truncated record
    with pytest.raises(records.RecordError):
        records.decode_record(bytes(good[:-1]))

    # trailing partial record in a stream
    with pytest.raises(records.RecordError):
        records.decode_stream(data + b"\x4a")
