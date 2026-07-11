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
        case 'E':
            if (books.apply(msg, execution)) {
                tape.record(execution, make_timestamp(seconds, msg.ns));
                res.sections = SEC_BOOK | SEC_TRADE;
                res.ticker = execution.orderbook_id;
                res.group = execution.group;
                res.has_delta = true;
                res.delta_op = 'E';
                res.order_number = msg.order_number;
                res.side = execution.side;
                res.price = execution.price; // passive stored price
                res.qty = execution.qty;     // clamped executed qty
                res.has_trade = true;
                res.trade = execution;
            }
            return res; // orphan E: applied, but nothing changed
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

} // namespace jnx
