// market.cpp — see market.h. Routing mirrors jnxfeed/book/market.py; the
// extra ApplyResult detail (ticker resolution for D/U via a store peek)
// changes no state and exists only for record building.
#include "market/market.h"

namespace jnx {

namespace {

bool known_type(char t) {
    switch (t) {
        case 'T': case 'S': case 'L': case 'R': case 'H': case 'Y':
        case 'A': case 'F': case 'E': case 'D': case 'U': case 'G':
            return true;
        default:
            return false;
    }
}

} // namespace

ApplyResult Market::apply(const ItchMsg& msg) {
    ApplyResult res;
    if (!known_type(msg.type)) {
        ++unknown_count;
        return res;
    }
    ++message_counts[msg.type];
    res.applied = true;
    res.trigger = msg.type;

    Execution execution;
    switch (msg.type) {
        case 'T':
            seconds = msg.seconds;
            return res;
        case 'G':
            end_of_snapshot_seq = msg.sequence_number;
            has_end_of_snapshot = true;
            return res;
        case 'A':
        case 'F':
            if (msg.type == 'A' && msg.order_number == 0) {
                // Reference-price pseudo-order: refdata only (§3.3(1)).
                refdata.apply(msg);
                res.sections = SEC_STATE;
                res.ticker = msg.orderbook_id;
                res.group = msg.group;
                return res;
            }
            books.apply(msg, execution);
            res.sections = SEC_BOOK;
            res.ticker = msg.orderbook_id;
            res.group = msg.group;
            res.has_delta = true;
            res.delta_op = msg.type;
            res.order_number = msg.order_number;
            res.side = msg.side;
            res.price = msg.price;
            res.qty = msg.qty;
            res.order_type = (msg.type == 'F') ? msg.order_type : ' ';
            return res;
        case 'E': {
            // Peek the stored order's type before apply (the order may be
            // erased by a fill-to-zero).
            std::unordered_map<uint64_t, Order>::const_iterator eit =
                books.orders().find(msg.order_number);
            char etype = eit != books.orders().end() ? eit->second.order_type
                                                     : ' ';
            if (books.apply(msg, execution)) {
                // Read-only lookup (NOT refdata.get(), which auto-creates a
                // directory_missing Instrument on first reference): an 'E'
                // is a book-store concern, not a refdata one (market.h
                // routing table), so it must never have the side effect of
                // materializing a refdata record. Doing so would make a
                // ticker that only ever trades diverge between full-replay
                // (auto-creates locally on the first E) and GLIMPSE-sync
                // bootstrap (the snapshot never serializes a phantom
                // Instrument that was only ever auto-created, not backed
                // by a real R/H/Y/reference-A) -- breaking the "final
                // Market state identical across all paths" invariant
                // (T6.2).
                int64_t base_price = -1;
                std::map<std::string, Instrument>::const_iterator ri =
                    refdata.instruments().find(execution.orderbook_id);
                if (ri != refdata.instruments().end()) {
                    base_price = ri->second.reference_price;
                }
                tape.record(execution, make_timestamp(seconds, msg.ns),
                           base_price);
                res.sections = SEC_BOOK | SEC_TRADE;
                res.ticker = execution.orderbook_id;
                res.group = execution.group;
                res.has_delta = true;
                res.delta_op = 'E';
                res.order_number = msg.order_number;
                res.side = execution.side;
                res.price = execution.price; // passive stored price
                res.qty = execution.qty;     // clamped executed qty
                res.order_type = etype;
                res.has_trade = true;
                res.trade = execution;
            }
            return res; // orphan E: applied, but nothing changed
        }
        case 'D': {
            // Peek before applying so the delta can name the affected book
            // (the store mutation itself is untouched prototype logic).
            std::unordered_map<uint64_t, Order>::const_iterator it =
                books.orders().find(msg.order_number);
            if (it != books.orders().end()) {
                res.sections = SEC_BOOK;
                res.ticker = it->second.orderbook_id;
                res.group = it->second.group;
                res.has_delta = true;
                res.delta_op = 'D';
                res.order_number = msg.order_number;
                res.side = it->second.side;
                res.price = it->second.price;
                res.qty = it->second.remaining_qty;
                res.order_type = it->second.order_type;
            }
            books.apply(msg, execution);
            return res;
        }
        case 'U': {
            std::unordered_map<uint64_t, Order>::const_iterator it =
                books.orders().find(msg.orig_order_number);
            if (it != books.orders().end()) {
                res.sections = SEC_BOOK;
                res.ticker = it->second.orderbook_id;
                res.group = it->second.group;
                res.has_delta = true;
                res.delta_op = 'U';
                res.order_number = msg.new_order_number;
                res.orig_order_number = msg.orig_order_number;
                res.side = it->second.side; // inherited from the original
                res.price = msg.price;
                res.qty = msg.qty;
                res.order_type = it->second.order_type; // inherited
            }
            books.apply(msg, execution);
            return res;
        }
        case 'R':
        case 'L':
            refdata.apply(msg);
            res.sections = SEC_STATIC;
            if (msg.type == 'R') {
                res.ticker = msg.orderbook_id;
                res.group = msg.group;
            }
            return res;
        case 'H':
        case 'Y':
            refdata.apply(msg);
            res.sections = SEC_STATE;
            res.ticker = msg.orderbook_id;
            res.group = msg.group;
            return res;
        case 'S':
            refdata.apply(msg);
            res.sections = SEC_STATE;
            res.group = msg.group; // "" = system-wide
            return res;
        default:
            return res; // unreachable (known_type gate)
    }
}

void Market::restore_tick(uint32_t table_id, uint32_t price_start,
                          uint32_t tick_size) {
    refdata.tick_table(table_id).add(price_start, tick_size);
}

void Market::restore_order(uint64_t order_number, const std::string& ticker,
                           const std::string& group, char side,
                           uint32_t price, uint32_t qty, char order_type) {
    books.restore_order(order_number, ticker, group, side, price, qty,
                        order_type);
}

void Market::restore_instrument(const std::string& ticker,
                                const std::string& group,
                                const std::string& isin, int64_t round_lot,
                                int64_t tick_table_id, int64_t price_decimals,
                                int64_t upper_limit, int64_t lower_limit,
                                bool directory_seen, char trading_state,
                                char short_sell_state,
                                int64_t reference_price) {
    Instrument& inst = refdata.get(ticker);
    if (inst.group.empty()) {
        inst.group = group;
    }
    if (directory_seen) {
        inst.isin = isin;
        inst.round_lot = round_lot;
        inst.tick_table_id = tick_table_id;
        inst.price_decimals = price_decimals;
        inst.upper_limit = upper_limit;
        inst.lower_limit = lower_limit;
        inst.directory_missing = false;
    }
    if (trading_state != '\0' && trading_state != '?') {
        inst.trading_state = trading_state;
    }
    if (short_sell_state != '\0' && short_sell_state != '?') {
        inst.short_sell_state = short_sell_state;
    }
    if (reference_price >= 0) {
        inst.reference_price = reference_price;
    }
}

void Market::restore_trades(const std::string& ticker, uint64_t trade_count,
                            uint64_t volume, uint64_t notional,
                            int64_t last_price, int64_t last_qty,
                            uint64_t last_match_number) {
    tape.restore_stats(ticker, trade_count, volume, notional, last_price,
                       last_qty, last_match_number);
}

} // namespace jnx
