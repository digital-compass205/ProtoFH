// tape.cpp — see tape.h.
#include "market/tape.h"

namespace jnx {

void TradeTape::record(const Execution& execution, uint64_t timestamp_ns,
                       int64_t base_price) {
    (void)timestamp_ns; // ring entries (which carry it) arrive in a later phase
    ++trade_count;
    total_volume += execution.qty;

    BookStats& s = stats_[execution.orderbook_id];
    ++s.trade_count;
    s.volume += execution.qty;
    s.notional +=
        static_cast<uint64_t>(execution.price) * static_cast<uint64_t>(execution.qty);

    // Short-sell uptick-rule zero/plus/minus tick test (tape.h comment on
    // BookStats::uptick): compare against the effective last traded price
    // -- the real last_price once this book has traded today, else the
    // assumed base price -- before overwriting it below.
    int64_t new_price = static_cast<int64_t>(execution.price);
    int64_t effective_ltp = s.has_last ? s.last_price : base_price;
    if (effective_ltp >= 0) {
        if (new_price > effective_ltp) {
            s.uptick = true;
        } else if (new_price < effective_ltp) {
            s.uptick = false;
        }
        // new_price == effective_ltp (zero tick): s.uptick unchanged.
    }
    // effective_ltp < 0 (no trade yet today AND no base price known): the
    // classification cannot be determined; leave s.uptick at its current
    // (default false) value -- compute_ssp separately reports SSP as
    // indeterminate (NO_PRICE) whenever last_price/base_price are both
    // unavailable, regardless of this flag.

    s.last_price = new_price;
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
    // Recovery does not carry the uptick classification (tape.h comment on
    // BookStats::uptick): reset to the conservative default and let the
    // next genuine price move re-establish real tracking.
    s.uptick = false;
}

} // namespace jnx
