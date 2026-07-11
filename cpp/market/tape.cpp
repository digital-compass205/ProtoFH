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

} // namespace jnx
