// soup.cpp — see soup.h.
#include "soup/soup.h"

#include <cstdio>
#include <cstring>

#include "common/endian.h"

namespace jnx {

namespace {

// Strip leading AND trailing spaces of a fixed-width field into out
// (room for width+1). Soup session fields are left-padded, so leading
// spaces are padding; trailing spaces can't be meaningful either.
void get_padded_str(const unsigned char* p, size_t width, char* out) {
    size_t start = 0;
    while (start < width && p[start] == ' ') {
        ++start;
    }
    size_t end = width;
    while (end > start && p[end - 1] == ' ') {
        --end;
    }
    std::memcpy(out, p + start, end - start);
    out[end - start] = '\0';
}

// Right-pad (left-justify) s into a width-byte field.
void put_right_padded(unsigned char* p, size_t width, const char* s) {
    size_t n = std::strlen(s);
    if (n > width) {
        n = width;
    }
    std::memcpy(p, s, n);
    std::memset(p + n, ' ', width - n);
}

// Left-pad (right-justify) s into a width-byte field.
void put_left_padded(unsigned char* p, size_t width, const char* s) {
    size_t n = std::strlen(s);
    if (n > width) {
        n = width;
    }
    std::memset(p, ' ', width - n);
    std::memcpy(p + (width - n), s, n);
}

// Parse an ASCII-decimal, space-left-padded numeric field. Empty (all
// spaces) is invalid.
bool parse_seq_field(const unsigned char* p, size_t width, uint64_t& out) {
    size_t i = 0;
    while (i < width && p[i] == ' ') {
        ++i;
    }
    if (i == width) {
        return false;
    }
    uint64_t v = 0;
    for (; i < width; ++i) {
        if (p[i] < '0' || p[i] > '9') {
            return false;
        }
        v = v * 10 + static_cast<uint64_t>(p[i] - '0');
    }
    out = v;
    return true;
}

// Render v as ASCII decimal left-padded with spaces into width bytes.
void put_seq_field(unsigned char* p, size_t width, uint64_t v) {
    char tmp[32];
    int n = std::snprintf(tmp, sizeof(tmp), "%llu",
                          static_cast<unsigned long long>(v));
    put_left_padded(p, width, tmp);
    (void)n;
}

} // namespace

bool soup_parse_login_request(const unsigned char* payload, size_t len,
                              SoupLoginRequest& out, const char** err) {
    if (len != 6 + 10 + 10 + 20) {
        if (err != NULL) *err = "LoginRequest payload must be 46 bytes";
        return false;
    }
    get_padded_str(payload, 6, out.username);
    get_padded_str(payload + 6, 10, out.password);
    get_padded_str(payload + 16, 10, out.requested_session);
    if (!parse_seq_field(payload + 26, 20, out.requested_sequence)) {
        if (err != NULL) *err = "LoginRequest bad requested_sequence";
        return false;
    }
    return true;
}

bool soup_parse_login_accepted(const unsigned char* payload, size_t len,
                               SoupLoginAccepted& out, const char** err) {
    if (len != 10 + 20) {
        if (err != NULL) *err = "LoginAccepted payload must be 30 bytes";
        return false;
    }
    get_padded_str(payload, 10, out.session);
    if (!parse_seq_field(payload + 10, 20, out.sequence)) {
        if (err != NULL) *err = "LoginAccepted bad sequence";
        return false;
    }
    return true;
}

bool soup_parse_login_rejected(const unsigned char* payload, size_t len,
                               SoupLoginRejected& out, const char** err) {
    if (len != 1) {
        if (err != NULL) *err = "LoginRejected payload must be 1 byte";
        return false;
    }
    out.reject_code = static_cast<char>(payload[0]);
    return true;
}

size_t soup_build_login_request(const SoupLoginRequest& in,
                                unsigned char* buf) {
    be_put_u16(buf, static_cast<uint16_t>(1 + 46));
    buf[2] = 'L';
    put_right_padded(buf + 3, 6, in.username);
    put_right_padded(buf + 9, 10, in.password);
    put_left_padded(buf + 19, 10, in.requested_session);
    put_seq_field(buf + 29, 20, in.requested_sequence);
    return SOUP_LOGIN_REQUEST_WIRE;
}

size_t soup_build_login_accepted(const SoupLoginAccepted& in,
                                 unsigned char* buf) {
    be_put_u16(buf, static_cast<uint16_t>(1 + 30));
    buf[2] = 'A';
    put_left_padded(buf + 3, 10, in.session);
    put_seq_field(buf + 13, 20, in.sequence);
    return SOUP_LOGIN_ACCEPTED_WIRE;
}

size_t soup_build_login_rejected(const SoupLoginRejected& in,
                                 unsigned char* buf) {
    be_put_u16(buf, static_cast<uint16_t>(1 + 1));
    buf[2] = 'J';
    buf[3] = static_cast<unsigned char>(in.reject_code);
    return SOUP_LOGIN_REJECTED_WIRE;
}

size_t soup_build_packet(char type, const unsigned char* payload,
                         size_t payload_len, unsigned char* buf) {
    be_put_u16(buf, static_cast<uint16_t>(1 + payload_len));
    buf[2] = static_cast<unsigned char>(type);
    if (payload_len > 0) {
        std::memcpy(buf + 3, payload, payload_len);
    }
    return 3 + payload_len;
}

void SoupFramer::feed(const unsigned char* data, size_t len) {
    buf_.insert(buf_.end(), data, data + len);
}

bool SoupFramer::next(SoupPacket& out) {
    for (;;) {
        if (buf_.size() < 2) {
            return false;
        }
        uint16_t plen = be_get_u16(&buf_[0]);
        if (plen == 0) {
            // Malformed: a packet must contain at least the type char.
            // Skip the bogus length prefix and keep scanning.
            ++bad_frames_;
            buf_.erase(buf_.begin(), buf_.begin() + 2);
            continue;
        }
        if (buf_.size() < static_cast<size_t>(2 + plen)) {
            return false;
        }
        out.type = static_cast<char>(buf_[2]);
        out.payload.assign(buf_.begin() + 3, buf_.begin() + 2 + plen);
        buf_.erase(buf_.begin(), buf_.begin() + 2 + plen);
        return true;
    }
}

} // namespace jnx
