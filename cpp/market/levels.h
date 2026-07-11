// levels.h — price-aggregated levels for one order book, both sides.
// C++ port of jnxfeed/book/orderbook.py _SideLevels/Book (qty per price;
// the prototype tracks NO per-level order count — order counts are derived
// from the order store by consumers that need them).
#ifndef JNX_MARKET_LEVELS_H
#define JNX_MARKET_LEVELS_H

#include <cstdint>
#include <functional>
#include <map>
#include <string>

namespace jnx {

// Aggregated qty per price for one side. Kept ascending (like the
// prototype's sorted list); Book's accessors present bids best-first by
// reverse iteration.
class SideLevels {
public:
    // Aggregate qty as uint64: individual qtys are u32 but a level's sum
    // may exceed it.
    typedef std::map<uint32_t, uint64_t> LevelMap;

    void add(uint32_t price, uint32_t qty);

    // Removes qty from the price level; erases the level at exactly zero.
    // Returns false if the level would go negative (a logic error — the
    // store clamps executions so this cannot happen on any input).
    bool remove(uint32_t price, uint32_t qty);

    uint64_t qty_at(uint32_t price) const {
        LevelMap::const_iterator it = levels_.find(price);
        return it == levels_.end() ? 0 : it->second;
    }

    uint64_t total_qty() const;

    size_t size() const { return levels_.size(); }

    // Ascending by price. Bids: iterate in reverse for best-first.
    const LevelMap& ascending() const { return levels_; }

private:
    LevelMap levels_;
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
    bool remove(char s, uint32_t price, uint32_t qty) {
        return side(s).remove(price, qty);
    }

private:
    std::string orderbook_id_;
    SideLevels bids_;
    SideLevels asks_;
};

} // namespace jnx

#endif // JNX_MARKET_LEVELS_H
