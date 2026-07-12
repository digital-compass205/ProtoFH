// refdata.cpp — see refdata.h.
#include "market/refdata.h"

namespace jnx {

uint32_t compute_ssp(char restricted, int64_t base_price, int64_t last_price,
                     bool has_last, bool uptick, const TickTable* ticks) {
    if (restricted != '1') {
        return 0;
    }
    int64_t ltp = has_last ? last_price : base_price;
    if (ltp < 0) {
        return NO_PRICE; // no trade yet today and no base price known
    }
    if (uptick) {
        return static_cast<uint32_t>(ltp);
    }
    uint32_t tick = 0;
    if (ticks == NULL || !ticks->tick_size(static_cast<uint32_t>(ltp), tick)) {
        return NO_PRICE; // tick size at LTP unknown
    }
    return static_cast<uint32_t>(ltp) + tick;
}

Instrument& RefData::get(const std::string& orderbook_id) {
    std::map<std::string, Instrument>::iterator it =
        instruments_.find(orderbook_id);
    if (it == instruments_.end()) {
        Instrument inst;
        inst.orderbook_id = orderbook_id;
        it = instruments_.insert(std::make_pair(orderbook_id, inst)).first;
    }
    return it->second;
}

bool RefData::apply(const ItchMsg& msg) {
    switch (msg.type) {
        case 'R': {
            Instrument& inst = get(msg.orderbook_id);
            inst.isin = msg.isin;
            inst.group = msg.group;
            inst.round_lot = static_cast<int64_t>(msg.round_lot);
            inst.tick_table_id = static_cast<int64_t>(msg.tick_table_id);
            inst.price_decimals = static_cast<int64_t>(msg.price_decimals);
            inst.upper_limit = static_cast<int64_t>(msg.upper_limit);
            inst.lower_limit = static_cast<int64_t>(msg.lower_limit);
            inst.directory_missing = false;
            return true;
        }
        case 'L':
            tick_table(msg.tick_table_id).add(msg.price_start, msg.tick_size);
            return true;
        case 'H': {
            Instrument& inst = get(msg.orderbook_id);
            if (inst.group.empty()) {
                inst.group = msg.group;
            }
            inst.trading_state = msg.state;
            return true;
        }
        case 'Y': {
            Instrument& inst = get(msg.orderbook_id);
            if (inst.group.empty()) {
                inst.group = msg.group;
            }
            inst.short_sell_state = msg.state;
            return true;
        }
        case 'S': {
            SystemEvent ev;
            ev.ns = msg.ns;
            ev.group = msg.group;
            ev.event = msg.event;
            system_events_.push_back(ev);
            return true;
        }
        case 'A':
            if (msg.order_number == 0) {
                // Reference-price update (§3.3(1)): NOT an order; price
                // may be NO_PRICE; side/qty meaningless.
                Instrument& inst = get(msg.orderbook_id);
                if (inst.group.empty()) {
                    inst.group = msg.group;
                }
                inst.reference_price = static_cast<int64_t>(msg.price);
                return true;
            }
            return false;
        default:
            return false;
    }
}

} // namespace jnx
