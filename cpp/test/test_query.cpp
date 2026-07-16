// test_query.cpp — jnxdb query server (cpp/db/query.{h,cpp}), focused on
// the SNAP bulk-snapshot command: header correctness and that every
// base64 body line round-trips back to the exact wire record
// make_dump_update() produces for its (ticker, group) row.
#include "db/query.h"

#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "common/minitest.h"
#include "db/tables.h"
#include "wire/record.h"

using namespace jnx;

namespace {

// Populates one book with distinctive static/state/book/trade values so a
// field regression in the SNAP path shows up as a byte mismatch.
UpdateRecord book_update(uint64_t epoch, uint64_t seq, const char* ticker,
                         uint32_t ref_price, uint32_t best_bid,
                         uint32_t best_ask) {
    UpdateRecord u;
    u.epoch = epoch;
    u.pub_seq = seq;              // dump rows must zero this out
    std::strcpy(u.session, "SIM0000001");
    u.exch_seq = seq;
    u.exch_ns = seq * 1000 + 7;
    u.trigger = 'A';
    std::strncpy(u.ticker, ticker, sizeof(u.ticker) - 1);
    u.ticker[sizeof(u.ticker) - 1] = '\0';
    std::strcpy(u.group, "DAY");
    std::strcpy(u.isin, "JP000000TEST");
    u.round_lot = 100;
    u.tick_table_id = 3;
    u.price_decimals = 1;
    u.upper_limit = ref_price + 1000;
    u.lower_limit = ref_price - 1000;
    u.trading_state = 'T';
    u.short_sell_restriction = '0';
    u.reference_price = ref_price;
    u.last_system_event = 'O';
    u.short_sell_price = best_bid;
    u.level_count_bid = 2;
    u.level_count_ask = 1;
    u.bids[0].price = best_bid;      u.bids[0].qty = 500; u.bids[0].order_count = 3;
    u.bids[1].price = best_bid - 10; u.bids[1].qty = 200; u.bids[1].order_count = 1;
    u.asks[0].price = best_ask;      u.asks[0].qty = 300; u.asks[0].order_count = 2;
    u.total_bid_qty = 700;
    u.total_ask_qty = 300;
    u.total_bid_orders = 4;
    u.total_ask_orders = 2;
    u.last_price = ref_price;
    u.last_qty = 50;
    u.last_match_number = seq * 10;
    u.last_trade_ns = seq * 2000;
    u.cum_qty = 1234;
    u.cum_turnover = 5678;
    u.trade_count = 9;
    u.delta_op = 'A';
    u.delta_order_number = seq;
    u.delta_side = 'B';
    u.delta_price = best_bid;
    u.delta_qty = 500;
    u.delta_order_type = ' ';
    return u;
}

// Minimal base64 decoder (inverse of query.cpp's base64_encode).
std::vector<unsigned char> base64_decode(const std::string& s) {
    static int rev[256];
    static bool init = false;
    if (!init) {
        for (int i = 0; i < 256; ++i) rev[i] = -1;
        const char* tbl =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        for (int i = 0; i < 64; ++i) rev[(unsigned char)tbl[i]] = i;
        init = true;
    }
    std::vector<unsigned char> out;
    int val = 0, bits = 0;
    for (size_t i = 0; i < s.size(); ++i) {
        unsigned char c = (unsigned char)s[i];
        if (c == '=') break;
        int d = rev[c];
        if (d < 0) continue;
        val = (val << 6) | d;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back((unsigned char)((val >> bits) & 0xFF));
        }
    }
    return out;
}

std::vector<std::string> split_lines(const std::string& s) {
    std::vector<std::string> out;
    size_t start = 0, nl;
    while ((nl = s.find('\n', start)) != std::string::npos) {
        out.push_back(s.substr(start, nl - start));
        start = nl + 1;
    }
    if (start < s.size()) out.push_back(s.substr(start));
    return out;
}

std::vector<unsigned char> encode_expected(const UpdateRecord& u) {
    unsigned char buf[MAX_RECORD_WIRE_SIZE];
    size_t n = encode_update(u, buf);
    return std::vector<unsigned char>(buf, buf + n);
}

} // namespace

TEST(snap_empty_db) {
    Tables t;
    QueryServer qs(t);
    std::string resp = qs.respond("SNAP");
    std::vector<std::string> lines = split_lines(resp);
    // header + terminator, no body rows.
    CHECK_EQ(lines.size(), static_cast<size_t>(2));
    CHECK(lines[0].find("count=0") != std::string::npos);
    CHECK(lines[0].find("epoch=0") != std::string::npos);
    CHECK_EQ(lines[1], std::string("."));
}

TEST(snap_header_reports_meta) {
    Tables t;
    t.apply_update(book_update(42, 800, "8306", 15000, 14990, 15010), false);
    QueryServer qs(t);
    std::vector<std::string> lines = split_lines(qs.respond("SNAP"));
    CHECK(lines[0].find("SNAP ") == 0);
    CHECK(lines[0].find("epoch=42") != std::string::npos);
    CHECK(lines[0].find("last_exch_seq=800") != std::string::npos);
    CHECK(lines[0].find("session=SIM0000001") != std::string::npos);
    CHECK(lines[0].find("count=1") != std::string::npos);
}

TEST(snap_rows_roundtrip_to_make_dump_update) {
    Tables t;
    // Apply in increasing exch_seq order (the DB's global dup guard drops
    // any UPDATE with exch_seq <= meta.last_exch_seq within an epoch).
    t.apply_update(book_update(7, 100, "8306", 15000, 14990, 15010), false);
    t.apply_update(book_update(7, 150, "7203", 22000, 21990, 22010), false);
    t.apply_update(book_update(7, 205, "9984", 30000, 29990, 30010), false);

    QueryServer qs(t);
    std::vector<std::string> lines = split_lines(qs.respond("SNAP"));

    // header + 3 rows + terminator
    CHECK_EQ(lines.size(), static_cast<size_t>(5));
    CHECK(lines[0].find("count=3") != std::string::npos);
    CHECK_EQ(lines.back(), std::string("."));

    size_t body_rows = 0;
    for (size_t i = 1; i + 1 < lines.size(); ++i) {
        std::vector<unsigned char> raw = base64_decode(lines[i]);
        CHECK_EQ(raw.size(), UPDATE_WIRE_SIZE);
        UpdateRecord got;
        const char* err = 0;
        CHECK(decode_update(&raw[0], raw.size(), got, &err));

        // Find the matching book and compare to what make_dump_update builds.
        Tables::Key key(std::string(got.ticker), std::string(got.group));
        Tables::BookMap::const_iterator b = t.books().find(key);
        CHECK(b != t.books().end());
        UpdateRecord expect = t.make_dump_update(b->first, b->second);
        CHECK(encode_expected(got) == encode_expected(expect));

        // Sync-critical envelope fields the client reconciles on.
        CHECK_EQ(got.epoch, 7u);
        CHECK_EQ(got.pub_seq, 0u);       // dump rows are not publications
        CHECK_EQ(got.trigger, '#');
        ++body_rows;
    }
    CHECK_EQ(body_rows, static_cast<size_t>(3));

    // Per-ticker exch_seq preserved (the merge key): 8306=100, 7203=150, 9984=205.
    UpdateRecord u8306 =
        t.make_dump_update(Tables::Key("8306", "DAY"), t.books().at(Tables::Key("8306", "DAY")));
    CHECK_EQ(u8306.exch_seq, 100u);
    CHECK_EQ(u8306.reference_price, 15000u);
    CHECK_EQ(u8306.bids[0].price, 14990u);
    CHECK_EQ(u8306.asks[0].price, 15010u);
}
