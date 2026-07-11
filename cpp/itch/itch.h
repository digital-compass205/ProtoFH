// itch.h — Japannext ITCH v2.02 message codec (decode + encode).
//
// Layouts transcribed from JNX_PLAN.md §3.1/§3.2 (the frozen cheat sheet,
// byte-verified against the official samples). All integers big-endian on
// the wire; all byte access goes through cpp/common/endian.h (no struct
// casts). Alpha fields are stripped of trailing spaces on decode and
// space-padded on encode.
//
// Representation: ONE flat "wide" struct with a type char and the union of
// all per-type fields (only the fields for `type` are meaningful). This is
// the simplest C++11-safe tagged representation — no variant, no unions
// with non-trivial members.
//
// NOTE: decode does not strip meaning — a reference-price `A`
// (order_number == 0) decodes as a normal `A`; that policy lives upstream
// in the market core.
#ifndef JNX_ITCH_H
#define JNX_ITCH_H

#include <cstddef>
#include <cstdint>

namespace jnx {

// Price sentinel: "no reference price" (only in reference-price A msgs).
const uint32_t NO_PRICE = 0x7FFFFFFFu;

struct ItchMsg {
    char type; // 'T','S','L','R','H','Y','A','F','E','D','U','G'

    // T
    uint32_t seconds;
    // Every non-T message: nanoseconds since the last T.
    uint32_t ns;
    // S / R / H / Y / A / F
    char group[5]; // 4-byte Alpha + NUL ("" = blank/system-wide in S)
    // S
    char event; // O/S/Q/M/E/C
    // L
    uint32_t tick_table_id; // also in R
    uint32_t tick_size;
    uint32_t price_start;
    // R / H / Y / A / F
    char orderbook_id[5]; // 4-byte Alpha (SICC code — a string, NOT a number)
    // R
    char isin[13];
    uint32_t round_lot;
    uint32_t price_decimals;
    uint32_t upper_limit;
    uint32_t lower_limit;
    // H / Y
    char state; // H: T/V; Y: 0/1
    // A / F / E / D
    uint64_t order_number;
    // A / F
    char side; // B/S
    uint32_t qty; // also U
    uint32_t price; // also U
    // F
    char attribution[5];
    char order_type; // 'Q' = DLP
    // E
    uint32_t executed_qty;
    uint64_t match_number;
    // U
    uint64_t orig_order_number;
    uint64_t new_order_number;
    // G (GLIMPSE end-of-snapshot)
    uint64_t sequence_number;

    ItchMsg();
};

// Total wire length (including the leading type byte) for a message type;
// -1 for unknown types.
int expected_len(char type);

// Strict decode: len must equal expected_len(type); unknown type fails.
// On failure returns false and, if err != NULL, points *err at a static
// reason string.
bool decode(const unsigned char* buf, size_t len, ItchMsg& out,
            const char** err);

// Encodes msg into buf (caller provides >= expected_len(msg.type) bytes).
// Returns bytes written, or 0 if msg.type is unknown.
size_t encode(const ItchMsg& msg, unsigned char* buf);

} // namespace jnx

#endif // JNX_ITCH_H
