// glimpse.cpp — see glimpse.h.
#include "fh/glimpse.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <cstring>

#include "common/log.h"
#include "common/time.h"
#include "itch/itch.h"
#include "soup/soup.h"

namespace jnx {

static const char* COMP = "glimpse";

bool glimpse_bootstrap(const std::string& host, int port,
                       const std::string& username,
                       const std::string& password, Market& market,
                       GlimpseResult& out, const char** err,
                       uint64_t timeout_ns) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        if (err) *err = "socket() failed";
        return false;
    }
    struct timeval tv;
    tv.tv_sec = 1;
    tv.tv_usec = 0;
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    tv.tv_sec = 5;
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        ::close(fd);
        if (err) *err = "bad glimpse host address";
        return false;
    }
    if (::connect(fd, reinterpret_cast<struct sockaddr*>(&addr),
                  sizeof(addr)) != 0) {
        ::close(fd);
        if (err) *err = "glimpse connect failed";
        return false;
    }

    // Login: blank requested session (spec requirement), seq 1.
    SoupLoginRequest req;
    std::snprintf(req.username, sizeof(req.username), "%s", username.c_str());
    std::snprintf(req.password, sizeof(req.password), "%s", password.c_str());
    req.requested_session[0] = '\0';
    req.requested_sequence = 1;
    unsigned char lbuf[SOUP_LOGIN_REQUEST_WIRE];
    size_t ln = soup_build_login_request(req, lbuf);
    if (::send(fd, lbuf, ln, MSG_NOSIGNAL) != static_cast<ssize_t>(ln)) {
        ::close(fd);
        if (err) *err = "glimpse login send failed";
        return false;
    }

    SoupFramer framer;
    unsigned char rbuf[65536];
    uint64_t deadline = mono_ns() + timeout_ns;
    uint64_t last_rx = mono_ns();
    uint64_t last_tx = mono_ns();

    for (;;) {
        if (mono_ns() > deadline) {
            ::close(fd);
            if (err) *err = "glimpse snapshot timeout";
            return false;
        }
        ssize_t n = ::recv(fd, rbuf, sizeof(rbuf), 0);
        if (n == 0) {
            ::close(fd);
            if (err) *err = "glimpse connection lost before end of snapshot";
            return false;
        }
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
                uint64_t now = mono_ns();
                if (now - last_rx > 15000000000ULL) {
                    ::close(fd);
                    if (err) *err = "glimpse peer silent for 15 s";
                    return false;
                }
                if (now - last_tx > 1000000000ULL) {
                    unsigned char hb[SOUP_BARE_WIRE];
                    size_t hn = soup_build_packet('R', NULL, 0, hb);
                    (void)::send(fd, hb, hn, MSG_NOSIGNAL);
                    last_tx = now;
                }
                continue;
            }
            ::close(fd);
            if (err) *err = "glimpse recv error";
            return false;
        }
        last_rx = mono_ns();
        framer.feed(rbuf, static_cast<size_t>(n));

        SoupPacket pkt;
        while (framer.next(pkt)) {
            const unsigned char* payload =
                pkt.payload.empty() ? NULL : &pkt.payload[0];
            if (pkt.type == 'J') {
                ::close(fd);
                if (err) *err = "glimpse login rejected";
                return false;
            }
            if (pkt.type == 'A') {
                SoupLoginAccepted la;
                const char* perr = NULL;
                if (soup_parse_login_accepted(payload, pkt.payload.size(),
                                              la, &perr)) {
                    out.session = la.session;
                    LOG_INFO(COMP) << "logged in, session='" << out.session
                                   << "', receiving snapshot";
                }
                continue;
            }
            if (pkt.type == 'Z') {
                ::close(fd);
                if (err) *err = "glimpse ended before end of snapshot";
                return false;
            }
            if (pkt.type != 'S') {
                continue; // heartbeat/debug
            }
            ItchMsg msg;
            const char* derr = NULL;
            if (!decode(payload, pkt.payload.size(), msg, &derr)) {
                LOG_WARN(COMP) << "undecodable snapshot message: "
                               << (derr ? derr : "?");
                continue;
            }
            if (msg.type == 'G') {
                out.next_live_seq = msg.sequence_number;
                LOG_INFO(COMP) << "end of snapshot: " << out.message_count
                               << " messages, next live seq "
                               << out.next_live_seq;
                // Clean logout, then done.
                unsigned char ob[SOUP_BARE_WIRE];
                size_t on = soup_build_packet('O', NULL, 0, ob);
                (void)::send(fd, ob, on, MSG_NOSIGNAL);
                ::close(fd);
                return true;
            }
            market.apply(msg);
            ++out.message_count;
        }
    }
}

} // namespace jnx
