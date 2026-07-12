// test_shortsell.cpp — Short Sell Price (SSP) computation: compute_ssp()
// (cpp/market/refdata.{h,cpp}) and the zero/plus/minus tick classification
// maintained in TradeTape::record() (cpp/market/tape.{h,cpp}), driven both
// directly and end-to-end through Market::apply(). Scenarios mirror the
// worked examples in the approved plan (JNX_Short_Selling_Rules_2.00.pdf).
#include "market/market.h"
#include "market/refdata.h"
#include "market/tape.h"

#include "common/minitest.h"

using namespace jnx;

namespace {

jnx::ItchMsg make_add(uint64_t number, const char* oid, const char* group,
                      char side, uint32_t qty, uint32_t price) {
    jnx::ItchMsg m;
    m.type = 'A';
    m.ns = 1;
    m.order_number = number;
    m.side = side;
    m.qty = qty;
    std::snprintf(m.orderbook_id, sizeof(m.orderbook_id), "%s", oid);
    std::snprintf(m.group, sizeof(m.group), "%s", group);
    m.price = price;
    m.order_type = ' ';
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

jnx::ItchMsg make_short_sell(const char* oid, const char* group, char state) {
    jnx::ItchMsg m;
    m.type = 'Y';
    m.ns = 1;
    std::snprintf(m.orderbook_id, sizeof(m.orderbook_id), "%s", oid);
    std::snprintf(m.group, sizeof(m.group), "%s", group);
    m.state = state;
    return m;
}

// One resting order on each side plus a match, so an 'E' against
// `order_number` reports `price` as the passive stored price. Every call
// uses a fresh, unique order_number/match_number pair.
void trade_at(Market& m, const char* oid, const char* group,
             uint64_t order_number, uint64_t match_number, uint32_t price) {
    m.apply(make_add(order_number, oid, group, 'S', 100, price));
    m.apply(make_exec(order_number, 100, match_number));
}

} // namespace

// --- compute_ssp: pure-function coverage -----------------------------------

TEST(ssp_unrestricted_is_always_zero) {
    TickTable ticks;
    ticks.add(0, 1);
    CHECK_EQ(compute_ssp('0', 1000, 990, true, true, &ticks), 0u);
    CHECK_EQ(compute_ssp('\0', 1000, 990, true, false, &ticks), 0u);
    CHECK_EQ(compute_ssp('?', 1000, 990, true, false, NULL), 0u);
}

TEST(ssp_restricted_from_open_no_trade_yet) {
    // Plan example 2: restricted before any trade -- LTP assumed = base
    // price, uptick defaults false (never an uptick) -> SSP = BP + 1 tick.
    TickTable ticks;
    ticks.add(0, 1);
    CHECK_EQ(compute_ssp('1', 1000, -1, false, false, &ticks), 1001u);
}

TEST(ssp_restricted_no_base_price_no_trade_is_indeterminate) {
    // No base price known and no trade yet: cannot compute -> NO_PRICE.
    TickTable ticks;
    ticks.add(0, 1);
    CHECK_EQ(compute_ssp('1', -1, -1, false, false, &ticks), NO_PRICE);
}

TEST(ssp_restricted_uptick_is_ltp_itself) {
    TickTable ticks;
    ticks.add(0, 1);
    CHECK_EQ(compute_ssp('1', 1000, 896, true, true, &ticks), 896u);
}

TEST(ssp_restricted_not_uptick_is_ltp_plus_tick) {
    // Plan example 3: circuit breaker trips on a down move (minus tick).
    TickTable ticks;
    ticks.add(0, 1);
    CHECK_EQ(compute_ssp('1', 1000, 895, true, false, &ticks), 896u);
}

TEST(ssp_restricted_unknown_tick_size_is_indeterminate) {
    TickTable empty;  // no rows at all
    CHECK_EQ(compute_ssp('1', 1000, 895, true, false, &empty), NO_PRICE);
    CHECK_EQ(compute_ssp('1', 1000, 895, true, false, NULL), NO_PRICE);
}

TEST(ssp_tick_size_boundary) {
    // Plan example 7: tick size changes at the 3000 price band.
    TickTable ticks;
    ticks.add(0, 1);
    ticks.add(3000, 5);
    CHECK_EQ(compute_ssp('1', 3000, 2999, true, false, &ticks), 3000u);
    CHECK_EQ(compute_ssp('1', 3000, 3000, true, false, &ticks), 3005u);
}

// --- TradeTape::record: zero/plus/minus tick classification ----------------

TEST(tape_first_trade_flat_to_base_price_is_not_uptick) {
    TradeTape tape;
    Execution e;
    e.orderbook_id = "8306";
    e.price = 1000;
    e.qty = 100;
    e.match_number = 1;
    tape.record(e, 0, /*base_price=*/1000);  // flat vs. assumed base
    const BookStats& s = tape.stats().at("8306");
    CHECK_EQ(s.last_price, static_cast<int64_t>(1000));
    CHECK(!s.uptick);
}

TEST(tape_first_trade_above_base_price_is_uptick) {
    TradeTape tape;
    Execution e;
    e.orderbook_id = "8306";
    e.price = 1010;
    e.qty = 100;
    e.match_number = 1;
    tape.record(e, 0, /*base_price=*/1000);
    CHECK(tape.stats().at("8306").uptick);
}

TEST(tape_repeated_price_is_zero_tick_and_does_not_flip_classification) {
    // Plan example 4: 895 (minus tick) -> 896 (plus tick, uptick=true) ->
    // 896 again (zero tick: uptick STAYS true, does not reset to false).
    TradeTape tape;
    Execution e;
    e.orderbook_id = "8306";
    e.qty = 100;

    e.price = 990;
    e.match_number = 1;
    tape.record(e, 0, 1000);  // minus tick vs base 1000
    CHECK(!tape.stats().at("8306").uptick);

    e.price = 970;
    e.match_number = 2;
    tape.record(e, 0, 1000);  // minus tick vs 990
    CHECK(!tape.stats().at("8306").uptick);

    e.price = 895;
    e.match_number = 3;
    tape.record(e, 0, 1000);  // minus tick vs 970
    CHECK(!tape.stats().at("8306").uptick);

    e.price = 896;
    e.match_number = 4;
    tape.record(e, 0, 1000);  // plus tick vs 895
    CHECK(tape.stats().at("8306").uptick);
    CHECK_EQ(tape.stats().at("8306").last_price, static_cast<int64_t>(896));

    e.price = 896;  // repeat print: zero tick
    e.match_number = 5;
    tape.record(e, 0, 1000);
    CHECK(tape.stats().at("8306").uptick);  // unchanged, still true
    CHECK_EQ(tape.stats().at("8306").last_price, static_cast<int64_t>(896));

    e.price = 900;
    e.match_number = 6;
    tape.record(e, 0, 1000);  // plus tick vs 896
    CHECK(tape.stats().at("8306").uptick);
    CHECK_EQ(tape.stats().at("8306").last_price, static_cast<int64_t>(900));
}

TEST(tape_minus_tick_after_uptick_flips_classification) {
    TradeTape tape;
    Execution e;
    e.orderbook_id = "8306";
    e.qty = 100;
    e.price = 896;
    e.match_number = 1;
    tape.record(e, 0, 1000);  // minus tick vs base -> false
    e.price = 900;
    e.match_number = 2;
    tape.record(e, 0, 1000);  // plus tick -> true
    CHECK(tape.stats().at("8306").uptick);
    e.price = 894;
    e.match_number = 3;
    tape.record(e, 0, 1000);  // minus tick vs 900 -> false
    CHECK(!tape.stats().at("8306").uptick);
    CHECK_EQ(tape.stats().at("8306").last_price, static_cast<int64_t>(894));
}

// --- end-to-end through Market::apply ---------------------------------------

TEST(market_ssp_end_to_end_restricted_from_open) {
    // Plan example 2: JNX marks the book restricted before any trade has
    // happened today (e.g. inherited from the primary exchange at the
    // start of the Nighttime Session). No BookStats row exists yet for
    // this ticker at all -- compute_ssp must fall back to the book's
    // reference (base) price, not crash or silently return 0.
    Market m;
    m.refdata.tick_table(1).add(0, 1);
    m.apply(make_add(0, "8306", "DAY", 'B', 0, 1000));  // reference price A
    m.apply(make_short_sell("8306", "DAY", '1'));        // restricted, no trade yet

    const Instrument& inst = m.refdata.instruments().at("8306");
    CHECK(m.tape.stats().find("8306") == m.tape.stats().end());
    BookStats none;  // has_last=false, last_price=-1: the "no trade yet" state
    const TickTable& ticks = m.refdata.tick_tables().at(1);
    uint32_t ssp = compute_ssp(inst.short_sell_state, inst.reference_price,
                               none.last_price, none.has_last, none.uptick,
                               &ticks);
    CHECK_EQ(ssp, 1001u);  // base price 1000, not-uptick default -> +1 tick
}

TEST(market_ssp_end_to_end_circuit_breaker_trip_and_recovery) {
    Market m;
    m.refdata.tick_table(1).add(0, 1);
    m.apply(make_add(0, "8306", "DAY", 'B', 0, 1000));  // base price 1000
    ItchMsg dir;
    dir.type = 'R';
    dir.ns = 1;
    std::snprintf(dir.orderbook_id, sizeof(dir.orderbook_id), "8306");
    std::snprintf(dir.group, sizeof(dir.group), "DAY");
    dir.tick_table_id = 1;
    m.apply(dir);

    trade_at(m, "8306", "DAY", 101, 1, 990);  // minus tick
    trade_at(m, "8306", "DAY", 102, 2, 970);  // minus tick
    trade_at(m, "8306", "DAY", 103, 3, 895);  // minus tick, CB trips here
    m.apply(make_short_sell("8306", "DAY", '1'));

    const Instrument& inst = m.refdata.instruments().at("8306");
    const BookStats& s = m.tape.stats().at("8306");
    const TickTable& ticks = m.refdata.tick_tables().at(1);
    CHECK_EQ(compute_ssp(inst.short_sell_state, inst.reference_price,
                         s.last_price, s.has_last, s.uptick, &ticks),
            896u);

    // A plus tick to 896, then a repeated 896 print (zero tick: SSP must
    // stay at 896, not drift to 897 -- the bug this feature originally had
    // before the zero-tick correction).
    trade_at(m, "8306", "DAY", 104, 4, 896);
    CHECK_EQ(compute_ssp(inst.short_sell_state, inst.reference_price,
                         s.last_price, s.has_last, s.uptick, &ticks),
            896u);
    trade_at(m, "8306", "DAY", 105, 5, 896);
    CHECK_EQ(compute_ssp(inst.short_sell_state, inst.reference_price,
                         s.last_price, s.has_last, s.uptick, &ticks),
            896u);

    // Restriction lifted: SSP reports 0 immediately regardless of tape state.
    m.apply(make_short_sell("8306", "DAY", '0'));
    CHECK_EQ(compute_ssp(inst.short_sell_state, inst.reference_price,
                         s.last_price, s.has_last, s.uptick, &ticks),
            0u);
}
