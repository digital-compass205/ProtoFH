// minitest.h — header-only C++11 micro test harness. No external deps.
//
// Usage: a test_*.cpp includes this header and writes:
//
//   #include "common/minitest.h"
//   TEST(some_name) {
//       CHECK(1 + 1 == 2);
//       CHECK_EQ(1 + 1, 2);
//   }
//
// The header supplies main(): it runs every registered test, prints one
// PASS/FAIL line per test plus a summary, and returns the number of
// failing tests as the process exit code (0 = all green).
#ifndef JNX_MINITEST_H
#define JNX_MINITEST_H

#include <cstdio>
#include <sstream>
#include <string>
#include <vector>

namespace minitest {

struct TestCase {
    const char* name;
    void (*fn)(int& failures);
};

// Function-local static registry: avoids static-init-order fiasco since
// the vector is constructed on first use (first REGISTRAR construction),
// not at an unspecified point relative to other globals.
inline std::vector<TestCase>& registry() {
    static std::vector<TestCase> tests;
    return tests;
}

struct Registrar {
    Registrar(const char* name, void (*fn)(int& failures)) {
        TestCase tc;
        tc.name = name;
        tc.fn = fn;
        registry().push_back(tc);
    }
};

// Streams two values via <sstream> so CHECK_EQ can print both operands
// on failure without requiring operator<< overload discovery tricks.
template <typename T>
inline std::string to_str(const T& v) {
    std::ostringstream oss;
    oss << v;
    return oss.str();
}

} // namespace minitest

#define TEST(name)                                                          \
    static void jnx_test_##name(int& jnx_failures);                         \
    static ::minitest::Registrar jnx_registrar_##name(#name, jnx_test_##name); \
    static void jnx_test_##name(int& jnx_failures)

#define CHECK(expr)                                                         \
    do {                                                                    \
        if (!(expr)) {                                                      \
            std::fprintf(stderr, "  CHECK failed at %s:%d: %s\n", __FILE__, \
                         __LINE__, #expr);                                  \
            ++jnx_failures;                                                 \
        }                                                                   \
    } while (0)

#define CHECK_EQ(a, b)                                                      \
    do {                                                                    \
        if (!((a) == (b))) {                                                \
            std::fprintf(stderr,                                           \
                         "  CHECK_EQ failed at %s:%d: %s == %s "            \
                         "(lhs=%s rhs=%s)\n",                                \
                         __FILE__, __LINE__, #a, #b,                        \
                         ::minitest::to_str(a).c_str(),                     \
                         ::minitest::to_str(b).c_str());                    \
            ++jnx_failures;                                                 \
        }                                                                   \
    } while (0)

int main() {
    int total_failures = 0;
    int ntests = 0;
    for (std::vector<minitest::TestCase>::const_iterator it =
             minitest::registry().begin();
         it != minitest::registry().end(); ++it) {
        int failures = 0;
        it->fn(failures);
        ++ntests;
        if (failures == 0) {
            std::fprintf(stderr, "PASS %s\n", it->name);
        } else {
            std::fprintf(stderr, "FAIL %s (%d check(s) failed)\n", it->name,
                         failures);
        }
        total_failures += failures;
    }
    std::fprintf(stderr, "--- %d test(s), %d failure(s) ---\n", ntests,
                 total_failures);
    return total_failures;
}

#endif // JNX_MINITEST_H
