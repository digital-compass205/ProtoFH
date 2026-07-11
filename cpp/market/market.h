// market.h — the market facade: C++ port of jnxfeed/book/market.py.
//
// Market::apply(msg) is the single entry point routing every decoded ITCH
// message to refdata / order store / tape, with the prototype's exact
// routing rules:
//   T            -> session clock only
//   G            -> end_of_snapshot_seq only
//   A with order_number==0 (ref-price) -> refdata only
//   A/F/D/U      -> order store
//   E            -> order store; a matched execution is recorded on the
//                   tape at make_timestamp(seconds, msg.ns)
//   R/L/H/Y/S    -> refdata
//   anything else -> unknown_count (never fatal)
//
// ApplyResult additionally reports what changed — exactly what the record
// builder (F3/F5) needs to assemble one UPDATE per message.
#ifndef JNX_MARKET_MARKET_H
#define JNX_MARKET_MARKET_H

#include <cstdint>
#include <map>
#include <string>

#include "itch/itch.h"
#include "market/orders.h"
#include "market/refdata.h"
#include "market/tape.h"

namespace jnx {

// Which state sections a message changed (bitmask).
enum Section {
    SEC_STATIC = 1, // T1: R/L
    SEC_STATE = 2,  // T2: H/Y/S/ref-price A
    SEC_BOOK = 4,   // T3/T4: A/F/E/D/U
    SEC_TRADE = 8   // T5: matched E
};

struct ApplyResult {
    bool applied;    // type recognized and consumed
    char trigger;    // the ITCH type char ('\0' if unknown)
    uint8_t sections; // Section bitmask (0 for T/G/orphans/unknown)

    // Affected book, when the message resolves to one ("" otherwise —
    // T/G/S(system-wide)/orphan E/D/U).
    std::string ticker;
    std::string group;

    // Order-level delta for A/F/E/D/U that touched the store.
    bool has_delta;
    char delta_op; // A/F/E/D/U
    uint64_t order_number;      // the affected order (U: the NEW number)
    uint64_t orig_order_number; // U only, else 0
    char side;
    uint32_t price; // E: passive stored price; A/F/U: message price
    uint32_t qty;   // E: clamped executed qty; A/F/U: message qty
    char order_type; // 'Q' for F DLP orders, else ' '

    // Trade info for a matched E.
    bool has_trade;
    Execution trade;

    ApplyResult()
        : applied(false),
          trigger('\0'),
          sections(0),
          has_delta(false),
          delta_op('\0'),
          order_number(0),
          orig_order_number(0),
          side('\0'),
          price(0),
          qty(0),
          order_type(' '),
          has_trade(false) {}
};

class Market {
public:
    Market() : seconds(0), end_of_snapshot_seq(0), has_end_of_snapshot(false),
               unknown_count(0) {}

    ApplyResult apply(const ItchMsg& msg);

    // --- recovery-only injection (F5 restart path; never used while live) --
    // Rebuild state from a DB recovery dump. No counters, no publishing.
    void restore_tick(uint32_t table_id, uint32_t price_start,
                      uint32_t tick_size);
    void restore_order(uint64_t order_number, const std::string& ticker,
                       const std::string& group, char side, uint32_t price,
                       uint32_t qty, char order_type);
    // reference_price: -1 = never seen (mirrors Instrument's sentinel).
    void restore_instrument(const std::string& ticker,
                            const std::string& group, const std::string& isin,
                            int64_t round_lot, int64_t tick_table_id,
                            int64_t price_decimals, int64_t upper_limit,
                            int64_t lower_limit, bool directory_seen,
                            char trading_state, char short_sell_state,
                            int64_t reference_price);
    void restore_trades(const std::string& ticker, uint64_t trade_count,
                        uint64_t volume, uint64_t notional,
                        int64_t last_price, int64_t last_qty,
                        uint64_t last_match_number);

    RefData refdata;
    OrderBookStore books;
    TradeTape tape;

    // Last T value: seconds past midnight of the session start day.
    uint64_t seconds;
    // Sequence carried by a G End of Snapshot (GLIMPSE only).
    uint64_t end_of_snapshot_seq;
    bool has_end_of_snapshot;

    // Applied-message counters by ITCH type char (sorted by char).
    std::map<char, uint64_t> message_counts;
    uint64_t unknown_count;
};

} // namespace jnx

#endif // JNX_MARKET_MARKET_H
