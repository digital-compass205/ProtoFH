// test_market.cpp — unit tests for every JNX_PLAN.md §3.3 gotcha, plus a
// randomized store-vs-levels invariant check.
#include "market/market.h"

#include <cstdio>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#include "common/minitest.h"

namespace {

jnx::ItchMsg make_add(uint64_t number, const char* oid, const char* group,
                      char side, uint32_t qty, uint32_t price,
                      bool with_attrs = false, char order_type = ' ') {
    jnx::ItchMsg m;
    m.type = with_attrs ? 'F' : 'A';
    m.ns = 1;
    m.order_number = number;
    m.side = side;
    m.qty = qty;
    std::snprintf(m.orderbook_id, sizeof(m.orderbook_id), "%s", oid);
    std::snprintf(m.group, sizeof(m.group), "%s", group);
    m.price = price;
    m.order_type = order_type;
    return m;
}

jnx::ItchMsg make_exec(uint64_t number, uint32_t qty, uint64_t match) {
    jnx::ItchMsg m;
    m.type = 'E';
    m.ns = 2;
    m.order_number = number;
    m.executed_qty = qty;
    m.match_number = match;
    return m;
}

jnx::ItchMsg make_delete(uint64_t number) {
    jnx::ItchMsg m;
    m.type = 'D';
    m.ns = 3;
    m.order_number = number;
    return m;
}

jnx::ItchMsg make_replace(uint64_t orig, uint64_t newnum, uint32_t qty,
                          uint32_t price) {
    jnx::ItchMsg m;
    m.type = 'U';
    m.ns = 4;
    m.orig_order_number = orig;
    m.new_order_number = newnum;
    m.qty = qty;
    m.price = price;
    return m;
}

// Invariant: for every book+side, the levels' total qty equals the sum of
// live orders' remaining qty, and level count/qty per price match.
bool store_levels_consistent(const jnx::Market& market) {
    typedef std::pair<std::string, char> SideKey;
    std::map<std::pair<SideKey, uint32_t>, uint64_t> want;
    std::map<SideKey, uint64_t> want_total;
    const std::unordered_map<uint64_t, jnx::Order>& orders =
        market.books.orders();
    for (std::unordered_map<uint64_t, jnx::Order>::const_iterator it =
             orders.begin();
         it != orders.end(); ++it) {
        const jnx::Order& o = it->second;
        want[std::make_pair(SideKey(o.orderbook_id, o.side), o.price)] +=
            o.remaining_qty;
        want_total[SideKey(o.orderbook_id, o.side)] += o.remaining_qty;
    }
    const std::map<std::string, jnx::Book>& books = market.books.books();
    for (std::map<std::string, jnx::Book>::const_iterator bit = books.begin();
         bit != books.end(); ++bit) {
        const char sides[2] = {'B', 'S'};
        for (int s = 0; s < 2; ++s) {
            const jnx::SideLevels& sl =
                sides[s] == 'B' ? bit->second.bids() : bit->second.asks();
            uint64_t total = 0;
            uint32_t total_orders = 0;
            for (jnx::SideLevels::LevelMap::const_iterator it =
                     sl.ascending().begin();
                 it != sl.ascending().end(); ++it) {
                if (it->second.qty == 0) return false; // empty level retained
                std::map<std::pair<SideKey, uint32_t>, uint64_t>::iterator w =
                    want.find(std::make_pair(SideKey(bit->first, sides[s]),
                                             it->first));
                if (w == want.end() || w->second != it->second.qty)
                    return false;
                want.erase(w);
                total += it->second.qty;
                total_orders += it->second.orders;
            }
            std::map<SideKey, uint64_t>::iterator wt =
                want_total.find(SideKey(bit->first, sides[s]));
            uint64_t want_t = wt == want_total.end() ? 0 : wt->second;
            if (total != want_t) return false;
            // Native running totals must agree with the recomputed sums.
            if (sl.total_qty() != total) return false;
            if (sl.total_orders() != total_orders) return false;
        }
    }
    return want.empty(); // no store order without a matching level
}

} // namespace

TEST(ref_price_a_never_enters_book) {
    jnx::Market market;
    jnx::ItchMsg ref = make_add(0, "8306", "DAY", 'B', 0, 15000);
    jnx::ApplyResult r = market.apply(ref);
    CHECK(r.applied);
    CHECK_EQ(static_cast<int>(r.sections), static_cast<int>(jnx::SEC_STATE));
    CHECK(!r.has_delta);
    CHECK_EQ(market.books.books().size(), static_cast<size_t>(0));
    CHECK_EQ(market.books.orders().size(), static_cast<size_t>(0));
    const jnx::Instrument& inst = market.refdata.get("8306");
    CHECK_EQ(inst.reference_price, static_cast<int64_t>(15000));
    CHECK_EQ(inst.group, std::string("DAY"));

    // NO_PRICE sentinel is stored verbatim.
    jnx::ItchMsg ref2 = make_add(0, "8306", "DAY", 'B', 0, jnx::NO_PRICE);
    market.apply(ref2);
    CHECK_EQ(market.refdata.get("8306").reference_price,
             static_cast<int64_t>(jnx::NO_PRICE));
    CHECK_EQ(market.books.ref_price_ignored, static_cast<uint64_t>(0));
}

TEST(execute_passive_price_multifill_and_removal_at_zero) {
    jnx::Market market;
    market.apply(make_add(42, "8306", "DAY", 'S', 300, 15000));
    // Aggressor price is unknown to the feed; trade price must be the
    // STORED order's price. Three fills, one trade each.
    jnx::ApplyResult r1 = market.apply(make_exec(42, 100, 1001));
    CHECK(r1.has_trade);
    CHECK_EQ(r1.trade.price, static_cast<uint32_t>(15000));
    CHECK_EQ(r1.trade.qty, static_cast<uint32_t>(100));
    CHECK_EQ(std::string(1, r1.trade.side), std::string("S"));
    CHECK_EQ(r1.trade.match_number, static_cast<uint64_t>(1001));
    CHECK_EQ(market.books.orders().at(42).remaining_qty,
             static_cast<uint32_t>(200));

    jnx::ApplyResult r2 = market.apply(make_exec(42, 150, 1002));
    CHECK(r2.has_trade);
    CHECK_EQ(market.books.orders().at(42).remaining_qty,
             static_cast<uint32_t>(50));

    jnx::ApplyResult r3 = market.apply(make_exec(42, 50, 1003));
    CHECK(r3.has_trade);
    // Removed at exactly zero.
    CHECK(market.books.orders().find(42) == market.books.orders().end());
    CHECK_EQ(market.books.execution_count, static_cast<uint64_t>(3));
    CHECK_EQ(market.books.executed_volume, static_cast<uint64_t>(300));
    CHECK_EQ(market.tape.trade_count, static_cast<uint64_t>(3));
    // Level fully consumed.
    const jnx::Book& book = market.books.books().at("8306");
    CHECK_EQ(book.asks().size(), static_cast<size_t>(0));
}

TEST(over_execution_clamps_to_zero) {
    jnx::Market market;
    market.apply(make_add(7, "8306", "DAY", 'B', 100, 20000));
    jnx::ApplyResult r = market.apply(make_exec(7, 500, 2001));
    CHECK(r.has_trade);
    CHECK_EQ(r.trade.qty, static_cast<uint32_t>(100)); // clamped
    CHECK(market.books.orders().find(7) == market.books.orders().end());
    CHECK_EQ(market.books.clamped_executions, static_cast<uint64_t>(1));
    CHECK_EQ(market.books.executed_volume, static_cast<uint64_t>(100));
    CHECK(store_levels_consistent(market));
}

TEST(orphan_execute_delete_replace_counted_never_fatal) {
    jnx::Market market;
    jnx::ApplyResult re = market.apply(make_exec(999, 10, 1));
    CHECK(re.applied);
    CHECK(!re.has_trade);
    CHECK(!re.has_delta);
    CHECK_EQ(re.ticker, std::string(""));
    market.apply(make_delete(998));
    market.apply(make_replace(997, 1997, 100, 5000));
    CHECK_EQ(market.books.orphan_executes, static_cast<uint64_t>(1));
    CHECK_EQ(market.books.orphan_deletes, static_cast<uint64_t>(1));
    CHECK_EQ(market.books.orphan_replaces, static_cast<uint64_t>(1));
    // Orphan U inserts NOTHING.
    CHECK_EQ(market.books.orders().size(), static_cast<size_t>(0));
    CHECK_EQ(market.tape.trade_count, static_cast<uint64_t>(0));
}

TEST(replace_inherits_from_original) {
    jnx::Market market;
    market.apply(make_add(10, "7203", "NGHT", 'S', 400, 26000));
    jnx::ApplyResult r = market.apply(make_replace(10, 11, 250, 26500));
    CHECK(r.has_delta);
    CHECK_EQ(std::string(1, r.delta_op), std::string("U"));
    CHECK_EQ(r.order_number, static_cast<uint64_t>(11));
    CHECK_EQ(r.orig_order_number, static_cast<uint64_t>(10));
    CHECK_EQ(r.ticker, std::string("7203"));
    CHECK(market.books.orders().find(10) == market.books.orders().end());
    const jnx::Order& o = market.books.orders().at(11);
    CHECK_EQ(o.orderbook_id, std::string("7203")); // inherited
    CHECK_EQ(o.group, std::string("NGHT"));        // inherited
    CHECK_EQ(std::string(1, o.side), std::string("S")); // inherited
    CHECK_EQ(o.price, static_cast<uint32_t>(26500)); // from message
    CHECK_EQ(o.remaining_qty, static_cast<uint32_t>(250)); // from message
    CHECK(store_levels_consistent(market));
}

TEST(collision_replaces_stale_order) {
    jnx::Market market;
    market.apply(make_add(5, "8306", "DAY", 'B', 100, 15000));
    market.apply(make_add(5, "7203", "DAY", 'S', 200, 26000));
    CHECK_EQ(market.books.collisions, static_cast<uint64_t>(1));
    CHECK_EQ(market.books.orders().size(), static_cast<size_t>(1));
    const jnx::Order& o = market.books.orders().at(5);
    CHECK_EQ(o.orderbook_id, std::string("7203"));
    // The stale order's level must be gone.
    CHECK_EQ(market.books.books().at("8306").bids().size(),
             static_cast<size_t>(0));
    CHECK(store_levels_consistent(market));
}

TEST(auto_create_and_absence_defaults) {
    jnx::Market market;
    // First reference via H: auto-created, directory_missing until R.
    jnx::ItchMsg h;
    h.type = 'H';
    h.ns = 5;
    std::snprintf(h.orderbook_id, sizeof(h.orderbook_id), "9984");
    std::snprintf(h.group, sizeof(h.group), "DAY");
    h.state = 'T';
    market.apply(h);
    const jnx::Instrument& inst = market.refdata.get("9984");
    CHECK(inst.directory_missing);
    CHECK_EQ(std::string(1, inst.trading_state), std::string("T"));
    // Absence defaults on a fresh instrument (§3.3(4)).
    const jnx::Instrument& fresh = market.refdata.get("0000");
    CHECK_EQ(std::string(1, fresh.trading_state), std::string("V"));
    CHECK_EQ(std::string(1, fresh.short_sell_state), std::string("0"));
    CHECK(fresh.directory_missing);

    // R clears directory_missing.
    jnx::ItchMsg r;
    r.type = 'R';
    r.ns = 6;
    std::snprintf(r.orderbook_id, sizeof(r.orderbook_id), "9984");
    std::snprintf(r.isin, sizeof(r.isin), "JP0000000000");
    std::snprintf(r.group, sizeof(r.group), "DAY");
    r.round_lot = 100;
    r.tick_table_id = 1;
    r.price_decimals = 1;
    r.upper_limit = 99999;
    r.lower_limit = 1;
    market.apply(r);
    CHECK(!market.refdata.get("9984").directory_missing);
    CHECK_EQ(market.refdata.get("9984").isin, std::string("JP0000000000"));
}

TEST(tick_table_rows_sorted_and_replace) {
    jnx::Market market;
    jnx::ItchMsg l;
    l.type = 'L';
    l.ns = 1;
    l.tick_table_id = 3;
    l.tick_size = 5;
    l.price_start = 10000;
    market.apply(l);
    l.tick_size = 1;
    l.price_start = 0;
    market.apply(l);
    l.tick_size = 10; // duplicate start replaces
    l.price_start = 10000;
    market.apply(l);
    const jnx::TickTable& tt = market.refdata.tick_tables().at(3);
    CHECK_EQ(tt.rows().size(), static_cast<size_t>(2));
    uint32_t ts = 0;
    CHECK(tt.tick_size(9999, ts));
    CHECK_EQ(ts, static_cast<uint32_t>(1));
    CHECK(tt.tick_size(10000, ts));
    CHECK_EQ(ts, static_cast<uint32_t>(10));
}

TEST(randomized_store_levels_invariant) {
    jnx::Market market;
    std::mt19937 rng(20260711u);
    std::vector<uint64_t> live;
    const char* oids[4] = {"8306", "7203", "1570", "1H2J"};
    uint64_t next_number = 1;
    for (int i = 0; i < 5000; ++i) {
        unsigned pick = rng() % 100;
        if (pick < 45 || live.empty()) {
            // Add.
            uint64_t num = next_number++;
            const char* oid = oids[rng() % 4];
            char side = (rng() % 2 == 0) ? 'B' : 'S';
            uint32_t qty = 1 + rng() % 1000;
            uint32_t price = 100 * (1 + rng() % 50);
            market.apply(make_add(num, oid, "DAY", side, qty, price,
                                  rng() % 5 == 0, 'Q'));
            live.push_back(num);
        } else {
            size_t idx = rng() % live.size();
            uint64_t num = live[idx];
            uint32_t rem = market.books.orders().at(num).remaining_qty;
            if (pick < 70) {
                // Execute (sometimes over-executing to exercise the clamp).
                uint32_t q = 1 + rng() % (rem + rem / 4 + 1);
                market.apply(make_exec(num, q, 100000 + i));
                if (market.books.orders().find(num) ==
                    market.books.orders().end()) {
                    live.erase(live.begin() + idx);
                }
            } else if (pick < 85) {
                market.apply(make_delete(num));
                live.erase(live.begin() + idx);
            } else {
                uint64_t newnum = next_number++;
                market.apply(make_replace(num, newnum, 1 + rng() % 1000,
                                          100 * (1 + rng() % 50)));
                live[idx] = newnum;
            }
        }
        // Sampled per-message check (cheap subset), full check at the end.
        if (i % 250 == 0) {
            CHECK(store_levels_consistent(market));
        }
    }
    // A few orphans on top (unknown numbers) — never fatal.
    market.apply(make_exec(999999999, 1, 1));
    market.apply(make_delete(999999998));
    market.apply(make_replace(999999997, 999999996, 10, 100));
    CHECK(store_levels_consistent(market));
    CHECK_EQ(market.books.orders().size(), live.size());
    CHECK_EQ(market.books.orphan_executes, static_cast<uint64_t>(1));
    CHECK_EQ(market.books.orphan_deletes, static_cast<uint64_t>(1));
    CHECK_EQ(market.books.orphan_replaces, static_cast<uint64_t>(1));
}
