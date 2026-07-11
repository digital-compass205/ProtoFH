// procstat.h — tiny /proc/self/status reader for operator-visible RSS.
// Linux-only (fine — both dev and target are Linux). No allocation on the
// hot path matters here: this is only called on the 5 s stats timer, not
// per-message.
#ifndef JNX_PROCSTAT_H
#define JNX_PROCSTAT_H

#include <cstdio>
#include <cstring>

namespace jnx {

// Returns current process resident set size in kilobytes, or 0 if
// /proc/self/status could not be read (e.g. non-Linux — never happens on
// our two target OSes, but fail soft rather than crash a stats line).
inline long rss_kb() {
    std::FILE* f = std::fopen("/proc/self/status", "r");
    if (f == NULL) {
        return 0;
    }
    long kb = 0;
    char line[256];
    while (std::fgets(line, sizeof(line), f) != NULL) {
        if (std::strncmp(line, "VmRSS:", 6) == 0) {
            std::sscanf(line + 6, "%ld", &kb);
            break;
        }
    }
    std::fclose(f);
    return kb;
}

} // namespace jnx

#endif // JNX_PROCSTAT_H
