// glimpse.h — GLIMPSE snapshot bootstrap (JNX_PLAN.md §3.5).
//
// Synchronous by design: bootstrap happens strictly before the live
// reactor phase, so a blocking socket with timeouts is the simplest
// correct implementation. Logs in with a BLANK requested session and
// seq 1, applies every snapshot ITCH message to the Market, and returns
// when the `G` End of Snapshot arrives.
#ifndef JNX_FH_GLIMPSE_H
#define JNX_FH_GLIMPSE_H

#include <cstdint>
#include <string>

#include "market/market.h"

namespace jnx {

struct GlimpseResult {
    std::string session;    // session id from Login Accepted
    uint64_t next_live_seq; // from the G message
    uint64_t message_count; // snapshot messages applied (excluding G)

    GlimpseResult() : next_live_seq(0), message_count(0) {}
};

// Returns false with *err (static string) on failure: connect/login
// rejected/connection lost before G/timeout. Applies snapshot messages
// into `market` (a failure can leave it partially filled — callers retry
// with a FRESH Market).
bool glimpse_bootstrap(const std::string& host, int port,
                       const std::string& username,
                       const std::string& password, Market& market,
                       GlimpseResult& out, const char** err,
                       uint64_t timeout_ns = 30000000000ULL);

} // namespace jnx

#endif // JNX_FH_GLIMPSE_H
