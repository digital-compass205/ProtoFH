// recover.h — FH startup recovery (JNX_PLAN2.md §1 restart matrix, row 1):
// rebuild the Market from a jnxdb GET_STATE dump so the FH can resume the
// exchange session at last_seq+1 with zero replay.
#ifndef JNX_FH_RECOVER_H
#define JNX_FH_RECOVER_H

#include <cstdint>
#include <string>

#include "fh/publish.h"
#include "market/market.h"

namespace jnx {

struct RecoveredMeta {
    std::string session;
    uint64_t last_exch_seq;
    uint64_t epoch;

    RecoveredMeta() : last_exch_seq(0), epoch(0) {}
};

// Pulls the full state from an already-connected DbLink (HELLO done) and
// rebuilds `market` + `ctx` from it: TICK rows -> tick tables, ORDER rows
// -> order store + levels, '#' UPDATE rows -> refdata/state/trade summary
// + publisher context. Fills `meta` from the SYNC_END. Returns false on
// stream failure (market may be partially filled — caller starts over
// with a fresh Market / falls back to bootstrap).
bool recover_from_db(DbLink& db, Market& market, PubContext& ctx,
                     RecoveredMeta& meta);

} // namespace jnx

#endif // JNX_FH_RECOVER_H
