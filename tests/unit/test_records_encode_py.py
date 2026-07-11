"""F4: encode side of jnxweb/records.py.

The C++ generator's records.bin is the golden reference:
encode_record(decode_record(x)) must reproduce every record
byte-for-byte, and the encoders must reject malformed input.
"""
import os

import pytest

from jnxweb import records

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
VECTORS_BIN = os.path.join(REPO_ROOT, "cpp", "test", "vectors", "records.bin")


@pytest.fixture(scope="module")
def golden_bytes():
    with open(VECTORS_BIN, "rb") as f:
        return f.read()


def test_encode_decode_round_trip_whole_file(golden_bytes):
    recs = records.decode_stream(golden_bytes)
    assert len(recs) == 10
    encoded = b"".join(records.encode_record(r) for r in recs)
    assert encoded == golden_bytes


def test_encode_decode_round_trip_each_record(golden_bytes):
    offset = 0
    n = 0
    while offset < len(golden_bytes):
        kind, body_len = records.decode_header(
            golden_bytes[offset:offset + records.RECORD_HEADER_SIZE]
        )
        total = records.RECORD_HEADER_SIZE + body_len
        wire = golden_bytes[offset:offset + total]
        rec = records.decode_record(wire)
        assert records.encode_record(rec) == wire, (
            "round-trip mismatch for kind {!r}".format(kind)
        )
        offset += total
        n += 1
    assert n == 10


def test_encode_control_kinds():
    for kind in ("B", "G", "R"):
        wire = records.encode_control(kind)
        assert len(wire) == records.RECORD_HEADER_SIZE
        assert records.decode_record(wire) == {"kind": kind}
    with pytest.raises(records.RecordError):
        records.encode_control("U")


def test_encode_update_zero_fills_slots_beyond_count():
    rec = {
        "kind": "U",
        "level_count_bid": 1,
        "level_count_ask": 0,
        # junk beyond the counts must not reach the wire
        "bids": [(100, 10, 1), (999, 99, 9)],
        "asks": [(888, 88, 8)],
    }
    wire = records.encode_update(rec)
    back = records.decode_record(wire)
    assert back["bids"][0] == (100, 10, 1)
    assert back["bids"][1:] == [(0, 0, 0)] * 9
    assert back["asks"] == [(0, 0, 0)] * 10


def test_encode_rejects_bad_values():
    with pytest.raises(records.RecordError):
        records.encode_update({"kind": "U", "ticker": "TOOLONG"})
    with pytest.raises(records.RecordError):
        records.encode_update({"kind": "U", "level_count_bid": 11})
    with pytest.raises(records.RecordError):
        records.encode_order({"kind": "O", "side": "BS"})


def test_encoded_update_has_frozen_size():
    wire = records.encode_update({"kind": "U"})
    assert len(wire) == records.UPDATE_WIRE_SIZE == 433
