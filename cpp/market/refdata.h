// refdata.h — reference data store: C++ port of jnxfeed/book/refdata.py.
//
// Consumes R / L / H / Y / S and reference-price A (order_number == 0).
// Absence semantics (JNX_PLAN.md §3.3(4)): default trading_state 'V'
// (suspended), default short_sell_state '0' (unrestricted). Auto-create
// (§3.3(5)): first reference to an unknown orderbook id creates a record
// flagged directory_missing until an R describes it.
//
// "None" representation (prototype uses Python None): numeric fields use
// int64_t -1; string fields use "".
#ifndef JNX_MARKET_REFDATA_H
#define JNX_MARKET_REFDATA_H

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "itch/itch.h"

namespace jnx {

struct Instrument {
    std::string orderbook_id;
    std::string isin;  // "" = unknown
    std::string group; // "" = unknown
    int64_t round_lot;      // -1 = unknown
    int64_t tick_table_id;  // -1 = unknown
    int64_t price_decimals; // -1 = unknown
    int64_t upper_limit;    // -1 = unknown
    int64_t lower_limit;    // -1 = unknown
    char trading_state;     // default 'V' (absence = suspended)
    char short_sell_state;  // default '0' (absence = unrestricted)
    int64_t reference_price; // -1 = no ref-price A seen (NO_PRICE is valid)
    bool directory_missing;  // true until an R arrives

    Instrument()
        : round_lot(-1),
          tick_table_id(-1),
          price_decimals(-1),
          upper_limit(-1),
          lower_limit(-1),
          trading_state('V'),
          short_sell_state('0'),
          reference_price(-1),
          directory_missing(true) {}
};

// One tick-size table from L rows: tick_size applies from price_start
// (inclusive) to the next row's start (exclusive). Duplicate price_start
// replaces the row.
class TickTable {
public:
    void add(uint32_t price_start, uint32_t tick_size) {
        rows_[price_start] = tick_size;
    }

    // Tick size in effect at price; false if below every row / empty.
    bool tick_size(uint32_t price, uint32_t& out) const {
        std::map<uint32_t, uint32_t>::const_iterator it =
            rows_.upper_bound(price);
        if (it == rows_.begin()) {
            return false;
        }
        --it;
        out = it->second;
        return true;
    }

    const std::map<uint32_t, uint32_t>& rows() const { return rows_; }

private:
    std::map<uint32_t, uint32_t> rows_; // price_start -> tick_size
};

struct SystemEvent {
    uint32_t ns;
    std::string group; // "" = system-wide
    char event;
};

class RefData {
public:
    // Instrument for orderbook_id, auto-creating a directory_missing
    // record on first reference.
    Instrument& get(const std::string& orderbook_id);

    TickTable& tick_table(uint32_t tick_table_id) {
        return tick_tables_[tick_table_id];
    }

    // Applies one decoded message; returns true if consumed (R/L/H/Y/S or
    // ref-price A), false if it is not a refdata concern.
    bool apply(const ItchMsg& msg);

    const std::map<std::string, Instrument>& instruments() const {
        return instruments_;
    }
    const std::map<uint32_t, TickTable>& tick_tables() const {
        return tick_tables_;
    }
    const std::vector<SystemEvent>& system_events() const {
        return system_events_;
    }

private:
    std::map<std::string, Instrument> instruments_;
    std::map<uint32_t, TickTable> tick_tables_;
    std::vector<SystemEvent> system_events_;
};

} // namespace jnx

#endif // JNX_MARKET_REFDATA_H
