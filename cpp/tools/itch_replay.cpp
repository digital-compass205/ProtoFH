// itch_replay — decode every message in an ITCH Binary Data File
// (JNX_PLAN.md §3.7: repeated `length:2:BE + ITCH message`) and print one
// summary line:
//   total=N  A=.. E=.. ...  errors=K
// Per-type counts are printed for types seen, in descending count order
// (ties broken by type char). On the first decode error, the file offset
// and reason are printed to stderr. Exit code: 0 if errors==0, else 1.
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <string>
#include <vector>

#include "itch/itch.h"

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: itch_replay <file.itch>\n");
        return 2;
    }
    std::FILE* f = std::fopen(argv[1], "rb");
    if (f == NULL) {
        std::fprintf(stderr, "itch_replay: cannot open %s\n", argv[1]);
        return 2;
    }

    uint64_t total = 0;
    uint64_t errors = 0;
    uint64_t counts[256];
    std::memset(counts, 0, sizeof(counts));
    bool first_error_reported = false;
    unsigned long long offset = 0;
    unsigned char lenbuf[2];
    unsigned char msgbuf[65536];

    for (;;) {
        size_t got = std::fread(lenbuf, 1, 2, f);
        if (got == 0) {
            break; // clean EOF
        }
        if (got != 2) {
            std::fprintf(stderr,
                         "itch_replay: truncated length prefix at offset "
                         "%llu\n",
                         offset);
            ++errors;
            break;
        }
        size_t mlen = (static_cast<size_t>(lenbuf[0]) << 8) |
                      static_cast<size_t>(lenbuf[1]);
        offset += 2;
        if (mlen == 0) {
            std::fprintf(stderr,
                         "itch_replay: zero-length message at offset %llu\n",
                         offset);
            ++errors;
            break;
        }
        if (std::fread(msgbuf, 1, mlen, f) != mlen) {
            std::fprintf(stderr,
                         "itch_replay: truncated message at offset %llu\n",
                         offset);
            ++errors;
            break;
        }
        ++total;
        jnx::ItchMsg msg;
        const char* err = NULL;
        if (jnx::decode(msgbuf, mlen, msg, &err)) {
            ++counts[static_cast<unsigned char>(msg.type)];
        } else {
            ++errors;
            if (!first_error_reported) {
                first_error_reported = true;
                std::fprintf(stderr,
                             "itch_replay: first decode error at offset "
                             "%llu (type 0x%02x len %zu): %s\n",
                             offset, msgbuf[0], mlen, err ? err : "?");
            }
        }
        offset += mlen;
    }
    std::fclose(f);

    // Collect seen types, descending count (ties by type char).
    std::vector<std::pair<uint64_t, char> > seen;
    for (int c = 0; c < 256; ++c) {
        if (counts[c] > 0) {
            seen.push_back(std::make_pair(counts[c], static_cast<char>(c)));
        }
    }
    struct ByCountDesc {
        bool operator()(const std::pair<uint64_t, char>& a,
                        const std::pair<uint64_t, char>& b) const {
            if (a.first != b.first) return a.first > b.first;
            return a.second < b.second;
        }
    };
    std::sort(seen.begin(), seen.end(), ByCountDesc());

    std::printf("total=%llu ", static_cast<unsigned long long>(total));
    for (size_t i = 0; i < seen.size(); ++i) {
        std::printf(" %c=%llu", seen[i].second,
                    static_cast<unsigned long long>(seen[i].first));
    }
    std::printf("  errors=%llu\n", static_cast<unsigned long long>(errors));
    return errors == 0 ? 0 : 1;
}
