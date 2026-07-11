// record.h — JNX record codec: the single wire format for the FH->DB UDS
// stream, the DB->FH recovery stream, and the UDP multicast feed.
//
// Byte layout is FROZEN by docs/wire_spec.md (version 1) — both this codec
// and jnxweb/records.py implement that document exactly. All integers are
// big-endian on the wire; all byte access goes through cpp/common/endian.h
// (never struct casts). Alpha fields are space-padded on encode, stripped
// on decode. Every unused/padding byte is zero-filled on encode.
#ifndef JNX_WIRE_RECORD_H
#define JNX_WIRE_RECORD_H

#include <cstddef>
#include <cstdint>
#include <vector>

namespace jnx {

// --- header ---------------------------------------------------------------

const uint16_t RECORD_MAGIC = 0x4A58;  // "JX"
const uint8_t RECORD_VERSION = 1;
const size_t RECORD_HEADER_SIZE = 8;

// Record kinds (header `kind` byte).
const char KIND_UPDATE = 'U';
const char KIND_ORDER = 'O';
const char KIND_TICK = 'K';
const char KIND_SYNC_BEGIN = 'B';
const char KIND_SYNC_END = 'E';
const char KIND_GET_STATE = 'G';
const char KIND_HELLO = 'H';
const char KIND_RESET = 'R';

// Fixed body sizes per kind (docs/wire_spec.md).
const size_t UPDATE_BODY_SIZE = 425;
const size_t ORDER_BODY_SIZE = 26;
const size_t TICK_BODY_SIZE = 12;
const size_t SYNC_END_BODY_SIZE = 26;
const size_t HELLO_BODY_SIZE = 16;
// SYNC_BEGIN / GET_STATE / RESET have empty bodies.

const size_t UPDATE_WIRE_SIZE = RECORD_HEADER_SIZE + UPDATE_BODY_SIZE;  // 433
const size_t ORDER_WIRE_SIZE = RECORD_HEADER_SIZE + ORDER_BODY_SIZE;    // 34
const size_t TICK_WIRE_SIZE = RECORD_HEADER_SIZE + TICK_BODY_SIZE;      // 20
const size_t SYNC_END_WIRE_SIZE = RECORD_HEADER_SIZE + SYNC_END_BODY_SIZE; // 34
const size_t HELLO_WIRE_SIZE = RECORD_HEADER_SIZE + HELLO_BODY_SIZE;    // 24
const size_t CONTROL_WIRE_SIZE = RECORD_HEADER_SIZE;  // B / G / R      // 8

// Largest possible record — handy for caller-side buffers.
const size_t MAX_RECORD_WIRE_SIZE = UPDATE_WIRE_SIZE;

// Fixed body size for a kind; -1 for unknown kinds.
int record_body_len(char kind);

// --- UPDATE ----------------------------------------------------------------

const int BOOK_DEPTH = 10;

struct BookLevel {
    uint32_t price;
    uint32_t qty;
    uint32_t order_count;

    BookLevel() : price(0), qty(0), order_count(0) {}
};

// Flags byte (static section) bit assignments — docs/wire_spec.md.
const uint8_t FLAG_DIRECTORY_SEEN = 0x01;
const uint8_t FLAG_ORDER_COLLISION_SEEN = 0x02;

// Full merged per-(ticker,group) row + order delta. Host-order fields;
// char[] fields are NUL-terminated stripped strings (one extra byte).
// Single-char tag fields use '\0' for "not applicable / no value".
struct UpdateRecord {
    // envelope
    uint64_t epoch;
    uint64_t pub_seq;
    char session[11];  // <= 10 chars
    uint64_t exch_seq;
    uint64_t exch_ns;
    char trigger;      // ITCH type char, or '#' for sync-dump rows
    char ticker[5];    // 4-byte alpha (SICC — a string, NOT a number)
    char group[5];     // 4-byte alpha

    // static section (T1)
    char isin[13];     // <= 12 chars
    uint32_t round_lot;
    uint32_t tick_table_id;
    uint8_t price_decimals;
    uint32_t upper_limit;
    uint32_t lower_limit;
    uint8_t flags;     // FLAG_* bits

    // state section (T2)
    char trading_state;           // 'T'/'V'/'?'
    char short_sell_restriction;  // '0'/'1'/'?'
    uint32_t reference_price;     // may be NO_PRICE
    char last_system_event;       // O/S/Q/M/E/C, '\0' = none

    // book section (T4)
    uint8_t level_count_bid;  // 0..10
    uint8_t level_count_ask;  // 0..10
    BookLevel bids[BOOK_DEPTH];  // best (highest) first
    BookLevel asks[BOOK_DEPTH];  // best (lowest) first
    uint64_t total_bid_qty;      // whole book, not just top 10
    uint64_t total_ask_qty;
    uint32_t total_bid_orders;
    uint32_t total_ask_orders;

    // trade summary section (T5, no tape)
    uint32_t last_price;
    uint32_t last_qty;
    uint64_t last_match_number;
    uint64_t last_trade_ns;
    uint64_t cum_qty;
    uint64_t cum_turnover;
    uint32_t trade_count;

    // delta section (T3 mutation)
    char delta_op;  // 'A'/'E'/'D'/'U'/'#'
    uint64_t delta_order_number;
    uint64_t delta_orig_order_number;  // 'U' only, else 0
    char delta_side;                   // 'B'/'S', '\0' when op '#'
    uint32_t delta_price;
    uint32_t delta_qty;
    char delta_order_type;  // 'Q' = DLP, ' ' = plain, '\0' when op '#'

    UpdateRecord();
};

// --- ORDER / TICK / HELLO / SYNC_END ---------------------------------------

struct OrderRecord {
    uint64_t order_number;
    char ticker[5];
    char group[5];
    char side;  // 'B'/'S'
    uint32_t price;
    uint32_t qty_remaining;
    char order_type;  // 'Q' = DLP, ' ' = plain

    OrderRecord();
};

struct TickRecord {
    uint32_t table_id;
    uint32_t price_start;
    uint32_t tick_size;

    TickRecord() : table_id(0), price_start(0), tick_size(0) {}
};

struct HelloRecord {
    uint64_t epoch;          // 0 = fresh FH / empty DB
    uint64_t last_exch_seq;  // 0 = none

    HelloRecord() : epoch(0), last_exch_seq(0) {}
};

struct SyncEndRecord {
    char session[11];  // <= 10 chars
    uint64_t last_exch_seq;
    uint64_t epoch;

    SyncEndRecord();
};

// --- encode ------------------------------------------------------------------
// Each writes header + body into buf (caller provides >= the kind's
// *_WIRE_SIZE bytes) and returns total bytes written. All padding and
// unused regions are zero-filled.

size_t encode_update(const UpdateRecord& in, unsigned char* buf);
size_t encode_order(const OrderRecord& in, unsigned char* buf);
size_t encode_tick(const TickRecord& in, unsigned char* buf);
size_t encode_hello(const HelloRecord& in, unsigned char* buf);
size_t encode_sync_end(const SyncEndRecord& in, unsigned char* buf);
// Body-less kinds: KIND_SYNC_BEGIN, KIND_GET_STATE, KIND_RESET.
// Returns 0 if kind is not one of those three.
size_t encode_control(char kind, unsigned char* buf);

// --- decode ------------------------------------------------------------------
// Strict: validate magic, version, kind, and body_len == the kind's fixed
// size; len must equal the kind's total wire size. On failure return false
// and, if err != NULL, point *err at a static reason string.

// Validates a header and returns its kind through *kind (buf must hold
// >= RECORD_HEADER_SIZE bytes). body_len is returned through *body_len.
bool decode_header(const unsigned char* buf, size_t len, char* kind,
                   uint16_t* body_len, const char** err);

bool decode_update(const unsigned char* buf, size_t len, UpdateRecord& out,
                   const char** err);
bool decode_order(const unsigned char* buf, size_t len, OrderRecord& out,
                  const char** err);
bool decode_tick(const unsigned char* buf, size_t len, TickRecord& out,
                 const char** err);
bool decode_hello(const unsigned char* buf, size_t len, HelloRecord& out,
                  const char** err);
bool decode_sync_end(const unsigned char* buf, size_t len, SyncEndRecord& out,
                     const char** err);

// --- stream framer -------------------------------------------------------------
// Incremental reassembler for the UDS stream (concatenated records — the
// header's body_len delimits; no extra length prefix). Same interface
// pattern as SoupFramer: feed() bytes as they arrive (byte-at-a-time safe,
// header may be split across feeds), pull complete records with next().
//
// A header that fails validation (bad magic/version/kind/body_len) marks
// the stream corrupt: the buffer is discarded, next() returns false
// forever, and corrupt() turns true. A corrupt record stream is
// unrecoverable by design (docs/wire_spec.md) — the connection owner
// must close and start a fresh framer on reconnect.

struct RawRecord {
    char kind;
    std::vector<unsigned char> body;  // owned copy, body_len bytes

    RawRecord() : kind('\0') {}
};

class RecordFramer {
public:
    RecordFramer() : corrupt_(false), corrupt_reason_(0) {}

    void feed(const unsigned char* data, size_t len);

    // Extracts the next complete record; false if none buffered (or the
    // stream is corrupt).
    bool next(RawRecord& out);

    bool corrupt() const { return corrupt_; }
    // Reason the stream was marked corrupt (static string), or NULL.
    const char* corrupt_reason() const { return corrupt_reason_; }

private:
    std::vector<unsigned char> buf_;
    bool corrupt_;
    const char* corrupt_reason_;
};

} // namespace jnx

#endif // JNX_WIRE_RECORD_H
