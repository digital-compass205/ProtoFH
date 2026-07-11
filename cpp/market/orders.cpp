// orders.cpp — see orders.h. Mirrors jnxfeed/book/orderbook.py exactly.
#include "market/orders.h"

#include "common/log.h"

namespace jnx {

Book& OrderBookStore::book(const std::string& orderbook_id) {
    std::map<std::string, Book>::iterator it = books_.find(orderbook_id);
    if (it == books_.end()) {
        it = books_.insert(std::make_pair(orderbook_id, Book(orderbook_id)))
                 .first;
    }
    return it->second;
}

bool OrderBookStore::apply(const ItchMsg& msg, Execution& execution) {
    switch (msg.type) {
        case 'A':
        case 'F':
            if (msg.order_number == 0) {
                // Reference-price pseudo-order (§3.3(1)) — refdata's
                // business. Count so misrouting shows up.
                ++ref_price_ignored;
                return false;
            }
            insert(msg.order_number, msg.orderbook_id, msg.group, msg.side,
                   msg.price, msg.qty,
                   msg.type == 'F' ? msg.order_type : ' ');
            return false;
        case 'E':
            return execute(msg, execution);
        case 'D':
            erase(msg);
            return false;
        case 'U':
            replace(msg);
            return false;
        default:
            return false;
    }
}

void OrderBookStore::insert(uint64_t order_number,
                            const std::string& orderbook_id,
                            const std::string& group, char side,
                            uint32_t price, uint32_t qty, char order_type) {
    std::unordered_map<uint64_t, Order>::iterator it =
        orders_.find(order_number);
    if (it != orders_.end()) {
        // §3.3(3): cross-group collision on a combined feed. Count + warn
        // and replace the stale record (keeping both corrupts the levels).
        ++collisions;
        LOG_WARN("orders") << "order number collision: #" << order_number
                           << " already live (" << it->second.orderbook_id
                           << "), replacing with " << orderbook_id << " "
                           << side << " " << qty << "@" << price;
        remove_order(it->second);
    }
    Order order;
    order.order_number = order_number;
    order.orderbook_id = orderbook_id;
    order.group = group;
    order.side = side;
    order.price = price;
    order.remaining_qty = qty;
    order.order_type = order_type;
    orders_[order_number] = order;
    book(orderbook_id).add(side, price, qty);
}

void OrderBookStore::restore_order(uint64_t order_number,
                                   const std::string& orderbook_id,
                                   const std::string& group, char side,
                                   uint32_t price, uint32_t qty,
                                   char order_type) {
    Order order;
    order.order_number = order_number;
    order.orderbook_id = orderbook_id;
    order.group = group;
    order.side = side;
    order.price = price;
    order.remaining_qty = qty;
    order.order_type = order_type;
    orders_[order_number] = order;
    book(orderbook_id).add(side, price, qty);
}

void OrderBookStore::remove_order(const Order& order) {
    // Copy the fields we need before erasing (order may reference the
    // stored object).
    std::string oid = order.orderbook_id;
    char side = order.side;
    uint32_t price = order.price;
    uint32_t rem = order.remaining_qty;
    orders_.erase(order.order_number);
    book(oid).remove(side, price, rem, true);
}

bool OrderBookStore::execute(const ItchMsg& msg, Execution& execution) {
    std::unordered_map<uint64_t, Order>::iterator it =
        orders_.find(msg.order_number);
    if (it == orders_.end()) {
        ++orphan_executes;
        return false;
    }
    Order& order = it->second;
    uint32_t qty = msg.executed_qty;
    if (qty > order.remaining_qty) {
        // Should not happen on a clean feed; clamp so levels never go
        // negative, and make it loud.
        LOG_WARN("orders") << "execution of " << qty << " exceeds remaining "
                           << order.remaining_qty << " on #"
                           << order.order_number << "; clamping";
        ++clamped_executions;
        qty = order.remaining_qty;
    }
    order.remaining_qty -= qty;
    book(order.orderbook_id)
        .remove(order.side, order.price, qty, order.remaining_qty == 0);
    execution.orderbook_id = order.orderbook_id;
    execution.group = order.group;
    execution.side = order.side;
    execution.price = order.price;
    execution.qty = qty;
    execution.match_number = msg.match_number;
    if (order.remaining_qty == 0) {
        orders_.erase(it);
    }
    executed_volume += qty;
    ++execution_count;
    return true;
}

void OrderBookStore::erase(const ItchMsg& msg) {
    std::unordered_map<uint64_t, Order>::iterator it =
        orders_.find(msg.order_number);
    if (it == orders_.end()) {
        ++orphan_deletes;
        return;
    }
    remove_order(it->second);
}

void OrderBookStore::replace(const ItchMsg& msg) {
    std::unordered_map<uint64_t, Order>::iterator it =
        orders_.find(msg.orig_order_number);
    if (it == orders_.end()) {
        // Without the original we know neither book nor side, so the new
        // order cannot be placed either (§3.3(2)).
        ++orphan_replaces;
        return;
    }
    // Copy inherited fields before removal invalidates the reference.
    std::string oid = it->second.orderbook_id;
    std::string group = it->second.group;
    char side = it->second.side;
    char order_type = it->second.order_type;
    remove_order(it->second);
    insert(msg.new_order_number, oid, group, side, msg.price, msg.qty,
           order_type);
}

} // namespace jnx
