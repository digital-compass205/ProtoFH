// gen_record_vectors.cpp — writes cpp/test/vectors/records.bin: one encoded
// record of every kind plus UPDATE edge cases, with fixed deterministic
// values (no clock, no randomness — two runs produce identical bytes).
//
// The Python decoder test (tests/unit/test_records_py.py) hardcodes the
// exact values written here; change them only together, in one commit,
// along with docs/wire_spec.md if the layout itself moved (it is FROZEN).
//
// Usage: gen_record_vectors <output-path>
#include <cstdio>
#include <cstring>
#include <vector>

#include "wire/record.h"

using namespace jnx;

namespace {

void append(std::vector<unsigned char>& out, const unsigned char* p,
            size_t n, const char* what) {
    out.insert(out.end(), p, p + n);
    std::printf("  %-28s %3zu bytes (total %5zu)\n", what, n, out.size());
}

} // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <output-path>\n", argv[0]);
        return 2;
    }

    std::vector<unsigned char> out;
    unsigned char buf[MAX_RECORD_WIRE_SIZE];
    std::printf("gen_record_vectors: manifest\n");

    // 1. HELLO — FH connecting with an existing epoch.
    HelloRecord hello;
    hello.epoch = 7;
    hello.last_exch_seq = 12561;
    append(out, buf, encode_hello(hello, buf), "hello");

    // 2. GET_STATE (empty body).
    append(out, buf, encode_control(KIND_GET_STATE, buf), "get_state");

    // 3. RESET (empty body).
    append(out, buf, encode_control(KIND_RESET, buf), "reset");

    // 4. SYNC_BEGIN (empty body).
    append(out, buf, encode_control(KIND_SYNC_BEGIN, buf), "sync_begin");

    // 5. TICK — one tick-table row.
    TickRecord tick;
    tick.table_id = 1;
    tick.price_start = 30000;
    tick.tick_size = 5;
    append(out, buf, encode_tick(tick, buf), "tick");

    // 6. ORDER — one live DLP order row.
    OrderRecord order;
    order.order_number = 990001;
    std::strcpy(order.ticker, "8306");
    std::strcpy(order.group, "DAY");
    order.side = 'B';
    order.price = 15000;
    order.qty_remaining = 400;
    order.order_type = 'Q';
    append(out, buf, encode_order(order, buf), "order");

    // 7. UPDATE "full": 10+10 levels, all flags, 'U' delta.
    UpdateRecord full;
    full.epoch = 7;
    full.pub_seq = 123456;
    std::strcpy(full.session, "1697659284");
    full.exch_seq = 234751;
    full.exch_ns = 1234567890123456789ULL;
    full.trigger = 'U';
    std::strcpy(full.ticker, "8306");
    std::strcpy(full.group, "DAY");
    std::strcpy(full.isin, "JP3902400005");
    full.round_lot = 100;
    full.tick_table_id = 1;
    full.price_decimals = 1;
    full.upper_limit = 200000;
    full.lower_limit = 100000;
    full.flags = FLAG_DIRECTORY_SEEN | FLAG_ORDER_COLLISION_SEEN;  // 3
    full.trading_state = 'T';
    full.short_sell_restriction = '0';
    full.reference_price = 15000;
    full.last_system_event = 'Q';
    full.level_count_bid = 10;
    full.level_count_ask = 10;
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        full.bids[i].price = 15000 - static_cast<uint32_t>(i) * 10;
        full.bids[i].qty = 100 * static_cast<uint32_t>(i + 1);
        full.bids[i].order_count = static_cast<uint32_t>(i + 1);
        full.asks[i].price = 15010 + static_cast<uint32_t>(i) * 10;
        full.asks[i].qty = 200 * static_cast<uint32_t>(i + 1);
        full.asks[i].order_count = static_cast<uint32_t>(i + 2);
    }
    full.total_bid_qty = 5500;
    full.total_ask_qty = 11000;
    full.total_bid_orders = 55;
    full.total_ask_orders = 65;
    full.last_price = 15000;
    full.last_qty = 300;
    full.last_match_number = 987654321;
    full.last_trade_ns = 1234567890000000000ULL;
    full.cum_qty = 400000;
    full.cum_turnover = 6000000000ULL;
    full.trade_count = 4242;
    full.delta_op = 'U';
    full.delta_order_number = 999002;
    full.delta_orig_order_number = 999001;
    full.delta_side = 'B';
    full.delta_price = 14990;
    full.delta_qty = 500;
    full.delta_order_type = ' ';
    append(out, buf, encode_update(full, buf), "update_full");

    // 8. UPDATE "sync row": empty book, NO_PRICE ref, auto-created book
    //    (no directory), '#' trigger and '#' delta, unknown states.
    UpdateRecord sync;
    sync.epoch = 7;
    sync.pub_seq = 1;
    std::strcpy(sync.session, "1697659284");
    sync.exch_seq = 12562;
    sync.exch_ns = 34200000000042ULL;
    sync.trigger = '#';
    std::strcpy(sync.ticker, "9999");
    std::strcpy(sync.group, "NGHT");
    // static section stays zero/empty: directory never seen (flags 0)
    sync.trading_state = '?';
    sync.short_sell_restriction = '?';
    sync.reference_price = 0x7FFFFFFFu;  // NO_PRICE
    sync.delta_op = '#';
    append(out, buf, encode_update(sync, buf), "update_sync_empty_book");

    // 9. UPDATE "trade": 1+1 levels, 'E' delta fully filling an order.
    UpdateRecord trade;
    trade.epoch = 7;
    trade.pub_seq = 123457;
    std::strcpy(trade.session, "1697659284");
    trade.exch_seq = 234752;
    trade.exch_ns = 1234567890123456790ULL;
    trade.trigger = 'E';
    std::strcpy(trade.ticker, "7203");
    std::strcpy(trade.group, "DAY");
    std::strcpy(trade.isin, "JP3633400001");
    trade.round_lot = 100;
    trade.tick_table_id = 2;
    trade.price_decimals = 1;
    trade.upper_limit = 999999;
    trade.lower_limit = 1;
    trade.flags = FLAG_DIRECTORY_SEEN;  // 1
    trade.trading_state = 'T';
    trade.short_sell_restriction = '1';
    trade.reference_price = 25000;
    trade.last_system_event = 'Q';
    trade.level_count_bid = 1;
    trade.level_count_ask = 1;
    trade.bids[0].price = 24990;
    trade.bids[0].qty = 1000;
    trade.bids[0].order_count = 3;
    trade.asks[0].price = 25010;
    trade.asks[0].qty = 4294967295u;  // max u32 qty at a level
    trade.asks[0].order_count = 1;
    trade.total_bid_qty = 1000;
    trade.total_ask_qty = 4294967295u;
    trade.total_bid_orders = 3;
    trade.total_ask_orders = 1;
    trade.last_price = 25000;
    trade.last_qty = 200;
    trade.last_match_number = 555001;
    trade.last_trade_ns = 1234567890123456790ULL;
    trade.cum_qty = 200;
    trade.cum_turnover = 5000000;
    trade.trade_count = 1;
    trade.delta_op = 'E';
    trade.delta_order_number = 424242;
    trade.delta_orig_order_number = 0;
    trade.delta_side = 'S';
    trade.delta_price = 25000;
    trade.delta_qty = 0;  // filled to zero -> row deleted
    trade.delta_order_type = 'Q';
    append(out, buf, encode_update(trade, buf), "update_trade_exec");

    // 10. SYNC_END — dump meta.
    SyncEndRecord se;
    std::strcpy(se.session, "1697659284");
    se.last_exch_seq = 234751;
    se.epoch = 7;
    append(out, buf, encode_sync_end(se, buf), "sync_end");

    std::FILE* f = std::fopen(argv[1], "wb");
    if (f == 0) {
        std::fprintf(stderr, "cannot open %s for writing\n", argv[1]);
        return 1;
    }
    size_t written = std::fwrite(&out[0], 1, out.size(), f);
    std::fclose(f);
    if (written != out.size()) {
        std::fprintf(stderr, "short write to %s\n", argv[1]);
        return 1;
    }
    std::printf("wrote %zu bytes (10 records) -> %s\n", out.size(), argv[1]);
    return 0;
}
