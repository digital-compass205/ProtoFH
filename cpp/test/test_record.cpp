// test_record.cpp — round-trip + framing tests for the record codec
// (cpp/wire/record.{h,cpp}); layout per docs/wire_spec.md v1 (FROZEN).
#include "wire/record.h"

#include <cstring>
#include <string>
#include <vector>

#include "common/minitest.h"

using namespace jnx;

namespace {

// A fully populated UPDATE with 10+10 levels and a 'U' delta.
UpdateRecord make_full_update() {
    UpdateRecord u;
    u.epoch = 7;
    u.pub_seq = 123456;
    std::strcpy(u.session, "1697659284");
    u.exch_seq = 234751;
    u.exch_ns = 1234567890123456789ULL;
    u.trigger = 'U';
    std::strcpy(u.ticker, "8306");
    std::strcpy(u.group, "DAY");
    std::strcpy(u.isin, "JP3902400005");
    u.round_lot = 100;
    u.tick_table_id = 1;
    u.price_decimals = 1;
    u.upper_limit = 200000;
    u.lower_limit = 100000;
    u.flags = FLAG_DIRECTORY_SEEN | FLAG_ORDER_COLLISION_SEEN;
    u.trading_state = 'T';
    u.short_sell_restriction = '0';
    u.reference_price = 15000;
    u.last_system_event = 'Q';
    u.level_count_bid = 10;
    u.level_count_ask = 10;
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        u.bids[i].price = 15000 - static_cast<uint32_t>(i) * 10;
        u.bids[i].qty = 100 * static_cast<uint32_t>(i + 1);
        u.bids[i].order_count = static_cast<uint32_t>(i + 1);
        u.asks[i].price = 15010 + static_cast<uint32_t>(i) * 10;
        u.asks[i].qty = 200 * static_cast<uint32_t>(i + 1);
        u.asks[i].order_count = static_cast<uint32_t>(i + 2);
    }
    u.total_bid_qty = 5500;
    u.total_ask_qty = 11000;
    u.total_bid_orders = 55;
    u.total_ask_orders = 65;
    u.last_price = 15000;
    u.last_qty = 300;
    u.last_match_number = 987654321;
    u.last_trade_ns = 1234567890000000000ULL;
    u.cum_qty = 400000;
    u.cum_turnover = 6000000000ULL;
    u.trade_count = 4242;
    u.delta_op = 'U';
    u.delta_order_number = 999002;
    u.delta_orig_order_number = 999001;
    u.delta_side = 'B';
    u.delta_price = 14990;
    u.delta_qty = 500;
    u.delta_order_type = ' ';
    return u;
}

bool update_eq(const UpdateRecord& a, const UpdateRecord& b) {
    if (a.epoch != b.epoch || a.pub_seq != b.pub_seq ||
        std::strcmp(a.session, b.session) != 0 || a.exch_seq != b.exch_seq ||
        a.exch_ns != b.exch_ns || a.trigger != b.trigger ||
        std::strcmp(a.ticker, b.ticker) != 0 ||
        std::strcmp(a.group, b.group) != 0) {
        return false;
    }
    if (std::strcmp(a.isin, b.isin) != 0 || a.round_lot != b.round_lot ||
        a.tick_table_id != b.tick_table_id ||
        a.price_decimals != b.price_decimals ||
        a.upper_limit != b.upper_limit || a.lower_limit != b.lower_limit ||
        a.flags != b.flags) {
        return false;
    }
    if (a.trading_state != b.trading_state ||
        a.short_sell_restriction != b.short_sell_restriction ||
        a.reference_price != b.reference_price ||
        a.last_system_event != b.last_system_event) {
        return false;
    }
    if (a.level_count_bid != b.level_count_bid ||
        a.level_count_ask != b.level_count_ask) {
        return false;
    }
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        if (a.bids[i].price != b.bids[i].price ||
            a.bids[i].qty != b.bids[i].qty ||
            a.bids[i].order_count != b.bids[i].order_count ||
            a.asks[i].price != b.asks[i].price ||
            a.asks[i].qty != b.asks[i].qty ||
            a.asks[i].order_count != b.asks[i].order_count) {
            return false;
        }
    }
    if (a.total_bid_qty != b.total_bid_qty ||
        a.total_ask_qty != b.total_ask_qty ||
        a.total_bid_orders != b.total_bid_orders ||
        a.total_ask_orders != b.total_ask_orders) {
        return false;
    }
    if (a.last_price != b.last_price || a.last_qty != b.last_qty ||
        a.last_match_number != b.last_match_number ||
        a.last_trade_ns != b.last_trade_ns || a.cum_qty != b.cum_qty ||
        a.cum_turnover != b.cum_turnover || a.trade_count != b.trade_count) {
        return false;
    }
    return a.delta_op == b.delta_op &&
           a.delta_order_number == b.delta_order_number &&
           a.delta_orig_order_number == b.delta_orig_order_number &&
           a.delta_side == b.delta_side && a.delta_price == b.delta_price &&
           a.delta_qty == b.delta_qty &&
           a.delta_order_type == b.delta_order_type;
}

} // namespace

TEST(update_size_is_frozen) {
    CHECK_EQ(UPDATE_WIRE_SIZE, static_cast<size_t>(433));
    unsigned char buf[UPDATE_WIRE_SIZE];
    CHECK_EQ(encode_update(make_full_update(), buf), UPDATE_WIRE_SIZE);
    CHECK_EQ(encode_update(UpdateRecord(), buf), UPDATE_WIRE_SIZE);
}

TEST(update_round_trip_full) {
    UpdateRecord in = make_full_update();
    unsigned char buf[UPDATE_WIRE_SIZE];
    size_t n = encode_update(in, buf);
    CHECK_EQ(n, UPDATE_WIRE_SIZE);
    UpdateRecord out;
    const char* err = 0;
    CHECK(decode_update(buf, n, out, &err));
    CHECK(update_eq(in, out));
}

TEST(update_round_trip_empty_book_and_no_price) {
    UpdateRecord in;  // default: everything zero / empty
    in.epoch = 1;
    in.pub_seq = 1;
    std::strcpy(in.session, "S");
    in.exch_seq = 12562;
    in.exch_ns = 42;
    in.trigger = '#';
    std::strcpy(in.ticker, "9999");
    std::strcpy(in.group, "NGHT");
    in.reference_price = 0x7FFFFFFFu;  // NO_PRICE sentinel
    in.trading_state = '?';
    in.short_sell_restriction = '?';
    in.delta_op = '#';

    unsigned char buf[UPDATE_WIRE_SIZE];
    size_t n = encode_update(in, buf);
    CHECK_EQ(n, UPDATE_WIRE_SIZE);
    UpdateRecord out;
    const char* err = 0;
    CHECK(decode_update(buf, n, out, &err));
    CHECK(update_eq(in, out));
    CHECK_EQ(out.reference_price, 0x7FFFFFFFu);
    CHECK_EQ(static_cast<int>(out.level_count_bid), 0);
    CHECK_EQ(static_cast<int>(out.level_count_ask), 0);
    // empty ticker's static section round-trips as empty strings
    CHECK_EQ(std::string(out.isin), std::string(""));
}

TEST(update_level_slots_beyond_count_are_zero_on_wire) {
    UpdateRecord in = make_full_update();
    in.level_count_bid = 2;
    in.level_count_ask = 1;
    // bids[2..9]/asks[1..9] still hold junk in the struct — the encoder
    // must zero them on the wire anyway.
    unsigned char buf[UPDATE_WIRE_SIZE];
    encode_update(in, buf);
    const size_t bids_off = 98;  // docs/wire_spec.md book section offsets
    const size_t asks_off = 218;
    for (size_t i = bids_off + 2 * 12; i < bids_off + 10 * 12; ++i) {
        CHECK_EQ(static_cast<int>(buf[i]), 0);
    }
    for (size_t i = asks_off + 1 * 12; i < asks_off + 10 * 12; ++i) {
        CHECK_EQ(static_cast<int>(buf[i]), 0);
    }
    // decode gives back zeroed slots
    UpdateRecord out;
    CHECK(decode_update(buf, UPDATE_WIRE_SIZE, out, 0));
    CHECK_EQ(out.bids[2].price, 0u);
    CHECK_EQ(out.asks[1].qty, 0u);
    CHECK_EQ(out.bids[0].price, in.bids[0].price);
    CHECK_EQ(out.asks[0].price, in.asks[0].price);
}

TEST(update_no_uninitialized_bytes_two_payloads) {
    // Two encodes with different payloads: every byte that should be equal
    // (header, zero-filled slots, reserved) is equal, i.e. no byte depends
    // on uninitialized memory. Belt-and-braces vs. the zero-fill test.
    UpdateRecord a;  // all-empty
    UpdateRecord b = make_full_update();
    b.level_count_bid = 0;
    b.level_count_ask = 0;
    unsigned char ba[UPDATE_WIRE_SIZE];
    unsigned char bb[UPDATE_WIRE_SIZE];
    // poison the buffers differently first
    std::memset(ba, 0xAA, sizeof(ba));
    std::memset(bb, 0x55, sizeof(bb));
    encode_update(a, ba);
    encode_update(b, bb);
    // level slot region must be all-zero in both
    for (size_t i = 98; i < 98 + 240; ++i) {
        CHECK_EQ(static_cast<int>(ba[i]), 0);
        CHECK_EQ(static_cast<int>(bb[i]), 0);
    }
    // reserved header bytes zero in both
    CHECK_EQ(static_cast<int>(ba[6]), 0);
    CHECK_EQ(static_cast<int>(ba[7]), 0);
    CHECK_EQ(static_cast<int>(bb[6]), 0);
    CHECK_EQ(static_cast<int>(bb[7]), 0);
}

TEST(update_delta_ops_round_trip) {
    const char ops[] = {'A', 'E', 'D', 'U', '#'};
    for (size_t i = 0; i < sizeof(ops); ++i) {
        UpdateRecord in = make_full_update();
        in.delta_op = ops[i];
        if (ops[i] == '#') {
            in.delta_order_number = 0;
            in.delta_orig_order_number = 0;
            in.delta_side = '\0';
            in.delta_price = 0;
            in.delta_qty = 0;
            in.delta_order_type = '\0';
        } else if (ops[i] != 'U') {
            in.delta_orig_order_number = 0;
        }
        unsigned char buf[UPDATE_WIRE_SIZE];
        encode_update(in, buf);
        UpdateRecord out;
        const char* err = 0;
        CHECK(decode_update(buf, UPDATE_WIRE_SIZE, out, &err));
        CHECK(update_eq(in, out));
    }
}

TEST(update_all_flag_combinations) {
    for (unsigned f = 0; f < 4; ++f) {
        UpdateRecord in = make_full_update();
        in.flags = static_cast<uint8_t>(f);
        unsigned char buf[UPDATE_WIRE_SIZE];
        encode_update(in, buf);
        UpdateRecord out;
        CHECK(decode_update(buf, UPDATE_WIRE_SIZE, out, 0));
        CHECK_EQ(static_cast<unsigned>(out.flags), f);
    }
}

TEST(order_round_trip) {
    OrderRecord in;
    in.order_number = 990001;
    std::strcpy(in.ticker, "8306");
    std::strcpy(in.group, "DAY");
    in.side = 'B';
    in.price = 15000;
    in.qty_remaining = 400;
    in.order_type = 'Q';
    unsigned char buf[ORDER_WIRE_SIZE];
    CHECK_EQ(encode_order(in, buf), ORDER_WIRE_SIZE);
    OrderRecord out;
    const char* err = 0;
    CHECK(decode_order(buf, ORDER_WIRE_SIZE, out, &err));
    CHECK_EQ(out.order_number, in.order_number);
    CHECK_EQ(std::string(out.ticker), std::string(in.ticker));
    CHECK_EQ(std::string(out.group), std::string(in.group));
    CHECK_EQ(out.side, in.side);
    CHECK_EQ(out.price, in.price);
    CHECK_EQ(out.qty_remaining, in.qty_remaining);
    CHECK_EQ(out.order_type, in.order_type);
}

TEST(tick_round_trip) {
    TickRecord in;
    in.table_id = 1;
    in.price_start = 30000;
    in.tick_size = 5;
    unsigned char buf[TICK_WIRE_SIZE];
    CHECK_EQ(encode_tick(in, buf), TICK_WIRE_SIZE);
    TickRecord out;
    CHECK(decode_tick(buf, TICK_WIRE_SIZE, out, 0));
    CHECK_EQ(out.table_id, 1u);
    CHECK_EQ(out.price_start, 30000u);
    CHECK_EQ(out.tick_size, 5u);
}

TEST(hello_round_trip) {
    HelloRecord in;
    in.epoch = 7;
    in.last_exch_seq = 12561;
    unsigned char buf[HELLO_WIRE_SIZE];
    CHECK_EQ(encode_hello(in, buf), HELLO_WIRE_SIZE);
    HelloRecord out;
    CHECK(decode_hello(buf, HELLO_WIRE_SIZE, out, 0));
    CHECK_EQ(out.epoch, 7u);
    CHECK_EQ(out.last_exch_seq, 12561u);
}

TEST(sync_end_round_trip) {
    SyncEndRecord in;
    std::strcpy(in.session, "1697659284");
    in.last_exch_seq = 234751;
    in.epoch = 7;
    unsigned char buf[SYNC_END_WIRE_SIZE];
    CHECK_EQ(encode_sync_end(in, buf), SYNC_END_WIRE_SIZE);
    SyncEndRecord out;
    CHECK(decode_sync_end(buf, SYNC_END_WIRE_SIZE, out, 0));
    CHECK_EQ(std::string(out.session), std::string("1697659284"));
    CHECK_EQ(out.last_exch_seq, 234751u);
    CHECK_EQ(out.epoch, 7u);
}

TEST(control_records) {
    unsigned char buf[CONTROL_WIRE_SIZE];
    const char kinds[] = {KIND_SYNC_BEGIN, KIND_GET_STATE, KIND_RESET};
    for (size_t i = 0; i < sizeof(kinds); ++i) {
        CHECK_EQ(encode_control(kinds[i], buf), CONTROL_WIRE_SIZE);
        char kind = '\0';
        uint16_t body_len = 9;
        const char* err = 0;
        CHECK(decode_header(buf, CONTROL_WIRE_SIZE, &kind, &body_len, &err));
        CHECK_EQ(kind, kinds[i]);
        CHECK_EQ(static_cast<int>(body_len), 0);
    }
    // not a control kind
    CHECK_EQ(encode_control(KIND_UPDATE, buf), static_cast<size_t>(0));
}

TEST(decode_rejects_corruption) {
    UpdateRecord in = make_full_update();
    unsigned char buf[UPDATE_WIRE_SIZE];
    encode_update(in, buf);
    UpdateRecord out;
    const char* err = 0;

    // bad magic
    unsigned char bad[UPDATE_WIRE_SIZE];
    std::memcpy(bad, buf, sizeof(buf));
    bad[0] = 0x00;
    CHECK(!decode_update(bad, sizeof(bad), out, &err));
    CHECK(err != 0);

    // bad version
    std::memcpy(bad, buf, sizeof(buf));
    bad[2] = 2;
    err = 0;
    CHECK(!decode_update(bad, sizeof(bad), out, &err));
    CHECK(err != 0);

    // unknown kind
    std::memcpy(bad, buf, sizeof(buf));
    bad[3] = 'x';
    err = 0;
    CHECK(!decode_update(bad, sizeof(bad), out, &err));

    // wrong body_len for the kind
    std::memcpy(bad, buf, sizeof(buf));
    bad[5] = static_cast<unsigned char>(bad[5] + 1);
    err = 0;
    CHECK(!decode_update(bad, sizeof(bad), out, &err));

    // kind mismatch (an ORDER buffer fed to decode_update)
    OrderRecord o;
    unsigned char obuf[ORDER_WIRE_SIZE];
    encode_order(o, obuf);
    err = 0;
    CHECK(!decode_update(obuf, sizeof(obuf), out, &err));

    // truncated buffer
    err = 0;
    CHECK(!decode_update(buf, UPDATE_WIRE_SIZE - 1, out, &err));
    err = 0;
    CHECK(!decode_update(buf, 3, out, &err));

    // excessive level count
    std::memcpy(bad, buf, sizeof(buf));
    bad[96] = 11;  // level_count_bid offset per docs/wire_spec.md
    err = 0;
    CHECK(!decode_update(bad, sizeof(bad), out, &err));
}

TEST(framer_byte_at_a_time) {
    // Build a stream: HELLO + UPDATE + SYNC_END, feed one byte at a time.
    std::vector<unsigned char> stream;
    unsigned char tmp[UPDATE_WIRE_SIZE];

    HelloRecord h;
    h.epoch = 7;
    h.last_exch_seq = 12561;
    size_t n = encode_hello(h, tmp);
    stream.insert(stream.end(), tmp, tmp + n);

    UpdateRecord u = make_full_update();
    n = encode_update(u, tmp);
    stream.insert(stream.end(), tmp, tmp + n);

    SyncEndRecord se;
    std::strcpy(se.session, "1697659284");
    se.last_exch_seq = 234751;
    se.epoch = 7;
    n = encode_sync_end(se, tmp);
    stream.insert(stream.end(), tmp, tmp + n);

    RecordFramer framer;
    std::vector<RawRecord> got;
    RawRecord rec;
    for (size_t i = 0; i < stream.size(); ++i) {
        framer.feed(&stream[i], 1);
        while (framer.next(rec)) {
            got.push_back(rec);
        }
    }
    CHECK(!framer.corrupt());
    CHECK_EQ(got.size(), static_cast<size_t>(3));
    CHECK_EQ(got[0].kind, KIND_HELLO);
    CHECK_EQ(got[0].body.size(), HELLO_BODY_SIZE);
    CHECK_EQ(got[1].kind, KIND_UPDATE);
    CHECK_EQ(got[1].body.size(), UPDATE_BODY_SIZE);
    CHECK_EQ(got[2].kind, KIND_SYNC_END);
    CHECK_EQ(got[2].body.size(), SYNC_END_BODY_SIZE);

    // The re-framed UPDATE decodes identically. Rebuild a full record
    // buffer: header + body (next() strips the header).
    std::vector<unsigned char> whole(RECORD_HEADER_SIZE + got[1].body.size());
    encode_update(u, tmp);  // easiest header source: re-encode
    std::memcpy(&whole[0], tmp, RECORD_HEADER_SIZE);
    std::memcpy(&whole[RECORD_HEADER_SIZE], &got[1].body[0],
                got[1].body.size());
    UpdateRecord out;
    CHECK(decode_update(&whole[0], whole.size(), out, 0));
    CHECK(update_eq(u, out));
}

TEST(framer_split_header) {
    unsigned char tmp[HELLO_WIRE_SIZE];
    HelloRecord h;
    h.epoch = 3;
    encode_hello(h, tmp);
    RecordFramer framer;
    RawRecord rec;
    framer.feed(tmp, 5);  // header split mid-way
    CHECK(!framer.next(rec));
    framer.feed(tmp + 5, 5);  // header complete, body partial
    CHECK(!framer.next(rec));
    framer.feed(tmp + 10, HELLO_WIRE_SIZE - 10);
    CHECK(framer.next(rec));
    CHECK_EQ(rec.kind, KIND_HELLO);
    CHECK(!framer.next(rec));
    CHECK_EQ(framer.corrupt(), false);
}

TEST(framer_corrupt_stream) {
    unsigned char junk[RECORD_HEADER_SIZE] = {0xDE, 0xAD, 0xBE, 0xEF,
                                              0x00, 0x00, 0x00, 0x00};
    RecordFramer framer;
    RawRecord rec;
    framer.feed(junk, sizeof(junk));
    CHECK(!framer.next(rec));
    CHECK(framer.corrupt());
    CHECK(framer.corrupt_reason() != 0);
    // once corrupt, further feeds are ignored
    unsigned char good[HELLO_WIRE_SIZE];
    HelloRecord h;
    encode_hello(h, good);
    framer.feed(good, sizeof(good));
    CHECK(!framer.next(rec));
}

TEST(record_body_len_table) {
    CHECK_EQ(record_body_len(KIND_UPDATE), 425);
    CHECK_EQ(record_body_len(KIND_ORDER), 26);
    CHECK_EQ(record_body_len(KIND_TICK), 12);
    CHECK_EQ(record_body_len(KIND_SYNC_BEGIN), 0);
    CHECK_EQ(record_body_len(KIND_SYNC_END), 26);
    CHECK_EQ(record_body_len(KIND_GET_STATE), 0);
    CHECK_EQ(record_body_len(KIND_HELLO), 16);
    CHECK_EQ(record_body_len(KIND_RESET), 0);
    CHECK_EQ(record_body_len('x'), -1);
}
