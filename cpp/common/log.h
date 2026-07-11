// log.h — header-only logging to stderr in the project-wide format:
//   YYYY-MM-DDTHH:MM:SS.mmm LEVEL component: message
//
// Level threshold is read once from env var JNX_LOG (DEBUG/INFO/WARN/ERROR,
// default INFO). Usage:
//
//   LOG_INFO("fh") << "connected to " << host << ":" << port;
//
// The macro expands to a statement that streams into a temporary; the
// message is fully built with operator<< before being flushed as one line,
// so multi-threaded logging doesn't interleave partial lines (each line is
// written with a single fwrite-backed ostream::flush at the end).
#ifndef JNX_LOG_H
#define JNX_LOG_H

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <sstream>
#include <string>

#include "time.h"

namespace jnx {
namespace log {

enum Level { LVL_DEBUG = 0, LVL_INFO = 1, LVL_WARN = 2, LVL_ERROR = 3 };

inline const char* level_name(Level lvl) {
    switch (lvl) {
        case LVL_DEBUG: return "DEBUG";
        case LVL_INFO: return "INFO";
        case LVL_WARN: return "WARN";
        case LVL_ERROR: return "ERROR";
    }
    return "?";
}

inline Level parse_level(const char* s) {
    if (s == NULL) return LVL_INFO;
    if (std::strcmp(s, "DEBUG") == 0) return LVL_DEBUG;
    if (std::strcmp(s, "INFO") == 0) return LVL_INFO;
    if (std::strcmp(s, "WARN") == 0) return LVL_WARN;
    if (std::strcmp(s, "ERROR") == 0) return LVL_ERROR;
    return LVL_INFO;
}

inline Level threshold() {
    static const Level lvl = parse_level(std::getenv("JNX_LOG"));
    return lvl;
}

inline bool enabled(Level lvl) {
    return lvl >= threshold();
}

// Formats the current wall-clock time as YYYY-MM-DDTHH:MM:SS.mmm.
inline std::string timestamp_now() {
    uint64_t ns = ::jnx::now_ns();
    std::time_t secs = static_cast<std::time_t>(ns / 1000000000ULL);
    unsigned long ms = static_cast<unsigned long>((ns / 1000000ULL) % 1000ULL);
    std::tm tmv;
    ::gmtime_r(&secs, &tmv);
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d.%03lu",
                  tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
                  tmv.tm_hour, tmv.tm_min, tmv.tm_sec, ms);
    return std::string(buf);
}

// RAII helper: accumulates a message via operator<< and writes one
// complete line to stderr on destruction.
class LineLogger {
public:
    LineLogger(Level lvl, const char* component) : lvl_(lvl) {
        oss_ << timestamp_now() << ' ' << level_name(lvl_) << ' ' << component
             << ": ";
    }
    ~LineLogger() {
        oss_ << '\n';
        std::string s = oss_.str();
        std::fwrite(s.data(), 1, s.size(), stderr);
    }
    template <typename T>
    LineLogger& operator<<(const T& v) {
        oss_ << v;
        return *this;
    }

private:
    Level lvl_;
    std::ostringstream oss_;
};

// Discards everything streamed into it; used when a level is disabled so
// callers still pay only for cheap formatting of a no-op.
class NullLogger {
public:
    template <typename T>
    NullLogger& operator<<(const T&) {
        return *this;
    }
};

} // namespace log
} // namespace jnx

#define LOG_DEBUG(component)                                                \
    if (!::jnx::log::enabled(::jnx::log::LVL_DEBUG)) {                      \
    } else                                                                  \
        ::jnx::log::LineLogger(::jnx::log::LVL_DEBUG, component)

#define LOG_INFO(component)                                                 \
    if (!::jnx::log::enabled(::jnx::log::LVL_INFO)) {                       \
    } else                                                                  \
        ::jnx::log::LineLogger(::jnx::log::LVL_INFO, component)

#define LOG_WARN(component)                                                 \
    if (!::jnx::log::enabled(::jnx::log::LVL_WARN)) {                       \
    } else                                                                  \
        ::jnx::log::LineLogger(::jnx::log::LVL_WARN, component)

#define LOG_ERROR(component)                                                \
    if (!::jnx::log::enabled(::jnx::log::LVL_ERROR)) {                      \
    } else                                                                  \
        ::jnx::log::LineLogger(::jnx::log::LVL_ERROR, component)

#endif // JNX_LOG_H
