// orders.h — order-number-keyed store + per-book aggregated price levels.
// C++ port of jnxfeed/book/orderbook.py OrderBookStore, with identical
// semantics and counter behavior:
//
// - A/F insert (ref-price A with order_number==0 is counted as
//   ref_price_ignored and dropped — it belongs to refdata; Market routes
//   it there and never calls the store with it).
// - E resolves the stored passive order; trade price = the STORED order's
//   price; executions are cumulative; executed_qty > remaining clamps to
//   remaining (counted); order erased at exactly zero remaining.
// - D erases; unknown order number = orphan (counted, ignored).
// - U erases the original and inserts the new order inheriting
//   orderbook_id/group/side from the ORIGINAL, price/qty from the message.
//   Unknown original = orphan; the new order is NOT inserted.
// - Insert over an already-live order number = collision: counted, the
//   stale order is removed (store + levels) and replaced.
#ifndef JNX_MARKET_ORDERS_H
#define JNX_MARKET_ORDERS_H

#include <cstdint>
#include <map>
#include <string>
#include <unordered_map>

#include "itch/itch.h"
#include "market/levels.h"

namespace jnx {

struct Order {
    uint64_t order_number;
    std::string orderbook_id;
    std::string group;
    char side; // 'B'/'S'
    uint32_t price;
    uint32_t remaining_qty;
    // 'Q' = DLP (from F), ' ' = plain. Not in the prototype (which never
    // consumed it downstream); inherited by U like side/book. Never dumped
    // by book_dump, so F2 parity is unaffected.
    char order_type;

    Order()
        : order_number(0), side('\0'), price(0), remaining_qty(0),
          order_type(' ') {}
};

// One fill derived from an E against the stored passive order. side/price
// are the PASSIVE order's (§3.3(2)).
struct Execution {
    std::string orderbook_id;
    std::string group;
    char side;
    uint32_t price;
    uint32_t qty; // clamped executed qty
    uint64_t match_number;

    Execution() : side('\0'), price(0), qty(0), match_number(0) {}
};

class OrderBookStore {
public:
    OrderBookStore()
        : collisions(0),
          orphan_executes(0),
          orphan_deletes(0),
          orphan_replaces(0),
          ref_price_ignored(0),
          clamped_executions(0),
          executed_volume(0),
          execution_count(0) {}

    // Applies one book message (A/F/E/D/U). For an E matched against a
    // known order fills `execution` and returns true; everything else
    // returns false (including consumed A/F/D/U). Non-book types are a
    // no-op (Market routes; passing anything else here does nothing).
    bool apply(const ItchMsg& msg, Execution& execution);

    // The Book for orderbook_id, auto-created on first use (mirrors the
    // prototype: books exist once an order touched them).
    Book& book(const std::string& orderbook_id);

    // Recovery-only injection (F5): place one order directly into the
    // store + levels, no counters, no collision handling (the recovery
    // stream is clean by construction). NEVER used on the live path.
    void restore_order(uint64_t order_number, const std::string& orderbook_id,
                       const std::string& group, char side, uint32_t price,
                       uint32_t qty, char order_type);

    const std::unordered_map<uint64_t, Order>& orders() const {
        return orders_;
    }
    const std::map<std::string, Book>& books() const { return books_; }
    size_t live_order_count() const { return orders_.size(); }
    uint64_t orphans_total() const {
        return orphan_executes + orphan_deletes + orphan_replaces;
    }

    // Diagnostics (public, mirroring the prototype's attributes).
    uint64_t collisions;        // insert over an already-live number
    uint64_t orphan_executes;   // E referencing an unknown order number
    uint64_t orphan_deletes;    // D referencing an unknown order number
    uint64_t orphan_replaces;   // U referencing an unknown orig number
    uint64_t ref_price_ignored; // ref-price A misrouted here
    uint64_t clamped_executions; // E with executed_qty > remaining
    uint64_t executed_volume;   // total qty across all executions
    uint64_t execution_count;

private:
    void insert(uint64_t order_number, const std::string& orderbook_id,
                const std::string& group, char side, uint32_t price,
                uint32_t qty, char order_type);
    void remove_order(const Order& order);
    bool execute(const ItchMsg& msg, Execution& execution);
    void erase(const ItchMsg& msg);
    void replace(const ItchMsg& msg);

    std::unordered_map<uint64_t, Order> orders_;
    // std::map so every iteration (dumps!) is deterministically sorted.
    std::map<std::string, Book> books_;
};

} // namespace jnx

#endif // JNX_MARKET_ORDERS_H
