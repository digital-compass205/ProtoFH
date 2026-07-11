// levels.cpp — see levels.h.
#include "market/levels.h"

namespace jnx {

void SideLevels::add(uint32_t price, uint32_t qty) {
    LevelMap::iterator it = levels_.find(price);
    if (it == levels_.end()) {
        levels_[price] = qty;
    } else {
        it->second += qty;
    }
}

bool SideLevels::remove(uint32_t price, uint32_t qty) {
    LevelMap::iterator it = levels_.find(price);
    if (it == levels_.end() || it->second < qty) {
        return false; // would go negative — upstream logic error
    }
    it->second -= qty;
    if (it->second == 0) {
        levels_.erase(it);
    }
    return true;
}

uint64_t SideLevels::total_qty() const {
    uint64_t total = 0;
    for (LevelMap::const_iterator it = levels_.begin(); it != levels_.end();
         ++it) {
        total += it->second;
    }
    return total;
}

} // namespace jnx
