// levels.h — price-aggregated levels for one order book, both sides.
// C++ port of jnxfeed/book/orderbook.py _SideLevels/Book. The prototype
// tracks qty only; this port additionally maintains a live-order count per
// level and per-side running totals (qty + orders) because the F5 record
// builder needs them per message — the qty aggregation semantics are
// unchanged (the F2 parity gate still applies and still passes).
#ifndef JNX_MARKET_LEVELS_H
#define JNX_MARKET_LEVELS_H

#include <cstdint>
#include <functional>
#include <map>
#include <string>

namespace jnx {

// One aggregated price level. Aggregate qty is uint64: individual qtys
// are u32 but a level's sum may exceed it.
struct Level {
    uint64_t qty;
    uint32_t orders;

    Level() : qty(0), orders(0) {}
};

// Aggregated (qty, order count) per price for one side. Kept ascending
// (like the prototype's sorted list); Book's accessors present bids
// best-first by reverse iteration.
class SideLevels {
public:
    typedef std::map<uint32_t, Level> LevelMap;

    SideLevels() : total_qty_(0), total_orders_(0) {}

    // Adds one order's qty at price (level order count +1).
    void add(uint32_t price, uint32_t qty);

    // Removes qty from the price level; `order_gone` decrements the
    // level's order count (full removal / order filled to zero) — a
    // partial execution passes false. Erases the level at exactly zero
    // qty. Returns false if the level would go negative (a logic error —
    // the store clamps executions so this cannot happen on any input).
    bool remove(uint32_t price, uint32_t qty, bool order_gone);

    uint64_t qty_at(uint32_t price) const {
        LevelMap::const_iterator it = levels_.find(price);
        return it == levels_.end() ? 0 : it->second.qty;
    }

    uint64_t total_qty() const { return total_qty_; }
    uint32_t total_orders() const { return total_orders_; }

    size_t size() const { return levels_.size(); }

    // Ascending by price. Bids: iterate in reverse for best-first.
    const LevelMap& ascending() const { return levels_; }

private:
    LevelMap levels_;
    uint64_t total_qty_;
    uint32_t total_orders_;
};

// Both sides of one order book.
class Book {
public:
    Book() {}
    explicit Book(const std::string& orderbook_id)
        : orderbook_id_(orderbook_id) {}

    const std::string& orderbook_id() const { return orderbook_id_; }

    SideLevels& side(char s) { return s == 'B' ? bids_ : asks_; }
    const SideLevels& bids() const { return bids_; }
    const SideLevels& asks() const { return asks_; }

    void add(char s, uint32_t price, uint32_t qty) {
        side(s).add(price, qty);
    }
    bool remove(char s, uint32_t price, uint32_t qty, bool order_gone) {
        return side(s).remove(price, qty, order_gone);
    }

private:
    std::string orderbook_id_;
    SideLevels bids_;
    SideLevels asks_;
};

} // namespace jnx

#endif // JNX_MARKET_LEVELS_H
