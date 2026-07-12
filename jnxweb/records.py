"""JNX record wire-format codec (docs/wire_spec.md, version 2 — FROZEN).

Python port of cpp/wire/record.{h,cpp}: decode for the multicast web
client (jnxweb), tools/mcast_spy.py, and tests; encode (added in F4)
for test feeders and tooling that must speak the FH->DB protocol.
Field names mirror the C++ structs exactly, and
encode_record(decode_record(wire)) == wire for every kind (asserted
against records.bin by tests/unit/test_records_encode_py.py).

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
RECORD_VERSION = 2
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
#             last_system_event, short_sell_price
#   book:     level_count_bid, level_count_ask, 10 bid + 10 ask levels
#             of (price u32, qty u32, order_count u32), totals
#   trades:   last_price, last_qty, last_match_number, last_trade_ns,
#             cum_qty, cum_turnover, trade_count
#   delta:    op, order_number, orig_order_number, side, price, qty,
#             order_type
_UPDATE_BODY = struct.Struct(
    ">QQ10sQQc4s4s"        # envelope
    "12sIIBIIB"             # static (incl. flags byte)
    "ccIcI"                 # state (short_sell_price added in wire v2)
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

UPDATE_BODY_SIZE = _UPDATE_BODY.size   # 429
UPDATE_WIRE_SIZE = RECORD_HEADER_SIZE + UPDATE_BODY_SIZE  # 437 (FROZEN)

# Import-time pin of the frozen sizes (mirrors the C++ static_assert).
assert UPDATE_BODY_SIZE == 429, "UPDATE body size drifted from wire_spec"
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
        raise RecordError("unsupported record version: {} (want 2)".format(version))
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
    rec["short_sell_price"] = v[19]
    # book
    rec["level_count_bid"] = v[20]
    rec["level_count_ask"] = v[21]
    if rec["level_count_bid"] > BOOK_DEPTH or rec["level_count_ask"] > BOOK_DEPTH:
        raise RecordError("level count exceeds book depth 10")
    i = 22
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


# --- encode (F4) ---------------------------------------------------------
# Inverse of the decoders above. Input dicts use exactly the shapes the
# decoders return; missing keys default to zero / empty (mirroring the
# zero-initialised C++ structs). Level slots at index >= level_count are
# forced to zero on the wire, matching the C++ encoder's zero-fill rule.

def _alpha_enc(value, width):
    """str -> fixed-width wire alpha: ASCII, right-padded with spaces."""
    raw = value.encode("ascii")
    if len(raw) > width:
        raise RecordError(
            "alpha value {!r} exceeds field width {}".format(value, width)
        )
    return raw + b" " * (width - len(raw))


def _char_enc(value):
    """1-char str (or '') -> single wire byte ('' -> 0x00)."""
    if not value:
        return b"\x00"
    raw = value.encode("latin-1")
    if len(raw) != 1:
        raise RecordError("char field must be one byte, got {!r}".format(value))
    return raw


def encode_header(kind, body_len):
    if kind not in BODY_SIZES:
        raise RecordError("unknown record kind: {!r}".format(kind))
    if body_len != BODY_SIZES[kind]:
        raise RecordError(
            "kind {!r}: body_len {} != expected {}".format(
                kind, body_len, BODY_SIZES[kind]
            )
        )
    return _HEADER.pack(RECORD_MAGIC, RECORD_VERSION,
                        kind.encode("ascii"), body_len, 0)


def encode_update(rec):
    """Encode an UPDATE dict (decode_update shape) -> full wire record."""
    bids = list(rec.get("bids", []))
    asks = list(rec.get("asks", []))
    while len(bids) < BOOK_DEPTH:
        bids.append((0, 0, 0))
    while len(asks) < BOOK_DEPTH:
        asks.append((0, 0, 0))
    n_bid = rec.get("level_count_bid", 0)
    n_ask = rec.get("level_count_ask", 0)
    if n_bid > BOOK_DEPTH or n_ask > BOOK_DEPTH:
        raise RecordError("level count exceeds book depth 10")
    level_values = []
    for i in range(BOOK_DEPTH):
        px, qty, cnt = bids[i] if i < n_bid else (0, 0, 0)
        level_values.extend((px, qty, cnt))
    for i in range(BOOK_DEPTH):
        px, qty, cnt = asks[i] if i < n_ask else (0, 0, 0)
        level_values.extend((px, qty, cnt))

    body = _UPDATE_BODY.pack(
        rec.get("epoch", 0),
        rec.get("pub_seq", 0),
        _alpha_enc(rec.get("session", ""), 10),
        rec.get("exch_seq", 0),
        rec.get("exch_ns", 0),
        _char_enc(rec.get("trigger", "")),
        _alpha_enc(rec.get("ticker", ""), 4),
        _alpha_enc(rec.get("group", ""), 4),
        _alpha_enc(rec.get("isin", ""), 12),
        rec.get("round_lot", 0),
        rec.get("tick_table_id", 0),
        rec.get("price_decimals", 0),
        rec.get("upper_limit", 0),
        rec.get("lower_limit", 0),
        rec.get("flags", 0),
        _char_enc(rec.get("trading_state", "")),
        _char_enc(rec.get("short_sell_restriction", "")),
        rec.get("reference_price", 0),
        _char_enc(rec.get("last_system_event", "")),
        rec.get("short_sell_price", 0),
        n_bid,
        n_ask,
        *(level_values + [
            rec.get("total_bid_qty", 0),
            rec.get("total_ask_qty", 0),
            rec.get("total_bid_orders", 0),
            rec.get("total_ask_orders", 0),
            rec.get("last_price", 0),
            rec.get("last_qty", 0),
            rec.get("last_match_number", 0),
            rec.get("last_trade_ns", 0),
            rec.get("cum_qty", 0),
            rec.get("cum_turnover", 0),
            rec.get("trade_count", 0),
            _char_enc(rec.get("delta_op", "")),
            rec.get("delta_order_number", 0),
            rec.get("delta_orig_order_number", 0),
            _char_enc(rec.get("delta_side", "")),
            rec.get("delta_price", 0),
            rec.get("delta_qty", 0),
            _char_enc(rec.get("delta_order_type", "")),
        ])
    )
    return encode_header(KIND_UPDATE, len(body)) + body


def encode_order(rec):
    body = _ORDER_BODY.pack(
        rec.get("order_number", 0),
        _alpha_enc(rec.get("ticker", ""), 4),
        _alpha_enc(rec.get("group", ""), 4),
        _char_enc(rec.get("side", "")),
        rec.get("price", 0),
        rec.get("qty_remaining", 0),
        _char_enc(rec.get("order_type", "")),
    )
    return encode_header(KIND_ORDER, len(body)) + body


def encode_tick(rec):
    body = _TICK_BODY.pack(
        rec.get("table_id", 0),
        rec.get("price_start", 0),
        rec.get("tick_size", 0),
    )
    return encode_header(KIND_TICK, len(body)) + body


def encode_hello(rec):
    body = _HELLO_BODY.pack(
        rec.get("epoch", 0),
        rec.get("last_exch_seq", 0),
    )
    return encode_header(KIND_HELLO, len(body)) + body


def encode_sync_end(rec):
    body = _SYNC_END_BODY.pack(
        _alpha_enc(rec.get("session", ""), 10),
        rec.get("last_exch_seq", 0),
        rec.get("epoch", 0),
    )
    return encode_header(KIND_SYNC_END, len(body)) + body


def encode_control(kind):
    """SYNC_BEGIN / GET_STATE / RESET — header-only records."""
    if kind not in (KIND_SYNC_BEGIN, KIND_GET_STATE, KIND_RESET):
        raise RecordError("not a control kind: {!r}".format(kind))
    return encode_header(kind, 0)


_KIND_ENCODERS = {
    KIND_UPDATE: encode_update,
    KIND_ORDER: encode_order,
    KIND_TICK: encode_tick,
    KIND_HELLO: encode_hello,
    KIND_SYNC_END: encode_sync_end,
}


def encode_record(rec):
    """Encode any record dict (shape of decode_record output) -> bytes."""
    kind = rec.get("kind")
    encoder = _KIND_ENCODERS.get(kind)
    if encoder is not None:
        return encoder(rec)
    return encode_control(kind)
