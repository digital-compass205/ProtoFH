// endian.h — big-endian read/write helpers, byte-wise only.
//
// Wire formats (ITCH and our own record codec) are big-endian; the dev and
// target machines are both little-endian x86-64. These helpers NEVER use
// pointer casts or memcpy-of-struct — always explicit shifts/masks — so
// they are correct at unaligned offsets and free of strict-aliasing UB.
#ifndef JNX_ENDIAN_H
#define JNX_ENDIAN_H

#include <cstdint>

namespace jnx {

inline uint16_t be_get_u16(const unsigned char* p) {
    return static_cast<uint16_t>(
        (static_cast<uint16_t>(p[0]) << 8) |
        (static_cast<uint16_t>(p[1])));
}

inline uint32_t be_get_u32(const unsigned char* p) {
    return (static_cast<uint32_t>(p[0]) << 24) |
           (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8) |
           (static_cast<uint32_t>(p[3]));
}

inline uint64_t be_get_u64(const unsigned char* p) {
    return (static_cast<uint64_t>(p[0]) << 56) |
           (static_cast<uint64_t>(p[1]) << 48) |
           (static_cast<uint64_t>(p[2]) << 40) |
           (static_cast<uint64_t>(p[3]) << 32) |
           (static_cast<uint64_t>(p[4]) << 24) |
           (static_cast<uint64_t>(p[5]) << 16) |
           (static_cast<uint64_t>(p[6]) << 8) |
           (static_cast<uint64_t>(p[7]));
}

inline void be_put_u16(unsigned char* p, uint16_t v) {
    p[0] = static_cast<unsigned char>((v >> 8) & 0xFF);
    p[1] = static_cast<unsigned char>(v & 0xFF);
}

inline void be_put_u32(unsigned char* p, uint32_t v) {
    p[0] = static_cast<unsigned char>((v >> 24) & 0xFF);
    p[1] = static_cast<unsigned char>((v >> 16) & 0xFF);
    p[2] = static_cast<unsigned char>((v >> 8) & 0xFF);
    p[3] = static_cast<unsigned char>(v & 0xFF);
}

inline void be_put_u64(unsigned char* p, uint64_t v) {
    p[0] = static_cast<unsigned char>((v >> 56) & 0xFF);
    p[1] = static_cast<unsigned char>((v >> 48) & 0xFF);
    p[2] = static_cast<unsigned char>((v >> 40) & 0xFF);
    p[3] = static_cast<unsigned char>((v >> 32) & 0xFF);
    p[4] = static_cast<unsigned char>((v >> 24) & 0xFF);
    p[5] = static_cast<unsigned char>((v >> 16) & 0xFF);
    p[6] = static_cast<unsigned char>((v >> 8) & 0xFF);
    p[7] = static_cast<unsigned char>(v & 0xFF);
}

} // namespace jnx

#endif // JNX_ENDIAN_H
