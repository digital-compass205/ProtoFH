// test_tables.cpp — jnxdb Tables semantics (cpp/db/tables.{h,cpp}):
// delta ops, dup guard, tape ring, dump/re-apply round trip, reset.
#include "db/tables.h"

#include <cstring>
#include <string>
#include <vector>

#include "common/minitest.h"
#include "wire/record.h"

using namespace jnx;

namespace {

// Minimal live UPDATE builder: envelope + delta only; sections default.
UpdateRecord upd(uint64_t epoch, uint64_t seq, const char* ticker,
                 char trigger, char op, uint64_t order_no, uint32_t price,
                 uint32_t qty) {
    UpdateRecord u;
    u.epoch = epoch;
    u.pub_seq = seq;
    std::strcpy(u.session, "TESTSESS");
    u.exch_seq = seq;
    u.exch_ns = seq * 1000;
    u.trigger = trigger;
    std::strncpy(u.ticker, ticker, sizeof(u.ticker) - 1);
    u.ticker[sizeof(u.ticker) - 1] = '\0';
    std::strcpy(u.group, "DAY");
    u.delta_op = op;
    u.delta_order_number = order_no;
    u.delta_side = (op == '#') ? '\0' : 'B';
    u.delta_price = price;
    u.delta_qty = qty;
    u.delta_order_type = (op == '#') ? '\0' : ' ';
    return u;
}

// Collects dump_state output into one byte vector.
std::vector<unsigned char> dump_bytes(const Tables& t) {
    std::vector<unsigned char> out;
    t.dump_state([&out](const unsigned char* d, size_t n) {
        out.insert(out.end(), d, d + n);
    });
    return out;
}

} // namespace

TEST(delta_op_a_inserts_order) {
    Tables t;
    CHECK(t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false));
    CHECK_EQ(t.orders().size(), static_cast<size_t>(1));
    const OrderRecord& o = t.orders().begin()->second;
    CHECK_EQ(o.order_number, 42u);
    CHECK_EQ(std::string(o.ticker), std::string("8306"));
    CHECK_EQ(std::string(o.group), std::string("DAY"));
    CHECK_EQ(o.side, 'B');
    CHECK_EQ(o.price, 15000u);
    CHECK_EQ(o.qty_remaining, 100u);
    CHECK_EQ(o.order_type, ' ');
}

TEST(delta_op_a_carries_order_type) {
    Tables t;
    UpdateRecord u = upd(1, 10, "8306", 'F', 'A', 43, 15000, 100);
    u.delta_order_type = 'Q';  // DLP
    t.apply_update(u, false);
    CHECK_EQ(t.orders().begin()->second.order_type, 'Q');
}

TEST(delta_op_e_decrements_and_erases_at_zero) {
    Tables t;
    t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false);
    // partial fill: qty_remaining now 60
    t.apply_update(upd(1, 11, "8306", 'E', 'E', 42, 15000, 60), false);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(1));
    CHECK_EQ(t.orders().begin()->second.qty_remaining, 60u);
    // full fill: erased
    t.apply_update(upd(1, 12, "8306", 'E', 'E', 42, 15000, 0), false);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(0));
}

TEST(delta_op_e_unknown_order_ignored) {
    Tables t;
    t.apply_update(upd(1, 10, "8306", 'E', 'E', 4242, 15000, 60), false);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(0));
    // still applied to the book row (wholesale sections)
    CHECK_EQ(t.books().size(), static_cast<size_t>(1));
}

TEST(delta_op_d_erases) {
    Tables t;
    t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false);
    t.apply_update(upd(1, 11, "8306", 'D', 'D', 42, 0, 0), false);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(0));
}

TEST(delta_op_u_replaces) {
    Tables t;
    t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false);
    UpdateRecord u = upd(1, 11, "8306", 'U', 'U', 4200, 15500, 200);
    u.delta_orig_order_number = 42;
    t.apply_update(u, false);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(1));
    const OrderRecord& o = t.orders().begin()->second;
    CHECK_EQ(o.order_number, 4200u);
    CHECK_EQ(o.price, 15500u);
    CHECK_EQ(o.qty_remaining, 200u);
}

TEST(delta_op_hash_no_mutation) {
    Tables t;
    t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false);
    t.apply_update(upd(1, 11, "8306", 'S', '#', 0, 0, 0), false);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(1));
}

TEST(dup_guard_same_epoch_old_seq_dropped) {
    Tables t;
    CHECK(t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false));
    // same epoch, same seq -> dropped
    CHECK(!t.apply_update(upd(1, 10, "8306", 'A', 'A', 43, 15000, 100), false));
    // same epoch, older seq -> dropped
    CHECK(!t.apply_update(upd(1, 9, "8306", 'A', 'A', 44, 15000, 100), false));
    CHECK_EQ(t.meta().dups_dropped, 2u);
    CHECK_EQ(t.meta().updates_applied, 1u);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(1));  // only 42
    // newer seq accepted
    CHECK(t.apply_update(upd(1, 11, "8306", 'A', 'A', 45, 15000, 100), false));
    CHECK_EQ(t.meta().last_exch_seq, 11u);
}

TEST(dup_guard_new_epoch_accepted) {
    Tables t;
    t.apply_update(upd(1, 100, "8306", 'A', 'A', 42, 15000, 100), false);
    // epoch bump: an old seq is fine (fresh FH incarnation restarted feed)
    CHECK(t.apply_update(upd(2, 5, "8306", 'A', 'A', 43, 15000, 100), false));
    CHECK_EQ(t.meta().epoch, 2u);
    CHECK_EQ(t.meta().last_exch_seq, 5u);
    CHECK_EQ(t.meta().dups_dropped, 0u);
}

TEST(sync_mode_bypasses_dup_guard_and_meta) {
    Tables t;
    t.apply_update(upd(1, 100, "8306", 'A', 'A', 42, 15000, 100), false);
    // sync rows carry the same exch_seq repeatedly — must all apply
    CHECK(t.apply_update(upd(1, 50, "7203", '#', '#', 0, 0, 0), true));
    CHECK(t.apply_update(upd(1, 50, "9999", '#', '#', 0, 0, 0), true));
    CHECK_EQ(t.books().size(), static_cast<size_t>(3));
    // meta untouched by sync rows
    CHECK_EQ(t.meta().last_exch_seq, 100u);
    CHECK_EQ(t.meta().dups_dropped, 0u);
}

TEST(tape_ring_caps_at_50_newest_kept) {
    Tables t;
    for (uint64_t i = 1; i <= 60; ++i) {
        UpdateRecord u = upd(1, i, "8306", 'E', '#', 0, 0, 0);
        u.last_price = static_cast<uint32_t>(1000 + i);
        u.last_qty = 10;
        u.last_match_number = i;
        u.last_trade_ns = i * 1000;
        t.apply_update(u, false);
    }
    const BookRow& row = t.books().begin()->second;
    CHECK_EQ(row.tape.size(), TAPE_CAP);
    CHECK_EQ(row.tape.front().match_number, 11u);  // oldest kept = 11
    CHECK_EQ(row.tape.back().match_number, 60u);   // newest
    CHECK_EQ(row.tape.back().price, 1060u);
}

TEST(tape_only_on_trigger_e) {
    Tables t;
    t.apply_update(upd(1, 1, "8306", 'A', 'A', 42, 15000, 100), false);
    t.apply_update(upd(1, 2, "8306", 'H', '#', 0, 0, 0), false);
    CHECK_EQ(t.books().begin()->second.tape.size(), static_cast<size_t>(0));
}

TEST(apply_order_and_tick) {
    Tables t;
    OrderRecord o;
    o.order_number = 7;
    std::strcpy(o.ticker, "8306");
    std::strcpy(o.group, "DAY");
    o.side = 'S';
    o.price = 200;
    o.qty_remaining = 5;
    o.order_type = ' ';
    t.apply_order(o);
    TickRecord k;
    k.table_id = 1;
    k.price_start = 0;
    k.tick_size = 1;
    t.apply_tick(k);
    k.price_start = 30000;
    k.tick_size = 5;
    t.apply_tick(k);
    CHECK_EQ(t.orders().size(), static_cast<size_t>(1));
    CHECK_EQ(t.tick_row_count(), static_cast<size_t>(2));
    CHECK_EQ(t.meta().orders_applied, 1u);
    CHECK_EQ(t.meta().ticks_applied, 2u);
}

TEST(dump_reapply_round_trip_identical) {
    // Populate a Tables with a mix of everything.
    Tables a;
    TickRecord k;
    k.table_id = 1;
    k.price_start = 0;
    k.tick_size = 1;
    a.apply_tick(k);
    k.price_start = 30000;
    k.tick_size = 5;
    a.apply_tick(k);
    for (uint64_t i = 1; i <= 5; ++i) {
        UpdateRecord u = upd(3, 100 + i, i % 2 ? "8306" : "7203", 'A', 'A',
                             1000 + i, 15000 + static_cast<uint32_t>(i), 100);
        u.level_count_bid = 1;
        u.bids[0].price = 15000;
        u.bids[0].qty = 100;
        u.bids[0].order_count = 1;
        u.total_bid_qty = 100;
        u.total_bid_orders = 1;
        u.flags = FLAG_DIRECTORY_SEEN;
        std::strcpy(u.isin, "JP0000000001");
        a.apply_update(u, false);
    }
    // one trade so summary fields are non-zero
    {
        UpdateRecord u = upd(3, 106, "8306", 'E', 'E', 1001, 15001, 0);
        u.last_price = 15001;
        u.last_qty = 100;
        u.last_match_number = 9001;
        u.last_trade_ns = 424242;
        u.cum_qty = 100;
        u.cum_turnover = 1500100;
        u.trade_count = 1;
        a.apply_update(u, false);
    }

    std::vector<unsigned char> dump_a = dump_bytes(a);
    CHECK(dump_a.size() > 0);

    // Re-apply the dump into a fresh Tables via the framer + decoders,
    // exactly as ingest would.
    Tables b;
    RecordFramer framer;
    framer.feed(&dump_a[0], dump_a.size());
    RawRecord rec;
    bool in_sync = false;
    int count = 0;
    while (framer.next(rec)) {
        ++count;
        std::vector<unsigned char> whole(RECORD_HEADER_SIZE +
                                         rec.body.size());
        // header reconstruction (same trick as ingest)
        whole[0] = 0x4A;
        whole[1] = 0x58;
        whole[2] = RECORD_VERSION;
        whole[3] = static_cast<unsigned char>(rec.kind);
        whole[4] = static_cast<unsigned char>(rec.body.size() >> 8);
        whole[5] = static_cast<unsigned char>(rec.body.size() & 0xFF);
        whole[6] = 0;
        whole[7] = 0;
        if (!rec.body.empty()) {
            std::memcpy(&whole[8], &rec.body[0], rec.body.size());
        }
        const char* err = 0;
        if (rec.kind == KIND_SYNC_BEGIN) {
            in_sync = true;
        } else if (rec.kind == KIND_SYNC_END) {
            SyncEndRecord se;
            CHECK(decode_sync_end(&whole[0], whole.size(), se, &err));
            b.adopt_meta(se);
            in_sync = false;
        } else if (rec.kind == KIND_TICK) {
            TickRecord tk;
            CHECK(decode_tick(&whole[0], whole.size(), tk, &err));
            b.apply_tick(tk);
        } else if (rec.kind == KIND_ORDER) {
            OrderRecord od;
            CHECK(decode_order(&whole[0], whole.size(), od, &err));
            b.apply_order(od);
        } else if (rec.kind == KIND_UPDATE) {
            UpdateRecord ud;
            CHECK(decode_update(&whole[0], whole.size(), ud, &err));
            CHECK_EQ(ud.trigger, '#');
            CHECK_EQ(ud.delta_op, '#');
            b.apply_update(ud, in_sync);
        }
    }
    CHECK(!framer.corrupt());
    // SYNC_BEGIN + 2 ticks + 5 orders (1001 erased by fill -> 4... wait:
    // orders 1001..1005 inserted, 1001 erased at fill) + 3 book rows? Let
    // the equality check below be the source of truth; just require the
    // bracket was complete.
    CHECK(!in_sync);
    CHECK(count >= 3);

    // The dump of the re-applied instance must be byte-identical.
    std::vector<unsigned char> dump_b = dump_bytes(b);
    CHECK_EQ(dump_a.size(), dump_b.size());
    CHECK(dump_a == dump_b);
    CHECK_EQ(b.meta().last_exch_seq, a.meta().last_exch_seq);
    CHECK_EQ(b.meta().epoch, a.meta().epoch);
    CHECK_EQ(std::string(b.meta().session.c_str()),
             std::string(a.meta().session.c_str()));
}

TEST(reset_wipes_everything) {
    Tables t;
    t.apply_update(upd(1, 10, "8306", 'A', 'A', 42, 15000, 100), false);
    TickRecord k;
    k.table_id = 1;
    k.price_start = 0;
    k.tick_size = 1;
    t.apply_tick(k);
    t.reset();
    CHECK_EQ(t.books().size(), static_cast<size_t>(0));
    CHECK_EQ(t.orders().size(), static_cast<size_t>(0));
    CHECK_EQ(t.tick_row_count(), static_cast<size_t>(0));
    CHECK_EQ(t.meta().last_exch_seq, 0u);
    CHECK_EQ(t.meta().epoch, 0u);
    CHECK_EQ(t.meta().updates_applied, 0u);
    CHECK_EQ(std::string(t.meta().session.c_str()), std::string(""));
}

TEST(wholesale_upsert_reflects_latest_record) {
    Tables t;
    UpdateRecord u = upd(1, 10, "8306", 'A', 'A', 42, 15000, 100);
    u.trading_state = 'T';
    u.reference_price = 15000;
    t.apply_update(u, false);
    UpdateRecord v = upd(1, 11, "8306", 'H', '#', 0, 0, 0);
    v.trading_state = 'V';
    v.reference_price = 15100;
    t.apply_update(v, false);
    const BookRow& row = t.books().begin()->second;
    CHECK_EQ(row.trading_state, 'V');
    CHECK_EQ(row.reference_price, 15100u);
    CHECK_EQ(row.last_exch_seq, 11u);
}
