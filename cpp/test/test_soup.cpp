// test_soup.cpp — SoupBinTCP codec + framer vs the committed golden
// vectors (cpp/test/vectors/soup.json), including byte-at-a-time and
// split-length-prefix framing.
#include "soup/soup.h"

#include <cstring>
#include <string>
#include <vector>

#include "common/minitest.h"
#include "test/jsonvec.h"

namespace {

std::string vectors_path() {
    std::ifstream probe("test/vectors/soup.json");
    if (probe.is_open()) {
        return "test/vectors/soup.json";
    }
    return "cpp/test/vectors/soup.json";
}

bool bytes_equal(const std::vector<unsigned char>& a,
                 const std::vector<unsigned char>& b) {
    return a.size() == b.size() &&
           (a.empty() || std::memcmp(&a[0], &b[0], a.size()) == 0);
}

// Decode one packet's fields and compare against the vector; encode from
// the vector's fields and compare against the golden bytes.
void check_packet_against_vector(const jsonvec::Vector& v,
                                 const jnx::SoupPacket& pkt,
                                 const std::vector<unsigned char>& wire,
                                 int& jnx_failures) {
    CHECK_EQ(std::string(1, pkt.type), v.type);

    const char* err = NULL;
    unsigned char out[128];
    const unsigned char* payload = pkt.payload.empty() ? NULL : &pkt.payload[0];

    if (v.type == "L") {
        jnx::SoupLoginRequest lr;
        CHECK(jnx::soup_parse_login_request(payload, pkt.payload.size(), lr,
                                            &err));
        CHECK_EQ(std::string(lr.username), v.str_fields.at("username"));
        CHECK_EQ(std::string(lr.password), v.str_fields.at("password"));
        CHECK_EQ(std::string(lr.requested_session),
                 v.str_fields.at("requested_session"));
        CHECK_EQ(lr.requested_sequence, v.int_fields.at("requested_sequence"));
        size_t n = jnx::soup_build_login_request(lr, out);
        CHECK_EQ(n, wire.size());
        CHECK(n == wire.size() && std::memcmp(out, &wire[0], n) == 0);
    } else if (v.type == "A") {
        jnx::SoupLoginAccepted la;
        CHECK(jnx::soup_parse_login_accepted(payload, pkt.payload.size(), la,
                                             &err));
        CHECK_EQ(std::string(la.session), v.str_fields.at("session"));
        CHECK_EQ(la.sequence, v.int_fields.at("sequence"));
        size_t n = jnx::soup_build_login_accepted(la, out);
        CHECK_EQ(n, wire.size());
        CHECK(n == wire.size() && std::memcmp(out, &wire[0], n) == 0);
    } else if (v.type == "J") {
        jnx::SoupLoginRejected rj;
        CHECK(jnx::soup_parse_login_rejected(payload, pkt.payload.size(), rj,
                                             &err));
        CHECK_EQ(std::string(1, rj.reject_code),
                 v.str_fields.at("reject_code"));
        size_t n = jnx::soup_build_login_rejected(rj, out);
        CHECK_EQ(n, wire.size());
        CHECK(n == wire.size() && std::memcmp(out, &wire[0], n) == 0);
    } else {
        // Payload-carrying (S/+/U) or bare (H/Z/R/O) packets: compare the
        // payload against the vector's hex field (if any), then rebuild.
        std::string key;
        if (v.has_str("payload_hex")) key = "payload_hex";
        else if (v.has_str("message_hex")) key = "message_hex";
        std::vector<unsigned char> want_payload;
        if (!key.empty()) {
            CHECK(jsonvec::hex_to_bytes(v.str_fields.at(key), want_payload));
        }
        CHECK(bytes_equal(pkt.payload, want_payload));
        size_t n = jnx::soup_build_packet(pkt.type, payload,
                                          pkt.payload.size(), out);
        CHECK_EQ(n, wire.size());
        CHECK(n == wire.size() && std::memcmp(out, &wire[0], n) == 0);
    }
}

} // namespace

TEST(soup_golden_vectors_whole_packet) {
    std::vector<jsonvec::Vector> vecs;
    CHECK(jsonvec::load(vectors_path(), vecs));
    CHECK_EQ(vecs.size(), static_cast<size_t>(16));
    for (size_t i = 0; i < vecs.size(); ++i) {
        const jsonvec::Vector& v = vecs[i];
        std::vector<unsigned char> wire;
        CHECK(jsonvec::hex_to_bytes(v.hex, wire));

        jnx::SoupFramer framer;
        framer.feed(&wire[0], wire.size());
        jnx::SoupPacket pkt;
        bool got = framer.next(pkt);
        if (!got) {
            std::fprintf(stderr, "  vector %s: framer produced no packet\n",
                         v.name.c_str());
        }
        CHECK(got);
        if (!got) continue;
        check_packet_against_vector(v, pkt, wire, jnx_failures);
        // Nothing left over.
        CHECK(!framer.next(pkt));
        CHECK_EQ(framer.bad_frames(), static_cast<uint64_t>(0));
    }
}

TEST(soup_golden_vectors_byte_at_a_time) {
    std::vector<jsonvec::Vector> vecs;
    CHECK(jsonvec::load(vectors_path(), vecs));
    for (size_t i = 0; i < vecs.size(); ++i) {
        const jsonvec::Vector& v = vecs[i];
        std::vector<unsigned char> wire;
        CHECK(jsonvec::hex_to_bytes(v.hex, wire));

        jnx::SoupFramer framer;
        jnx::SoupPacket pkt;
        // Until the last byte arrives, no packet may be produced.
        for (size_t j = 0; j + 1 < wire.size(); ++j) {
            framer.feed(&wire[j], 1);
            CHECK(!framer.next(pkt));
        }
        framer.feed(&wire[wire.size() - 1], 1);
        bool got = framer.next(pkt);
        CHECK(got);
        if (got) {
            check_packet_against_vector(v, pkt, wire, jnx_failures);
        }
    }
}

TEST(soup_split_length_prefix_and_back_to_back) {
    // Two packets concatenated, delivered with the second packet's length
    // prefix split across feeds.
    std::vector<jsonvec::Vector> vecs;
    CHECK(jsonvec::load(vectors_path(), vecs));
    // Use the first two vectors (login request + typical login request).
    std::vector<unsigned char> w1, w2;
    CHECK(jsonvec::hex_to_bytes(vecs[0].hex, w1));
    CHECK(jsonvec::hex_to_bytes(vecs[1].hex, w2));

    std::vector<unsigned char> stream(w1);
    stream.insert(stream.end(), w2.begin(), w2.end());

    jnx::SoupFramer framer;
    // Feed everything up to and including the FIRST byte of packet 2's
    // length prefix, then the rest.
    size_t cut = w1.size() + 1;
    framer.feed(&stream[0], cut);
    jnx::SoupPacket p1;
    CHECK(framer.next(p1));
    check_packet_against_vector(vecs[0], p1, w1, jnx_failures);
    jnx::SoupPacket p2;
    CHECK(!framer.next(p2)); // second packet incomplete (half a prefix)
    framer.feed(&stream[cut], stream.size() - cut);
    CHECK(framer.next(p2));
    check_packet_against_vector(vecs[1], p2, w2, jnx_failures);
    CHECK(!framer.next(p2));
}

TEST(soup_zero_length_frame_skipped) {
    // A zero length prefix is malformed (a packet has at least the type
    // char); the framer must count and skip it, then resume.
    unsigned char bogus[2] = {0x00, 0x00};
    unsigned char hb[3] = {0x00, 0x01, 'H'};
    jnx::SoupFramer framer;
    framer.feed(bogus, 2);
    framer.feed(hb, 3);
    jnx::SoupPacket pkt;
    CHECK(framer.next(pkt));
    CHECK_EQ(std::string(1, pkt.type), std::string("H"));
    CHECK_EQ(pkt.payload.size(), static_cast<size_t>(0));
    CHECK_EQ(framer.bad_frames(), static_cast<uint64_t>(1));
}
