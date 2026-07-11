// levels.cpp — see levels.h.
#include "market/levels.h"

namespace jnx {

void SideLevels::add(uint32_t price, uint32_t qty) {
    Level& lvl = levels_[price];
    lvl.qty += qty;
    ++lvl.orders;
    total_qty_ += qty;
    ++total_orders_;
}

bool SideLevels::remove(uint32_t price, uint32_t qty, bool order_gone) {
    LevelMap::iterator it = levels_.find(price);
    if (it == levels_.end() || it->second.qty < qty) {
        return false; // would go negative — upstream logic error
    }
    it->second.qty -= qty;
    total_qty_ -= qty;
    if (order_gone) {
        if (it->second.orders > 0) {
            --it->second.orders;
        }
        if (total_orders_ > 0) {
            --total_orders_;
        }
    }
    if (it->second.qty == 0) {
        levels_.erase(it);
    }
    return true;
}

} // namespace jnx
