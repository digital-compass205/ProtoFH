// itch.cpp — see itch.h. All field offsets from JNX_PLAN.md §3.2.
#include "itch/itch.h"

#include <cstring>

#include "common/endian.h"

namespace jnx {

namespace {

// Decode a fixed-width Alpha field: copy, strip trailing spaces, NUL-term.
// `out` must have room for width+1 chars.
void get_alpha(const unsigned char* p, size_t width, char* out) {
    size_t n = width;
    while (n > 0 && p[n - 1] == ' ') {
        --n;
    }
    std::memcpy(out, p, n);
    out[n] = '\0';
}

// Encode a fixed-width Alpha field: left-justified, right-padded w/ spaces.
void put_alpha(unsigned char* p, size_t width, const char* s) {
    size_t n = std::strlen(s);
    if (n > width) {
        n = width;
    }
    std::memcpy(p, s, n);
    std::memset(p + n, ' ', width - n);
}

} // namespace

ItchMsg::ItchMsg()
    : type('\0'),
      seconds(0),
      ns(0),
      event('\0'),
      tick_table_id(0),
      tick_size(0),
      price_start(0),
      round_lot(0),
      price_decimals(0),
      upper_limit(0),
      lower_limit(0),
      state('\0'),
      order_number(0),
      side('\0'),
      qty(0),
      price(0),
      order_type('\0'),
      executed_qty(0),
      match_number(0),
      orig_order_number(0),
      new_order_number(0),
      sequence_number(0) {
    group[0] = '\0';
    orderbook_id[0] = '\0';
    isin[0] = '\0';
    attribution[0] = '\0';
}

int expected_len(char type) {
    switch (type) {
        case 'T': return 5;
        case 'S': return 10;
        case 'L': return 17;
        case 'R': return 45;
        case 'H': return 14;
        case 'Y': return 14;
        case 'A': return 30;
        case 'F': return 35;
        case 'E': return 25;
        case 'D': return 13;
        case 'U': return 29;
        case 'G': return 9;
        default: return -1;
    }
}

bool decode(const unsigned char* buf, size_t len, ItchMsg& out,
            const char** err) {
    if (len == 0) {
        if (err != NULL) *err = "empty buffer";
        return false;
    }
    char type = static_cast<char>(buf[0]);
    int want = expected_len(type);
    if (want < 0) {
        if (err != NULL) *err = "unknown message type";
        return false;
    }
    if (len != static_cast<size_t>(want)) {
        if (err != NULL) *err = "length mismatch for message type";
        return false;
    }
    out = ItchMsg();
    out.type = type;
    switch (type) {
        case 'T':
            out.seconds = be_get_u32(buf + 1);
            break;
        case 'S':
            out.ns = be_get_u32(buf + 1);
            get_alpha(buf + 5, 4, out.group);
            out.event = static_cast<char>(buf[9]);
            break;
        case 'L':
            out.ns = be_get_u32(buf + 1);
            out.tick_table_id = be_get_u32(buf + 5);
            out.tick_size = be_get_u32(buf + 9);
            out.price_start = be_get_u32(buf + 13);
            break;
        case 'R':
            out.ns = be_get_u32(buf + 1);
            get_alpha(buf + 5, 4, out.orderbook_id);
            get_alpha(buf + 9, 12, out.isin);
            get_alpha(buf + 21, 4, out.group);
            out.round_lot = be_get_u32(buf + 25);
            out.tick_table_id = be_get_u32(buf + 29);
            out.price_decimals = be_get_u32(buf + 33);
            out.upper_limit = be_get_u32(buf + 37);
            out.lower_limit = be_get_u32(buf + 41);
            break;
        case 'H':
        case 'Y':
            out.ns = be_get_u32(buf + 1);
            get_alpha(buf + 5, 4, out.orderbook_id);
            get_alpha(buf + 9, 4, out.group);
            out.state = static_cast<char>(buf[13]);
            break;
        case 'A':
        case 'F':
            out.ns = be_get_u32(buf + 1);
            out.order_number = be_get_u64(buf + 5);
            out.side = static_cast<char>(buf[13]);
            out.qty = be_get_u32(buf + 14);
            get_alpha(buf + 18, 4, out.orderbook_id);
            get_alpha(buf + 22, 4, out.group);
            out.price = be_get_u32(buf + 26);
            if (type == 'F') {
                get_alpha(buf + 30, 4, out.attribution);
                out.order_type = static_cast<char>(buf[34]);
            }
            break;
        case 'E':
            out.ns = be_get_u32(buf + 1);
            out.order_number = be_get_u64(buf + 5);
            out.executed_qty = be_get_u32(buf + 13);
            out.match_number = be_get_u64(buf + 17);
            break;
        case 'D':
            out.ns = be_get_u32(buf + 1);
            out.order_number = be_get_u64(buf + 5);
            break;
        case 'U':
            out.ns = be_get_u32(buf + 1);
            out.orig_order_number = be_get_u64(buf + 5);
            out.new_order_number = be_get_u64(buf + 13);
            out.qty = be_get_u32(buf + 21);
            out.price = be_get_u32(buf + 25);
            break;
        case 'G':
            out.sequence_number = be_get_u64(buf + 1);
            break;
        default:
            if (err != NULL) *err = "unknown message type";
            return false;
    }
    return true;
}

size_t encode(const ItchMsg& msg, unsigned char* buf) {
    int want = expected_len(msg.type);
    if (want < 0) {
        return 0;
    }
    buf[0] = static_cast<unsigned char>(msg.type);
    switch (msg.type) {
        case 'T':
            be_put_u32(buf + 1, msg.seconds);
            break;
        case 'S':
            be_put_u32(buf + 1, msg.ns);
            put_alpha(buf + 5, 4, msg.group);
            buf[9] = static_cast<unsigned char>(msg.event);
            break;
        case 'L':
            be_put_u32(buf + 1, msg.ns);
            be_put_u32(buf + 5, msg.tick_table_id);
            be_put_u32(buf + 9, msg.tick_size);
            be_put_u32(buf + 13, msg.price_start);
            break;
        case 'R':
            be_put_u32(buf + 1, msg.ns);
            put_alpha(buf + 5, 4, msg.orderbook_id);
            put_alpha(buf + 9, 12, msg.isin);
            put_alpha(buf + 21, 4, msg.group);
            be_put_u32(buf + 25, msg.round_lot);
            be_put_u32(buf + 29, msg.tick_table_id);
            be_put_u32(buf + 33, msg.price_decimals);
            be_put_u32(buf + 37, msg.upper_limit);
            be_put_u32(buf + 41, msg.lower_limit);
            break;
        case 'H':
        case 'Y':
            be_put_u32(buf + 1, msg.ns);
            put_alpha(buf + 5, 4, msg.orderbook_id);
            put_alpha(buf + 9, 4, msg.group);
            buf[13] = static_cast<unsigned char>(msg.state);
            break;
        case 'A':
        case 'F':
            be_put_u32(buf + 1, msg.ns);
            be_put_u64(buf + 5, msg.order_number);
            buf[13] = static_cast<unsigned char>(msg.side);
            be_put_u32(buf + 14, msg.qty);
            put_alpha(buf + 18, 4, msg.orderbook_id);
            put_alpha(buf + 22, 4, msg.group);
            be_put_u32(buf + 26, msg.price);
            if (msg.type == 'F') {
                put_alpha(buf + 30, 4, msg.attribution);
                buf[34] = static_cast<unsigned char>(msg.order_type);
            }
            break;
        case 'E':
            be_put_u32(buf + 1, msg.ns);
            be_put_u64(buf + 5, msg.order_number);
            be_put_u32(buf + 13, msg.executed_qty);
            be_put_u64(buf + 17, msg.match_number);
            break;
        case 'D':
            be_put_u32(buf + 1, msg.ns);
            be_put_u64(buf + 5, msg.order_number);
            break;
        case 'U':
            be_put_u32(buf + 1, msg.ns);
            be_put_u64(buf + 5, msg.orig_order_number);
            be_put_u64(buf + 13, msg.new_order_number);
            be_put_u32(buf + 21, msg.qty);
            be_put_u32(buf + 25, msg.price);
            break;
        case 'G':
            be_put_u64(buf + 1, msg.sequence_number);
            break;
        default:
            return 0;
    }
    return static_cast<size_t>(want);
}

} // namespace jnx
