// time.h — nanosecond timestamp helpers.
#ifndef JNX_TIME_H
#define JNX_TIME_H

#include <cstdint>
#include <ctime>

namespace jnx {

// Wall-clock time since the Unix epoch, in nanoseconds.
inline uint64_t now_ns() {
    struct timespec ts;
    ::clock_gettime(CLOCK_REALTIME, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL +
           static_cast<uint64_t>(ts.tv_nsec);
}

// Monotonic time (not tied to wall clock, immune to NTP jumps), in
// nanoseconds. Use for measuring durations / timeouts.
inline uint64_t mono_ns() {
    struct timespec ts;
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL +
           static_cast<uint64_t>(ts.tv_nsec);
}

} // namespace jnx

#endif // JNX_TIME_H
