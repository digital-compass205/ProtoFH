// tape.h — trade tape: per-book cumulative stats. C++ port of
// jnxfeed/book/tape.py (per-book trade_count / volume / notional /
// last price+qty, plus feed totals). The prototype's rolling entry ring
// is not needed by the F2 dump; last_match_number is retained here (the
// prototype dumper tracks it from Market.apply's returned Executions —
// identical values by construction).
#ifndef JNX_MARKET_TAPE_H
#define JNX_MARKET_TAPE_H

#include <cstdint>
#include <map>
#include <string>

#include "market/orders.h"

namespace jnx {

struct BookStats {
    uint64_t trade_count;
    uint64_t volume;
    uint64_t notional; // sum(price * qty), raw price units
    int64_t last_price; // -1 = no trade yet
    int64_t last_qty;   // -1 = no trade yet
    uint64_t last_match_number;
    bool has_last;

    BookStats()
        : trade_count(0),
          volume(0),
          notional(0),
          last_price(-1),
          last_qty(-1),
          last_match_number(0),
          has_last(false) {}
};

class TradeTape {
public:
    TradeTape() : trade_count(0), total_volume(0) {}

    // Records one Execution at `timestamp_ns` (ns past midnight of the
    // session start day; kept for parity with the prototype signature —
    // the F2 state dump does not use it).
    void record(const Execution& execution, uint64_t timestamp_ns);

    const std::map<std::string, BookStats>& stats() const { return stats_; }

    uint64_t trade_count;  // total, feed-wide
    uint64_t total_volume; // total qty, feed-wide

private:
    std::map<std::string, BookStats> stats_; // orderbook_id -> stats
};

// Combine the T clock (seconds past midnight) with a message ns field.
inline uint64_t make_timestamp(uint64_t seconds, uint32_t ns) {
    return seconds * 1000000000ULL + static_cast<uint64_t>(ns);
}

} // namespace jnx

#endif // JNX_MARKET_TAPE_H
