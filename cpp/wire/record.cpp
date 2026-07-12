// record.cpp — see record.h. Byte layout frozen by docs/wire_spec.md v2.
#include "wire/record.h"

#include <cstring>

#include "common/endian.h"

namespace jnx {

namespace {

// Compile-time pin of the frozen UPDATE body size: the sum of every field
// in docs/wire_spec.md's UPDATE tables. If a field is ever added/removed
// this fails to compile until BOTH the constant and the spec are updated.
static_assert(
    // envelope: epoch, pub_seq, session, exch_seq, exch_ns, trigger,
    //           ticker, group
    (8 + 8 + 10 + 8 + 8 + 1 + 4 + 4) +
    // static: isin, round_lot, tick_table_id, price_decimals,
    //         upper_limit, lower_limit, flags
    (12 + 4 + 4 + 1 + 4 + 4 + 1) +
    // state: trading_state, short_sell_restriction, reference_price,
    //        last_system_event, short_sell_price
    (1 + 1 + 4 + 1 + 4) +
    // book: level_count_bid, level_count_ask, 10+10 levels of
    //       (price u32, qty u32, order_count u32), totals
    (1 + 1 + 2 * 10 * (4 + 4 + 4) + 8 + 8 + 4 + 4) +
    // trades: last_price, last_qty, last_match_number, last_trade_ns,
    //         cum_qty, cum_turnover, trade_count
    (4 + 4 + 8 + 8 + 8 + 8 + 4) +
    // delta: op, order_number, orig_order_number, side, price, qty,
    //        order_type
    (1 + 8 + 8 + 1 + 4 + 4 + 1)
    == UPDATE_BODY_SIZE,
    "UPDATE body layout does not sum to the frozen size in docs/wire_spec.md");

static_assert((8 + 4 + 4 + 1 + 4 + 4 + 1) == ORDER_BODY_SIZE,
              "ORDER body layout mismatch");
static_assert((4 + 4 + 4) == TICK_BODY_SIZE, "TICK body layout mismatch");
static_assert((10 + 8 + 8) == SYNC_END_BODY_SIZE,
              "SYNC_END body layout mismatch");
static_assert((8 + 8) == HELLO_BODY_SIZE, "HELLO body layout mismatch");

// Decode a fixed-width Alpha field: copy, strip trailing spaces, NUL-term.
// `out` must have room for width+1 chars.
void get_alpha(const unsigned char* p, size_t width, char* out) {
    size_t n = width;
    while (n > 0 && p[n - 1] == ' ') {
        --n;
    }
    std::memcpy(out, p, n);
    out[n] = '\0';
}

// Encode a fixed-width Alpha field: left-justified, right-padded w/ spaces.
void put_alpha(unsigned char* p, size_t width, const char* s) {
    size_t n = std::strlen(s);
    if (n > width) {
        n = width;
    }
    std::memcpy(p, s, n);
    std::memset(p + n, ' ', width - n);
}

void put_header(unsigned char* p, char kind, uint16_t body_len) {
    be_put_u16(p, RECORD_MAGIC);
    p[2] = RECORD_VERSION;
    p[3] = static_cast<unsigned char>(kind);
    be_put_u16(p + 4, body_len);
    be_put_u16(p + 6, 0);  // reserved — always zero on the wire
}

bool set_err(const char** err, const char* why) {
    if (err != 0) {
        *err = why;
    }
    return false;
}

// Shared strict prologue for the typed decoders: validate the header,
// require the expected kind, and require len == header + that kind's body.
bool check_record(const unsigned char* buf, size_t len, char want_kind,
                  size_t want_body, const char** err) {
    char kind = '\0';
    uint16_t body_len = 0;
    if (!decode_header(buf, len, &kind, &body_len, err)) {
        return false;
    }
    if (kind != want_kind) {
        return set_err(err, "record kind mismatch");
    }
    if (len != RECORD_HEADER_SIZE + want_body) {
        return set_err(err, "buffer length != record wire size");
    }
    (void)body_len;  // already validated against the kind by decode_header
    return true;
}

} // namespace

int record_body_len(char kind) {
    switch (kind) {
        case KIND_UPDATE:
            return static_cast<int>(UPDATE_BODY_SIZE);
        case KIND_ORDER:
            return static_cast<int>(ORDER_BODY_SIZE);
        case KIND_TICK:
            return static_cast<int>(TICK_BODY_SIZE);
        case KIND_SYNC_END:
            return static_cast<int>(SYNC_END_BODY_SIZE);
        case KIND_HELLO:
            return static_cast<int>(HELLO_BODY_SIZE);
        case KIND_SYNC_BEGIN:
        case KIND_GET_STATE:
        case KIND_RESET:
            return 0;
        default:
            return -1;
    }
}

UpdateRecord::UpdateRecord()
    : epoch(0),
      pub_seq(0),
      exch_seq(0),
      exch_ns(0),
      trigger('\0'),
      round_lot(0),
      tick_table_id(0),
      price_decimals(0),
      upper_limit(0),
      lower_limit(0),
      flags(0),
      trading_state('\0'),
      short_sell_restriction('\0'),
      reference_price(0),
      last_system_event('\0'),
      short_sell_price(0),
      level_count_bid(0),
      level_count_ask(0),
      total_bid_qty(0),
      total_ask_qty(0),
      total_bid_orders(0),
      total_ask_orders(0),
      last_price(0),
      last_qty(0),
      last_match_number(0),
      last_trade_ns(0),
      cum_qty(0),
      cum_turnover(0),
      trade_count(0),
      delta_op('\0'),
      delta_order_number(0),
      delta_orig_order_number(0),
      delta_side('\0'),
      delta_price(0),
      delta_qty(0),
      delta_order_type('\0') {
    session[0] = '\0';
    ticker[0] = '\0';
    group[0] = '\0';
    isin[0] = '\0';
}

OrderRecord::OrderRecord()
    : order_number(0),
      side('\0'),
      price(0),
      qty_remaining(0),
      order_type('\0') {
    ticker[0] = '\0';
    group[0] = '\0';
}

SyncEndRecord::SyncEndRecord() : last_exch_seq(0), epoch(0) {
    session[0] = '\0';
}

// --- encode --------------------------------------------------------------

size_t encode_update(const UpdateRecord& in, unsigned char* buf) {
    put_header(buf, KIND_UPDATE, static_cast<uint16_t>(UPDATE_BODY_SIZE));
    unsigned char* p = buf + RECORD_HEADER_SIZE;

    // envelope
    be_put_u64(p, in.epoch);
    p += 8;
    be_put_u64(p, in.pub_seq);
    p += 8;
    put_alpha(p, 10, in.session);
    p += 10;
    be_put_u64(p, in.exch_seq);
    p += 8;
    be_put_u64(p, in.exch_ns);
    p += 8;
    *p++ = static_cast<unsigned char>(in.trigger);
    put_alpha(p, 4, in.ticker);
    p += 4;
    put_alpha(p, 4, in.group);
    p += 4;

    // static
    put_alpha(p, 12, in.isin);
    p += 12;
    be_put_u32(p, in.round_lot);
    p += 4;
    be_put_u32(p, in.tick_table_id);
    p += 4;
    *p++ = in.price_decimals;
    be_put_u32(p, in.upper_limit);
    p += 4;
    be_put_u32(p, in.lower_limit);
    p += 4;
    *p++ = in.flags;

    // state
    *p++ = static_cast<unsigned char>(in.trading_state);
    *p++ = static_cast<unsigned char>(in.short_sell_restriction);
    be_put_u32(p, in.reference_price);
    p += 4;
    *p++ = static_cast<unsigned char>(in.last_system_event);
    be_put_u32(p, in.short_sell_price);
    p += 4;

    // book — slots at index >= level_count are forced to zero on the wire
    // regardless of struct contents (docs/wire_spec.md zero-fill rule).
    *p++ = in.level_count_bid;
    *p++ = in.level_count_ask;
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        if (i < static_cast<int>(in.level_count_bid)) {
            be_put_u32(p, in.bids[i].price);
            be_put_u32(p + 4, in.bids[i].qty);
            be_put_u32(p + 8, in.bids[i].order_count);
        } else {
            std::memset(p, 0, 12);
        }
        p += 12;
    }
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        if (i < static_cast<int>(in.level_count_ask)) {
            be_put_u32(p, in.asks[i].price);
            be_put_u32(p + 4, in.asks[i].qty);
            be_put_u32(p + 8, in.asks[i].order_count);
        } else {
            std::memset(p, 0, 12);
        }
        p += 12;
    }
    be_put_u64(p, in.total_bid_qty);
    p += 8;
    be_put_u64(p, in.total_ask_qty);
    p += 8;
    be_put_u32(p, in.total_bid_orders);
    p += 4;
    be_put_u32(p, in.total_ask_orders);
    p += 4;

    // trades
    be_put_u32(p, in.last_price);
    p += 4;
    be_put_u32(p, in.last_qty);
    p += 4;
    be_put_u64(p, in.last_match_number);
    p += 8;
    be_put_u64(p, in.last_trade_ns);
    p += 8;
    be_put_u64(p, in.cum_qty);
    p += 8;
    be_put_u64(p, in.cum_turnover);
    p += 8;
    be_put_u32(p, in.trade_count);
    p += 4;

    // delta
    *p++ = static_cast<unsigned char>(in.delta_op);
    be_put_u64(p, in.delta_order_number);
    p += 8;
    be_put_u64(p, in.delta_orig_order_number);
    p += 8;
    *p++ = static_cast<unsigned char>(in.delta_side);
    be_put_u32(p, in.delta_price);
    p += 4;
    be_put_u32(p, in.delta_qty);
    p += 4;
    *p++ = static_cast<unsigned char>(in.delta_order_type);

    return static_cast<size_t>(p - buf);
}

size_t encode_order(const OrderRecord& in, unsigned char* buf) {
    put_header(buf, KIND_ORDER, static_cast<uint16_t>(ORDER_BODY_SIZE));
    unsigned char* p = buf + RECORD_HEADER_SIZE;
    be_put_u64(p, in.order_number);
    p += 8;
    put_alpha(p, 4, in.ticker);
    p += 4;
    put_alpha(p, 4, in.group);
    p += 4;
    *p++ = static_cast<unsigned char>(in.side);
    be_put_u32(p, in.price);
    p += 4;
    be_put_u32(p, in.qty_remaining);
    p += 4;
    *p++ = static_cast<unsigned char>(in.order_type);
    return static_cast<size_t>(p - buf);
}

size_t encode_tick(const TickRecord& in, unsigned char* buf) {
    put_header(buf, KIND_TICK, static_cast<uint16_t>(TICK_BODY_SIZE));
    unsigned char* p = buf + RECORD_HEADER_SIZE;
    be_put_u32(p, in.table_id);
    be_put_u32(p + 4, in.price_start);
    be_put_u32(p + 8, in.tick_size);
    return TICK_WIRE_SIZE;
}

size_t encode_hello(const HelloRecord& in, unsigned char* buf) {
    put_header(buf, KIND_HELLO, static_cast<uint16_t>(HELLO_BODY_SIZE));
    unsigned char* p = buf + RECORD_HEADER_SIZE;
    be_put_u64(p, in.epoch);
    be_put_u64(p + 8, in.last_exch_seq);
    return HELLO_WIRE_SIZE;
}

size_t encode_sync_end(const SyncEndRecord& in, unsigned char* buf) {
    put_header(buf, KIND_SYNC_END, static_cast<uint16_t>(SYNC_END_BODY_SIZE));
    unsigned char* p = buf + RECORD_HEADER_SIZE;
    put_alpha(p, 10, in.session);
    p += 10;
    be_put_u64(p, in.last_exch_seq);
    be_put_u64(p + 8, in.epoch);
    return SYNC_END_WIRE_SIZE;
}

size_t encode_control(char kind, unsigned char* buf) {
    if (kind != KIND_SYNC_BEGIN && kind != KIND_GET_STATE &&
        kind != KIND_RESET) {
        return 0;
    }
    put_header(buf, kind, 0);
    return CONTROL_WIRE_SIZE;
}

// --- decode ---------------------------------------------------------------

bool decode_header(const unsigned char* buf, size_t len, char* kind,
                   uint16_t* body_len, const char** err) {
    if (len < RECORD_HEADER_SIZE) {
        return set_err(err, "buffer shorter than record header");
    }
    if (be_get_u16(buf) != RECORD_MAGIC) {
        return set_err(err, "bad record magic (want 0x4A58)");
    }
    if (buf[2] != RECORD_VERSION) {
        return set_err(err, "unsupported record version (want 2)");
    }
    char k = static_cast<char>(buf[3]);
    int want = record_body_len(k);
    if (want < 0) {
        return set_err(err, "unknown record kind");
    }
    uint16_t bl = be_get_u16(buf + 4);
    if (bl != static_cast<uint16_t>(want)) {
        return set_err(err, "body_len does not match record kind");
    }
    if (kind != 0) {
        *kind = k;
    }
    if (body_len != 0) {
        *body_len = bl;
    }
    return true;
}

bool decode_update(const unsigned char* buf, size_t len, UpdateRecord& out,
                   const char** err) {
    if (!check_record(buf, len, KIND_UPDATE, UPDATE_BODY_SIZE, err)) {
        return false;
    }
    const unsigned char* p = buf + RECORD_HEADER_SIZE;

    out.epoch = be_get_u64(p);
    p += 8;
    out.pub_seq = be_get_u64(p);
    p += 8;
    get_alpha(p, 10, out.session);
    p += 10;
    out.exch_seq = be_get_u64(p);
    p += 8;
    out.exch_ns = be_get_u64(p);
    p += 8;
    out.trigger = static_cast<char>(*p++);
    get_alpha(p, 4, out.ticker);
    p += 4;
    get_alpha(p, 4, out.group);
    p += 4;

    get_alpha(p, 12, out.isin);
    p += 12;
    out.round_lot = be_get_u32(p);
    p += 4;
    out.tick_table_id = be_get_u32(p);
    p += 4;
    out.price_decimals = *p++;
    out.upper_limit = be_get_u32(p);
    p += 4;
    out.lower_limit = be_get_u32(p);
    p += 4;
    out.flags = *p++;

    out.trading_state = static_cast<char>(*p++);
    out.short_sell_restriction = static_cast<char>(*p++);
    out.reference_price = be_get_u32(p);
    p += 4;
    out.last_system_event = static_cast<char>(*p++);
    out.short_sell_price = be_get_u32(p);
    p += 4;

    out.level_count_bid = *p++;
    out.level_count_ask = *p++;
    if (out.level_count_bid > BOOK_DEPTH || out.level_count_ask > BOOK_DEPTH) {
        return set_err(err, "level count exceeds book depth 10");
    }
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        out.bids[i].price = be_get_u32(p);
        out.bids[i].qty = be_get_u32(p + 4);
        out.bids[i].order_count = be_get_u32(p + 8);
        p += 12;
    }
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        out.asks[i].price = be_get_u32(p);
        out.asks[i].qty = be_get_u32(p + 4);
        out.asks[i].order_count = be_get_u32(p + 8);
        p += 12;
    }
    out.total_bid_qty = be_get_u64(p);
    p += 8;
    out.total_ask_qty = be_get_u64(p);
    p += 8;
    out.total_bid_orders = be_get_u32(p);
    p += 4;
    out.total_ask_orders = be_get_u32(p);
    p += 4;

    out.last_price = be_get_u32(p);
    p += 4;
    out.last_qty = be_get_u32(p);
    p += 4;
    out.last_match_number = be_get_u64(p);
    p += 8;
    out.last_trade_ns = be_get_u64(p);
    p += 8;
    out.cum_qty = be_get_u64(p);
    p += 8;
    out.cum_turnover = be_get_u64(p);
    p += 8;
    out.trade_count = be_get_u32(p);
    p += 4;

    out.delta_op = static_cast<char>(*p++);
    out.delta_order_number = be_get_u64(p);
    p += 8;
    out.delta_orig_order_number = be_get_u64(p);
    p += 8;
    out.delta_side = static_cast<char>(*p++);
    out.delta_price = be_get_u32(p);
    p += 4;
    out.delta_qty = be_get_u32(p);
    p += 4;
    out.delta_order_type = static_cast<char>(*p++);

    return true;
}

bool decode_order(const unsigned char* buf, size_t len, OrderRecord& out,
                  const char** err) {
    if (!check_record(buf, len, KIND_ORDER, ORDER_BODY_SIZE, err)) {
        return false;
    }
    const unsigned char* p = buf + RECORD_HEADER_SIZE;
    out.order_number = be_get_u64(p);
    p += 8;
    get_alpha(p, 4, out.ticker);
    p += 4;
    get_alpha(p, 4, out.group);
    p += 4;
    out.side = static_cast<char>(*p++);
    out.price = be_get_u32(p);
    p += 4;
    out.qty_remaining = be_get_u32(p);
    p += 4;
    out.order_type = static_cast<char>(*p);
    return true;
}

bool decode_tick(const unsigned char* buf, size_t len, TickRecord& out,
                 const char** err) {
    if (!check_record(buf, len, KIND_TICK, TICK_BODY_SIZE, err)) {
        return false;
    }
    const unsigned char* p = buf + RECORD_HEADER_SIZE;
    out.table_id = be_get_u32(p);
    out.price_start = be_get_u32(p + 4);
    out.tick_size = be_get_u32(p + 8);
    return true;
}

bool decode_hello(const unsigned char* buf, size_t len, HelloRecord& out,
                  const char** err) {
    if (!check_record(buf, len, KIND_HELLO, HELLO_BODY_SIZE, err)) {
        return false;
    }
    const unsigned char* p = buf + RECORD_HEADER_SIZE;
    out.epoch = be_get_u64(p);
    out.last_exch_seq = be_get_u64(p + 8);
    return true;
}

bool decode_sync_end(const unsigned char* buf, size_t len, SyncEndRecord& out,
                     const char** err) {
    if (!check_record(buf, len, KIND_SYNC_END, SYNC_END_BODY_SIZE, err)) {
        return false;
    }
    const unsigned char* p = buf + RECORD_HEADER_SIZE;
    get_alpha(p, 10, out.session);
    p += 10;
    out.last_exch_seq = be_get_u64(p);
    out.epoch = be_get_u64(p + 8);
    return true;
}

// --- framer ----------------------------------------------------------------

void RecordFramer::feed(const unsigned char* data, size_t len) {
    if (corrupt_) {
        return;  // stream already dead — drop everything
    }
    buf_.insert(buf_.end(), data, data + len);
}

bool RecordFramer::next(RawRecord& out) {
    if (corrupt_ || buf_.size() < RECORD_HEADER_SIZE) {
        return false;
    }
    char kind = '\0';
    uint16_t body_len = 0;
    const char* why = 0;
    if (!decode_header(&buf_[0], buf_.size(), &kind, &body_len, &why)) {
        // A full header is buffered but invalid: the stream is corrupt and
        // unrecoverable (no resync marker in the format by design).
        corrupt_ = true;
        corrupt_reason_ = why;
        buf_.clear();
        return false;
    }
    size_t total = RECORD_HEADER_SIZE + body_len;
    if (buf_.size() < total) {
        return false;  // body still arriving
    }
    out.kind = kind;
    out.body.assign(buf_.begin() + RECORD_HEADER_SIZE, buf_.begin() + total);
    buf_.erase(buf_.begin(), buf_.begin() + total);
    return true;
}

} // namespace jnx
