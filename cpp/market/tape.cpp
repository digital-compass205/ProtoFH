// tape.cpp — see tape.h.
#include "market/tape.h"

namespace jnx {

void TradeTape::record(const Execution& execution, uint64_t timestamp_ns) {
    (void)timestamp_ns; // ring entries (which carry it) arrive in a later phase
    ++trade_count;
    total_volume += execution.qty;

    BookStats& s = stats_[execution.orderbook_id];
    ++s.trade_count;
    s.volume += execution.qty;
    s.notional +=
        static_cast<uint64_t>(execution.price) * static_cast<uint64_t>(execution.qty);
    s.last_price = static_cast<int64_t>(execution.price);
    s.last_qty = static_cast<int64_t>(execution.qty);
    s.last_match_number = execution.match_number;
    s.has_last = true;
}

void TradeTape::restore_stats(const std::string& orderbook_id,
                              uint64_t trades, uint64_t volume,
                              uint64_t notional, int64_t last_price,
                              int64_t last_qty, uint64_t last_match_number) {
    BookStats& s = stats_[orderbook_id];
    trade_count += trades - s.trade_count;
    total_volume += volume - s.volume;
    s.trade_count = trades;
    s.volume = volume;
    s.notional = notional;
    s.last_price = last_price;
    s.last_qty = last_qty;
    s.last_match_number = last_match_number;
    s.has_last = last_price >= 0;
}

} // namespace jnx
