// tables.h — jnxdb in-memory state: the 5 tables of JNX_PLAN2.md §2 plus
// meta and tick_tables, fed by UPDATE/ORDER/TICK records (docs/wire_spec.md).
//
// Storage decision: T1 static / T2 state / T4 book_agg / T5 trades are all
// keyed by (ticker, group) and arrive WHOLESALE in every UPDATE record, so
// they are stored as one merged row per key (`BookRow`) and sliced per
// table at query time. T3 orders is its own map keyed by order number,
// mutated by the UPDATE delta section. All maps are std::map so iteration
// (dumps, CSV tables) is deterministic without extra sorting.
//
// Single-threaded by design — no locking anywhere (jnxdb runs one poll loop).
#ifndef JNX_DB_TABLES_H
#define JNX_DB_TABLES_H

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <string>
#include <utility>

#include "wire/record.h"

namespace jnx {

// One tape entry (T5 ring, DB-side only — never dumped/recovered).
struct TapeEntry {
    uint64_t ns;
    uint32_t price;
    uint32_t qty;
    uint64_t match_number;

    TapeEntry() : ns(0), price(0), qty(0), match_number(0) {}
};

const size_t TAPE_CAP = 50;

// Merged T1+T2+T4+T5 row for one (ticker, group). Field names match the
// UPDATE record sections (docs/wire_spec.md) one-to-one.
struct BookRow {
    // T1 static
    std::string isin;
    uint32_t round_lot;
    uint32_t tick_table_id;
    uint8_t price_decimals;
    uint32_t upper_limit;
    uint32_t lower_limit;
    uint8_t flags;  // FLAG_DIRECTORY_SEEN | FLAG_ORDER_COLLISION_SEEN

    // T2 state
    char trading_state;
    char short_sell_restriction;
    uint32_t reference_price;
    char last_system_event;
    uint64_t last_exch_seq;   // from the UPDATE envelope (spec decision)
    uint64_t last_update_ns;  // from the UPDATE envelope

    // T4 book_agg
    uint8_t level_count_bid;
    uint8_t level_count_ask;
    BookLevel bids[BOOK_DEPTH];
    BookLevel asks[BOOK_DEPTH];
    uint64_t total_bid_qty;
    uint64_t total_ask_qty;
    uint32_t total_bid_orders;
    uint32_t total_ask_orders;

    // T5 trades summary
    uint32_t last_price;
    uint32_t last_qty;
    uint64_t last_match_number;
    uint64_t last_trade_ns;
    uint64_t cum_qty;
    uint64_t cum_turnover;
    uint32_t trade_count;

    // T5 tape ring (newest at the back; capped at TAPE_CAP)
    std::deque<TapeEntry> tape;

    BookRow();
};

// Internal meta + counters (exposed via the STATS query).
struct Meta {
    std::string session;
    uint64_t last_exch_seq;
    uint64_t epoch;

    uint64_t updates_applied;
    uint64_t dups_dropped;
    uint64_t orders_applied;   // ORDER records (sync/recovery rows)
    uint64_t ticks_applied;    // TICK records
    uint64_t syncs_completed;  // SYNC_BEGIN..SYNC_END brackets adopted
    uint64_t syncs_discarded;  // partial syncs wiped

    Meta();
};

class Tables {
public:
    typedef std::pair<std::string, std::string> Key;  // (ticker, group)
    typedef std::map<Key, BookRow> BookMap;
    typedef std::map<uint64_t, OrderRecord> OrderMap;
    // table_id -> (price_start -> tick_size), both sorted.
    typedef std::map<uint32_t, std::map<uint32_t, uint32_t> > TickMap;

    Tables() {}

    // Applies one UPDATE record. Live path (in_sync == false): the dup
    // guard drops records with the same epoch and exch_seq <=
    // meta.last_exch_seq (returns false, counts, WARN rate-limited);
    // otherwise upserts the merged row, mutates T3 from the delta,
    // appends the tape on trigger 'E', and adopts envelope meta.
    // Sync path (in_sync == true): no dup guard, no meta adoption (the
    // bracket's SYNC_END carries the meta to adopt).
    bool apply_update(const UpdateRecord& rec, bool in_sync);

    // Sync/recovery rows.
    void apply_order(const OrderRecord& rec);
    void apply_tick(const TickRecord& rec);

    // Adopt meta from a SYNC_END record (end of a successful bracket).
    void adopt_meta(const SyncEndRecord& rec);

    // Wipe everything (RESET record / partial-sync discard).
    void reset();

    // Streams the full state as encoded records through `sink`:
    // SYNC_BEGIN, every TICK row, every ORDER row, one UPDATE per
    // (ticker, group) with trigger '#'/delta '#', then SYNC_END with
    // meta. Deterministic: all maps iterate sorted.
    void dump_state(const std::function<void(const unsigned char*, size_t)>&
                        sink) const;

    // Read access for the query server / tests.
    const BookMap& books() const { return books_; }
    const OrderMap& orders() const { return orders_; }
    const TickMap& ticks() const { return ticks_; }
    const Meta& meta() const { return meta_; }

    // Counters the ingest layer maintains (kept here so STATS has one home).
    void count_sync_completed() { ++meta_.syncs_completed; }
    void count_sync_discarded() { ++meta_.syncs_discarded; }

    // Number of tick-table rows across all tables.
    size_t tick_row_count() const;

private:
    // Builds the sync-dump UPDATE for one row (trigger '#', delta '#').
    UpdateRecord make_dump_update(const Key& key, const BookRow& row) const;

    void apply_delta(const UpdateRecord& rec);

    BookMap books_;
    OrderMap orders_;
    TickMap ticks_;
    Meta meta_;
};

} // namespace jnx

#endif // JNX_DB_TABLES_H
