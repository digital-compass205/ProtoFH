"""JNX record wire-format decoder (docs/wire_spec.md, version 1 — FROZEN).

Python port of the decode side of cpp/wire/record.{h,cpp}, for the
multicast web client (jnxweb), tools/mcast_spy.py, and tests. Field
names mirror the C++ structs exactly.

Target runtime is Python 3.6 (RHEL 8 platform python) — stdlib only,
JNX_PLAN.md §0 subset. All wire integers are big-endian, hence the
explicit '>' in every struct format string.

Decoded records are plain dicts with a "kind" key (the 1-char record
kind) plus that kind's fields. Alpha fields are returned stripped of
trailing spaces; single-char tag fields are returned as 1-char strings
("\\x00" = not applicable / no value, as encoded).
"""
import struct
from collections import OrderedDict

# --- constants (docs/wire_spec.md) ------------------------------------

RECORD_MAGIC = 0x4A58  # "JX"
RECORD_VERSION = 1
RECORD_HEADER_SIZE = 8

KIND_UPDATE = "U"
KIND_ORDER = "O"
KIND_TICK = "K"
KIND_SYNC_BEGIN = "B"
KIND_SYNC_END = "E"
KIND_GET_STATE = "G"
KIND_HELLO = "H"
KIND_RESET = "R"

BOOK_DEPTH = 10

FLAG_DIRECTORY_SEEN = 0x01
FLAG_ORDER_COLLISION_SEEN = 0x02

NO_PRICE = 0x7FFFFFFF

# --- struct formats -----------------------------------------------------

_HEADER = struct.Struct(">HBcHH")  # magic, version, kind, body_len, reserved

# UPDATE body, one flat format string in spec field order:
#   envelope: epoch, pub_seq, session, exch_seq, exch_ns, trigger,
#             ticker, group
#   static:   isin, round_lot, tick_table_id, price_decimals,
#             upper_limit, lower_limit, flags
#   state:    trading_state, short_sell_restriction, reference_price,
#             last_system_event
#   book:     level_count_bid, level_count_ask, 10 bid + 10 ask levels
#             of (price u32, qty u32, order_count u32), totals
#   trades:   last_price, last_qty, last_match_number, last_trade_ns,
#             cum_qty, cum_turnover, trade_count
#   delta:    op, order_number, orig_order_number, side, price, qty,
#             order_type
_UPDATE_BODY = struct.Struct(
    ">QQ10sQQc4s4s"        # envelope
    "12sIIBIIB"             # static (incl. flags byte)
    "ccIc"                  # state
    "BB" + "III" * 20 +     # book: counts + 10 bid + 10 ask levels
    "QQII"                  # book totals
    "IIQQQQI"               # trade summary
    "cQQcIIc"               # delta
)

_ORDER_BODY = struct.Struct(">Q4s4scIIc")
_TICK_BODY = struct.Struct(">III")
_HELLO_BODY = struct.Struct(">QQ")
_SYNC_END_BODY = struct.Struct(">10sQQ")

#: kind -> fixed body length (bytes); decoders reject any other length.
BODY_SIZES = OrderedDict([
    (KIND_UPDATE, _UPDATE_BODY.size),
    (KIND_ORDER, _ORDER_BODY.size),
    (KIND_TICK, _TICK_BODY.size),
    (KIND_SYNC_BEGIN, 0),
    (KIND_SYNC_END, _SYNC_END_BODY.size),
    (KIND_GET_STATE, 0),
    (KIND_HELLO, _HELLO_BODY.size),
    (KIND_RESET, 0),
])

UPDATE_BODY_SIZE = _UPDATE_BODY.size   # 425
UPDATE_WIRE_SIZE = RECORD_HEADER_SIZE + UPDATE_BODY_SIZE  # 433 (FROZEN)

# Import-time pin of the frozen sizes (mirrors the C++ static_assert).
assert UPDATE_BODY_SIZE == 425, "UPDATE body size drifted from wire_spec"
assert _ORDER_BODY.size == 26
assert _TICK_BODY.size == 12
assert _HELLO_BODY.size == 16
assert _SYNC_END_BODY.size == 26


class RecordError(Exception):
    """Malformed record (bad magic/version/kind/length)."""


def _alpha(raw):
    """Wire alpha field -> str: ASCII, trailing spaces stripped."""
    return raw.decode("ascii").rstrip(" ")


def _char(raw):
    """Wire single-char tag field -> 1-char str (may be "\\x00")."""
    return raw.decode("latin-1")


def decode_header(buf):
    """Decode + validate an 8-byte record header.

    Returns (kind, body_len). Raises RecordError on anything invalid.
    """
    if len(buf) < RECORD_HEADER_SIZE:
        raise RecordError("buffer shorter than record header")
    magic, version, kind_b, body_len, _reserved = _HEADER.unpack(
        bytes(buf[:RECORD_HEADER_SIZE])
    )
    if magic != RECORD_MAGIC:
        raise RecordError("bad record magic: 0x{:04X} (want 0x4A58)".format(magic))
    if version != RECORD_VERSION:
        raise RecordError("unsupported record version: {} (want 1)".format(version))
    kind = _char(kind_b)
    if kind not in BODY_SIZES:
        raise RecordError("unknown record kind: {!r}".format(kind))
    if body_len != BODY_SIZES[kind]:
        raise RecordError(
            "kind {!r}: body_len {} != expected {}".format(
                kind, body_len, BODY_SIZES[kind]
            )
        )
    return kind, body_len


def _decode_update_body(body):
    v = _UPDATE_BODY.unpack(body)
    i = 0

    rec = OrderedDict()
    rec["kind"] = KIND_UPDATE
    # envelope
    rec["epoch"] = v[0]
    rec["pub_seq"] = v[1]
    rec["session"] = _alpha(v[2])
    rec["exch_seq"] = v[3]
    rec["exch_ns"] = v[4]
    rec["trigger"] = _char(v[5])
    rec["ticker"] = _alpha(v[6])
    rec["group"] = _alpha(v[7])
    # static
    rec["isin"] = _alpha(v[8])
    rec["round_lot"] = v[9]
    rec["tick_table_id"] = v[10]
    rec["price_decimals"] = v[11]
    rec["upper_limit"] = v[12]
    rec["lower_limit"] = v[13]
    rec["flags"] = v[14]
    # state
    rec["trading_state"] = _char(v[15])
    rec["short_sell_restriction"] = _char(v[16])
    rec["reference_price"] = v[17]
    rec["last_system_event"] = _char(v[18])
    # book
    rec["level_count_bid"] = v[19]
    rec["level_count_ask"] = v[20]
    if rec["level_count_bid"] > BOOK_DEPTH or rec["level_count_ask"] > BOOK_DEPTH:
        raise RecordError("level count exceeds book depth 10")
    i = 21
    bids = []
    for _ in range(BOOK_DEPTH):
        bids.append((v[i], v[i + 1], v[i + 2]))  # (price, qty, order_count)
        i += 3
    asks = []
    for _ in range(BOOK_DEPTH):
        asks.append((v[i], v[i + 1], v[i + 2]))
        i += 3
    rec["bids"] = bids
    rec["asks"] = asks
    rec["total_bid_qty"] = v[i]
    rec["total_ask_qty"] = v[i + 1]
    rec["total_bid_orders"] = v[i + 2]
    rec["total_ask_orders"] = v[i + 3]
    i += 4
    # trade summary
    rec["last_price"] = v[i]
    rec["last_qty"] = v[i + 1]
    rec["last_match_number"] = v[i + 2]
    rec["last_trade_ns"] = v[i + 3]
    rec["cum_qty"] = v[i + 4]
    rec["cum_turnover"] = v[i + 5]
    rec["trade_count"] = v[i + 6]
    i += 7
    # delta
    rec["delta_op"] = _char(v[i])
    rec["delta_order_number"] = v[i + 1]
    rec["delta_orig_order_number"] = v[i + 2]
    rec["delta_side"] = _char(v[i + 3])
    rec["delta_price"] = v[i + 4]
    rec["delta_qty"] = v[i + 5]
    rec["delta_order_type"] = _char(v[i + 6])
    return rec


def _decode_order_body(body):
    (order_number, ticker, group, side, price, qty_remaining,
     order_type) = _ORDER_BODY.unpack(body)
    rec = OrderedDict()
    rec["kind"] = KIND_ORDER
    rec["order_number"] = order_number
    rec["ticker"] = _alpha(ticker)
    rec["group"] = _alpha(group)
    rec["side"] = _char(side)
    rec["price"] = price
    rec["qty_remaining"] = qty_remaining
    rec["order_type"] = _char(order_type)
    return rec


def _decode_tick_body(body):
    table_id, price_start, tick_size = _TICK_BODY.unpack(body)
    rec = OrderedDict()
    rec["kind"] = KIND_TICK
    rec["table_id"] = table_id
    rec["price_start"] = price_start
    rec["tick_size"] = tick_size
    return rec


def _decode_hello_body(body):
    epoch, last_exch_seq = _HELLO_BODY.unpack(body)
    rec = OrderedDict()
    rec["kind"] = KIND_HELLO
    rec["epoch"] = epoch
    rec["last_exch_seq"] = last_exch_seq
    return rec


def _decode_sync_end_body(body):
    session, last_exch_seq, epoch = _SYNC_END_BODY.unpack(body)
    rec = OrderedDict()
    rec["kind"] = KIND_SYNC_END
    rec["session"] = _alpha(session)
    rec["last_exch_seq"] = last_exch_seq
    rec["epoch"] = epoch
    return rec


def _decode_empty_body(kind, body):
    if body:
        raise RecordError("kind {!r}: unexpected body bytes".format(kind))
    rec = OrderedDict()
    rec["kind"] = kind
    return rec


_BODY_DECODERS = {
    KIND_UPDATE: _decode_update_body,
    KIND_ORDER: _decode_order_body,
    KIND_TICK: _decode_tick_body,
    KIND_HELLO: _decode_hello_body,
    KIND_SYNC_END: _decode_sync_end_body,
}


def decode_record(buf):
    """Decode exactly one record (header + body) from `buf`.

    `buf` must cover exactly one record. Returns an OrderedDict with a
    "kind" key plus that kind's fields. Raises RecordError on anything
    malformed.
    """
    kind, body_len = decode_header(buf)
    total = RECORD_HEADER_SIZE + body_len
    if len(buf) != total:
        raise RecordError(
            "kind {!r}: buffer length {} != record size {}".format(
                kind, len(buf), total
            )
        )
    body = bytes(buf[RECORD_HEADER_SIZE:total])
    decoder = _BODY_DECODERS.get(kind)
    if decoder is None:
        return _decode_empty_body(kind, body)
    return decoder(body)


def decode_stream(data):
    """Decode a byte string of concatenated records -> list of dicts.

    The whole buffer must be consumed exactly (a trailing partial record
    raises RecordError — this helper is for complete framed captures
    like cpp/test/vectors/records.bin, not for incremental socket
    reads).
    """
    records = []
    offset = 0
    n = len(data)
    while offset < n:
        if n - offset < RECORD_HEADER_SIZE:
            raise RecordError("trailing partial header at offset {}".format(offset))
        kind, body_len = decode_header(data[offset:offset + RECORD_HEADER_SIZE])
        total = RECORD_HEADER_SIZE + body_len
        if n - offset < total:
            raise RecordError(
                "trailing partial {!r} record at offset {}".format(kind, offset)
            )
        records.append(decode_record(data[offset:offset + total]))
        offset += total
    return records
