// test_common.cpp — smoke tests for cpp/common/*: endian, cfg. minitest
// itself is exercised implicitly (this binary IS a minitest program); we
// keep the harness self-check to passing checks only, per F0 scope (a
// deliberately-failing check would make this binary's exit code nonzero
// and break `make -C cpp test`).
#include "common/cfg.h"
#include "common/endian.h"
#include "common/minitest.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

// ---------------------------------------------------------------------
// endian round-trips, including unaligned offsets and boundary values.
// ---------------------------------------------------------------------

TEST(endian_u16_roundtrip_unaligned) {
    unsigned char buf[16];
    std::memset(buf, 0xAA, sizeof(buf));
    uint16_t values[] = {0, 1, 0x00FF, 0xFF00, 0xFFFF, 0x1234};
    for (size_t off = 0; off < 3; ++off) {
        for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
            jnx::be_put_u16(buf + off, values[i]);
            uint16_t got = jnx::be_get_u16(buf + off);
            CHECK_EQ(got, values[i]);
        }
    }
}

TEST(endian_u32_roundtrip_unaligned) {
    unsigned char buf[16];
    std::memset(buf, 0x55, sizeof(buf));
    uint32_t values[] = {0, 1, 0x7FFFFFFFu, 0x80000000u, 0xFFFFFFFFu,
                          0x12345678u};
    for (size_t off = 0; off < 5; ++off) {
        for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
            jnx::be_put_u32(buf + off, values[i]);
            uint32_t got = jnx::be_get_u32(buf + off);
            CHECK_EQ(got, values[i]);
        }
    }
}

TEST(endian_u64_roundtrip_unaligned) {
    unsigned char buf[24];
    std::memset(buf, 0x33, sizeof(buf));
    uint64_t values[] = {0ULL,
                          1ULL,
                          0x7FFFFFFFULL,
                          0xFFFFFFFFULL,
                          0x7FFFFFFFFFFFFFFFULL,
                          0xFFFFFFFFFFFFFFFFULL,
                          0x0123456789ABCDEFULL};
    for (size_t off = 0; off < 7; ++off) {
        for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
            jnx::be_put_u64(buf + off, values[i]);
            uint64_t got = jnx::be_get_u64(buf + off);
            CHECK_EQ(got, values[i]);
        }
    }
}

TEST(endian_u32_matches_manual_bytes) {
    // Sanity check the byte order explicitly (big-endian: MSB first).
    unsigned char buf[4];
    jnx::be_put_u32(buf, 0x01020304u);
    CHECK_EQ(static_cast<int>(buf[0]), 0x01);
    CHECK_EQ(static_cast<int>(buf[1]), 0x02);
    CHECK_EQ(static_cast<int>(buf[2]), 0x03);
    CHECK_EQ(static_cast<int>(buf[3]), 0x04);
}

// ---------------------------------------------------------------------
// cfg: file parsing (comments, blanks, whitespace) + argv override.
// ---------------------------------------------------------------------

TEST(cfg_missing_file_is_not_fatal) {
    jnx::Cfg cfg;
    bool ok = cfg.load_file("/nonexistent/path/does/not/exist.cfg");
    CHECK(!ok);
    // Defaults still work.
    CHECK_EQ(cfg.get("missing_key", "default"), std::string("default"));
    CHECK_EQ(cfg.get_int("missing_int", 42), 42L);
}

TEST(cfg_parses_file_with_comments_and_blanks) {
    const char* path = "/tmp/jnx_test_common_cfg.tmp";
    std::FILE* f = std::fopen(path, "w");
    CHECK(f != NULL);
    if (f == NULL) {
        return;
    }
    std::fputs("# a comment\n", f);
    std::fputs("\n", f);
    std::fputs("  \n", f);
    std::fputs("host = 127.0.0.1\n", f);
    std::fputs("port=15001\n", f);
    std::fputs("   # indented comment\n", f);
    std::fputs("session = ABCDEFGHIJ  \n", f);
    std::fputs("unknown_key=whatever\n", f);
    std::fclose(f);

    jnx::Cfg cfg;
    bool ok = cfg.load_file(path);
    CHECK(ok);
    CHECK_EQ(cfg.get("host", ""), std::string("127.0.0.1"));
    CHECK_EQ(cfg.get_int("port", 0), 15001L);
    CHECK_EQ(cfg.get("session", ""), std::string("ABCDEFGHIJ"));
    CHECK_EQ(cfg.get("unknown_key", ""), std::string("whatever"));
    CHECK_EQ(cfg.get("does_not_exist", "fallback"), std::string("fallback"));

    std::remove(path);
}

TEST(cfg_argv_override_wins_over_file) {
    const char* path = "/tmp/jnx_test_common_cfg2.tmp";
    std::FILE* f = std::fopen(path, "w");
    CHECK(f != NULL);
    if (f == NULL) {
        return;
    }
    std::fputs("port=15001\n", f);
    std::fputs("host=10.0.0.1\n", f);
    std::fclose(f);

    jnx::Cfg cfg;
    cfg.load_file(path);
    CHECK_EQ(cfg.get_int("port", 0), 15001L);

    char arg0[] = "progname";
    char arg1[] = "--port=26401";
    char arg2[] = "--session=NEWSESSION";
    char* argv[] = {arg0, arg1, arg2};
    cfg.apply_args(3, argv);

    CHECK_EQ(cfg.get_int("port", 0), 26401L);
    CHECK_EQ(cfg.get("host", ""), std::string("10.0.0.1")); // untouched
    CHECK_EQ(cfg.get("session", ""), std::string("NEWSESSION"));

    std::remove(path);
}

TEST(cfg_get_int_defaults_on_non_numeric) {
    jnx::Cfg cfg;
    char arg0[] = "progname";
    char arg1[] = "--foo=not_a_number";
    char* argv[] = {arg0, arg1};
    cfg.apply_args(2, argv);
    CHECK_EQ(cfg.get_int("foo", 7), 7L);
}
