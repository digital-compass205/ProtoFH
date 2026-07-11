// test_soupclient.cpp — sans-I/O SoupClientSession state machine against
// scripted byte sequences (no sockets).
#include "fh/soupclient.h"

#include <cstring>
#include <string>
#include <vector>

#include "common/minitest.h"
#include "soup/soup.h"

namespace {

const uint64_t SEC = 1000000000ULL;

std::vector<unsigned char> login_accepted(const char* session,
                                          uint64_t seq) {
    jnx::SoupLoginAccepted la;
    std::snprintf(la.session, sizeof(la.session), "%s", session);
    la.sequence = seq;
    unsigned char buf[jnx::SOUP_LOGIN_ACCEPTED_WIRE];
    size_t n = jnx::soup_build_login_accepted(la, buf);
    return std::vector<unsigned char>(buf, buf + n);
}

std::vector<unsigned char> sequenced(const std::string& payload) {
    unsigned char buf[128];
    size_t n = jnx::soup_build_packet(
        'S', reinterpret_cast<const unsigned char*>(payload.data()),
        payload.size(), buf);
    return std::vector<unsigned char>(buf, buf + n);
}

std::vector<unsigned char> bare(char type) {
    unsigned char buf[jnx::SOUP_BARE_WIRE];
    size_t n = jnx::soup_build_packet(type, NULL, 0, buf);
    return std::vector<unsigned char>(buf, buf + n);
}

// Count Soup packets of a given type in a raw byte stream.
int count_packets(const std::vector<unsigned char>& bytes, char type) {
    jnx::SoupFramer framer;
    if (!bytes.empty()) {
        framer.feed(&bytes[0], bytes.size());
    }
    jnx::SoupPacket pkt;
    int n = 0;
    while (framer.next(pkt)) {
        if (pkt.type == type) ++n;
    }
    return n;
}

} // namespace

TEST(soupclient_login_flow_and_self_counted_seq) {
    jnx::SoupClientSession s("USER", "PASS", "", 1);
    std::string got_session;
    uint64_t got_seq = 0;
    std::vector<std::pair<uint64_t, std::string> > msgs;
    s.on_login_accepted = [&](const std::string& sid, uint64_t seq) {
        got_session = sid;
        got_seq = seq;
    };
    s.on_message = [&](uint64_t seq, const unsigned char* p, size_t n) {
        msgs.push_back(std::make_pair(
            seq, std::string(reinterpret_cast<const char*>(p), n)));
    };

    s.start(0);
    std::vector<unsigned char> out;
    CHECK(s.take_output(out));
    // Exactly one LoginRequest queued.
    CHECK_EQ(count_packets(out, 'L'), 1);
    CHECK_EQ(static_cast<int>(s.state()),
             static_cast<int>(jnx::SoupClientSession::ST_LOGIN_SENT));

    std::vector<unsigned char> la = login_accepted("SIM0000001", 12563);
    s.on_bytes(&la[0], la.size(), 1 * SEC);
    CHECK_EQ(static_cast<int>(s.state()),
             static_cast<int>(jnx::SoupClientSession::ST_LIVE));
    CHECK_EQ(got_session, std::string("SIM0000001"));
    CHECK_EQ(got_seq, static_cast<uint64_t>(12563));

    // Two sequenced messages: self-counted 12563, 12564.
    std::vector<unsigned char> d1 = sequenced("hello");
    std::vector<unsigned char> d2 = sequenced("world");
    s.on_bytes(&d1[0], d1.size(), 2 * SEC);
    s.on_bytes(&d2[0], d2.size(), 2 * SEC);
    CHECK_EQ(msgs.size(), static_cast<size_t>(2));
    CHECK_EQ(msgs[0].first, static_cast<uint64_t>(12563));
    CHECK_EQ(msgs[0].second, std::string("hello"));
    CHECK_EQ(msgs[1].first, static_cast<uint64_t>(12564));
    CHECK_EQ(s.next_seq(), static_cast<uint64_t>(12565));
}

TEST(soupclient_login_rejected_terminal) {
    jnx::SoupClientSession s("USER", "PASS", "", 1);
    char code = '\0';
    s.on_login_rejected = [&](char c) { code = c; };
    s.start(0);
    std::vector<unsigned char> out;
    s.take_output(out);

    jnx::SoupLoginRejected rj;
    rj.reject_code = 'A';
    unsigned char buf[jnx::SOUP_LOGIN_REJECTED_WIRE];
    size_t n = jnx::soup_build_login_rejected(rj, buf);
    s.on_bytes(buf, n, 1 * SEC);
    CHECK_EQ(static_cast<int>(s.state()),
             static_cast<int>(jnx::SoupClientSession::ST_FAILED));
    CHECK_EQ(std::string(1, code), std::string("A"));
    CHECK_EQ(std::string(1, s.reject_code()), std::string("A"));
}

TEST(soupclient_heartbeat_after_1s_idle) {
    jnx::SoupClientSession s("USER", "PASS", "", 1);
    s.start(10 * SEC);
    std::vector<unsigned char> out;
    s.take_output(out); // drain the login request
    out.clear();

    // Under 1 s outbound idle: no heartbeat.
    s.on_tick(10 * SEC + SEC / 2);
    CHECK(!s.has_output());
    // Over 1 s: exactly one client heartbeat 'R'.
    s.on_tick(11 * SEC + 1);
    CHECK(s.take_output(out));
    CHECK_EQ(count_packets(out, 'R'), 1);
    // Timer was refreshed by the heartbeat itself.
    out.clear();
    s.on_tick(11 * SEC + SEC / 2);
    CHECK(!s.has_output());
}

TEST(soupclient_peer_silent_after_15s) {
    jnx::SoupClientSession s("USER", "PASS", "", 1);
    int silent = 0;
    s.on_peer_silent = [&]() { ++silent; };
    s.start(0);
    std::vector<unsigned char> out;
    s.take_output(out);

    std::vector<unsigned char> la = login_accepted("X", 1);
    s.on_bytes(&la[0], la.size(), 1 * SEC);

    s.on_tick(15 * SEC); // 14 s inbound silence: fine
    CHECK_EQ(silent, 0);
    s.on_tick(16 * SEC + 1); // >15 s: report
    CHECK_EQ(silent, 1);

    // Any inbound byte (e.g. a server heartbeat) resets the clock.
    std::vector<unsigned char> hb = bare('H');
    s.on_bytes(&hb[0], hb.size(), 17 * SEC);
    s.on_tick(18 * SEC);
    CHECK_EQ(silent, 1);
}

TEST(soupclient_end_of_session) {
    jnx::SoupClientSession s("USER", "PASS", "", 1);
    int ended = 0;
    s.on_end_of_session = [&]() { ++ended; };
    s.start(0);
    std::vector<unsigned char> out;
    s.take_output(out);
    std::vector<unsigned char> la = login_accepted("X", 5);
    s.on_bytes(&la[0], la.size(), 1 * SEC);
    std::vector<unsigned char> z = bare('Z');
    s.on_bytes(&z[0], z.size(), 2 * SEC);
    CHECK_EQ(ended, 1);
    CHECK_EQ(static_cast<int>(s.state()),
             static_cast<int>(jnx::SoupClientSession::ST_ENDED));
}

TEST(soupclient_reset_keeps_resume_point) {
    jnx::SoupClientSession s("USER", "PASS", "", 1);
    s.start(0);
    std::vector<unsigned char> out;
    s.take_output(out);
    std::vector<unsigned char> la = login_accepted("SESSA", 100);
    s.on_bytes(&la[0], la.size(), 1 * SEC);
    std::vector<unsigned char> d = sequenced("x");
    s.on_bytes(&d[0], d.size(), 2 * SEC); // consumed seq 100 -> next 101

    // Simulated TCP drop: reset, re-login must request SESSA @ 101.
    s.reset();
    CHECK_EQ(static_cast<int>(s.state()),
             static_cast<int>(jnx::SoupClientSession::ST_CONNECTED));
    s.start(3 * SEC);
    out.clear();
    CHECK(s.take_output(out));
    jnx::SoupFramer framer;
    framer.feed(&out[0], out.size());
    jnx::SoupPacket pkt;
    CHECK(framer.next(pkt));
    CHECK_EQ(std::string(1, pkt.type), std::string("L"));
    jnx::SoupLoginRequest req;
    const char* err = NULL;
    CHECK(jnx::soup_parse_login_request(&pkt.payload[0], pkt.payload.size(),
                                        req, &err));
    CHECK_EQ(std::string(req.requested_session), std::string("SESSA"));
    CHECK_EQ(req.requested_sequence, static_cast<uint64_t>(101));
}
