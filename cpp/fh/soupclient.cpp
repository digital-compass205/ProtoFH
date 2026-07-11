// soupclient.cpp — see soupclient.h.
#include "fh/soupclient.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstring>

#include "common/log.h"
#include "common/time.h"

namespace jnx {

static const char* COMP = "soup";

// --- SoupClientSession -----------------------------------------------------

SoupClientSession::SoupClientSession(const std::string& username,
                                     const std::string& password,
                                     const std::string& requested_session,
                                     uint64_t requested_seq)
    : username_(username),
      password_(password),
      session_id_(requested_session),
      next_seq_(requested_seq),
      state_(ST_CONNECTED),
      reject_code_('\0'),
      last_sent_ns_(0),
      last_received_ns_(0) {}

void SoupClientSession::reset() {
    framer_ = SoupFramer();
    out_.clear();
    state_ = ST_CONNECTED;
    reject_code_ = '\0';
    last_sent_ns_ = 0;
    last_received_ns_ = 0;
}

void SoupClientSession::set_resume(const std::string& session_id,
                                   uint64_t next_seq) {
    session_id_ = session_id;
    next_seq_ = next_seq;
}

void SoupClientSession::start(uint64_t now_ns) {
    SoupLoginRequest req;
    std::snprintf(req.username, sizeof(req.username), "%s",
                  username_.c_str());
    std::snprintf(req.password, sizeof(req.password), "%s",
                  password_.c_str());
    std::snprintf(req.requested_session, sizeof(req.requested_session), "%s",
                  session_id_.c_str());
    req.requested_sequence = next_seq_;
    unsigned char buf[SOUP_LOGIN_REQUEST_WIRE];
    size_t n = soup_build_login_request(req, buf);
    out_.insert(out_.end(), buf, buf + n);
    last_sent_ns_ = now_ns;
    state_ = ST_LOGIN_SENT;
}

void SoupClientSession::logout(uint64_t now_ns) {
    unsigned char buf[SOUP_BARE_WIRE];
    size_t n = soup_build_packet('O', NULL, 0, buf);
    out_.insert(out_.end(), buf, buf + n);
    last_sent_ns_ = now_ns;
}

void SoupClientSession::on_bytes(const unsigned char* data, size_t len,
                                 uint64_t now_ns) {
    last_received_ns_ = now_ns;
    framer_.feed(data, len);
    SoupPacket pkt;
    while (framer_.next(pkt)) {
        handle_packet(pkt, now_ns);
    }
}

void SoupClientSession::handle_packet(const SoupPacket& pkt,
                                      uint64_t now_ns) {
    (void)now_ns;
    const unsigned char* payload =
        pkt.payload.empty() ? NULL : &pkt.payload[0];
    switch (pkt.type) {
        case 'A': {
            SoupLoginAccepted la;
            const char* err = NULL;
            if (!soup_parse_login_accepted(payload, pkt.payload.size(), la,
                                           &err)) {
                LOG_WARN(COMP) << "bad LoginAccepted: " << (err ? err : "?");
                return;
            }
            session_id_ = la.session;
            next_seq_ = la.sequence;
            state_ = ST_LIVE;
            if (on_login_accepted) on_login_accepted(session_id_, next_seq_);
            return;
        }
        case 'J': {
            SoupLoginRejected rj;
            const char* err = NULL;
            if (soup_parse_login_rejected(payload, pkt.payload.size(), rj,
                                          &err)) {
                reject_code_ = rj.reject_code;
            }
            state_ = ST_FAILED;
            if (on_login_rejected) on_login_rejected(reject_code_);
            return;
        }
        case 'S': {
            uint64_t seq = next_seq_++;
            if (on_message) on_message(seq, payload, pkt.payload.size());
            return;
        }
        case 'H':
            return; // inbound byte already refreshed last_received_ns_
        case 'Z':
            state_ = ST_ENDED;
            if (on_end_of_session) on_end_of_session();
            return;
        case '+':
            return; // debug packet: ignore
        default:
            return; // never sent by a well-behaved server; ignore
    }
}

void SoupClientSession::on_tick(uint64_t now_ns) {
    if (state_ != ST_LOGIN_SENT && state_ != ST_LIVE) {
        return;
    }
    if (last_received_ns_ != 0 &&
        now_ns - last_received_ns_ >= SOUP_SILENCE_TIMEOUT_NS) {
        if (on_peer_silent) on_peer_silent();
        return;
    }
    if (last_sent_ns_ != 0 && now_ns - last_sent_ns_ >= SOUP_HEARTBEAT_NS) {
        unsigned char buf[SOUP_BARE_WIRE];
        size_t n = soup_build_packet('R', NULL, 0, buf);
        out_.insert(out_.end(), buf, buf + n);
        last_sent_ns_ = now_ns;
    }
}

bool SoupClientSession::take_output(std::vector<unsigned char>& out) {
    if (out_.empty()) {
        return false;
    }
    out.insert(out.end(), out_.begin(), out_.end());
    out_.clear();
    return true;
}

// --- TcpSoupConnector --------------------------------------------------------

TcpSoupConnector::TcpSoupConnector(Reactor& reactor, const std::string& host,
                                   int port, SoupClientSession& session)
    : reactor_(reactor),
      host_(host),
      port_(port),
      session_(session),
      fd_(-1),
      established_(false),
      stopped_(false),
      backoff_ns_(1000000000ULL),
      retry_timer_(0),
      tick_timer_(0) {}

void TcpSoupConnector::start() {
    attempt_connect();
}

void TcpSoupConnector::stop() {
    stopped_ = true;
    if (retry_timer_) reactor_.cancel(retry_timer_);
    if (tick_timer_) reactor_.cancel(tick_timer_);
    retry_timer_ = tick_timer_ = 0;
    close_fd();
}

void TcpSoupConnector::close_fd() {
    if (fd_ >= 0) {
        reactor_.remove(fd_);
        ::close(fd_);
        fd_ = -1;
    }
    established_ = false;
    sendbuf_.clear();
}

void TcpSoupConnector::attempt_connect() {
    if (stopped_) return;
    fd_ = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
    if (fd_ < 0) {
        LOG_ERROR(COMP) << "socket: " << std::strerror(errno);
        schedule_retry();
        return;
    }
    int one = 1;
    ::setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port_));
    if (::inet_pton(AF_INET, host_.c_str(), &addr.sin_addr) != 1) {
        LOG_ERROR(COMP) << "bad host address: " << host_;
        close_fd();
        schedule_retry();
        return;
    }
    int rc = ::connect(fd_, reinterpret_cast<struct sockaddr*>(&addr),
                       sizeof(addr));
    if (rc == 0) {
        on_connect_writable();
        return;
    }
    if (errno != EINPROGRESS) {
        LOG_WARN(COMP) << "connect " << host_ << ":" << port_ << ": "
                       << std::strerror(errno);
        close_fd();
        schedule_retry();
        return;
    }
    // Completion signalled by writability.
    TcpSoupConnector* self = this;
    reactor_.set_write(fd_, [self]() { self->on_connect_writable(); });
}

void TcpSoupConnector::on_connect_writable() {
    int err = 0;
    socklen_t elen = sizeof(err);
    ::getsockopt(fd_, SOL_SOCKET, SO_ERROR, &err, &elen);
    if (err != 0) {
        LOG_WARN(COMP) << "connect " << host_ << ":" << port_ << ": "
                       << std::strerror(err);
        close_fd();
        schedule_retry();
        return;
    }
    established_ = true;
    backoff_ns_ = 1000000000ULL; // reset backoff on a successful connect
    LOG_INFO(COMP) << "connected to " << host_ << ":" << port_
                   << ", logging in (session='" << session_.session_id()
                   << "' seq=" << session_.next_seq() << ")";
    reactor_.set_write(fd_, Reactor::Callback());
    TcpSoupConnector* self = this;
    reactor_.set_read(fd_, [self]() { self->on_readable(); });
    session_.reset();
    session_.start(mono_ns());
    flush();
    schedule_tick();
}

void TcpSoupConnector::on_readable() {
    unsigned char buf[65536];
    for (;;) {
        ssize_t n = ::recv(fd_, buf, sizeof(buf), 0);
        if (n > 0) {
            session_.on_bytes(buf, static_cast<size_t>(n), mono_ns());
            if (session_.state() == SoupClientSession::ST_ENDED ||
                session_.state() == SoupClientSession::ST_FAILED) {
                flush(); // let a queued logout out before the owner acts
                return;
            }
            continue;
        }
        if (n == 0) {
            handle_disconnect("peer closed");
            return;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            break;
        }
        if (errno == EINTR) {
            continue;
        }
        handle_disconnect(std::strerror(errno));
        return;
    }
    flush();
}

void TcpSoupConnector::flush() {
    if (fd_ < 0 || !established_) return;
    session_.take_output(sendbuf_);
    while (!sendbuf_.empty()) {
        ssize_t n = ::send(fd_, &sendbuf_[0], sendbuf_.size(), MSG_NOSIGNAL);
        if (n > 0) {
            sendbuf_.erase(sendbuf_.begin(), sendbuf_.begin() + n);
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            TcpSoupConnector* self = this;
            reactor_.set_write(fd_, [self]() { self->on_writable(); });
            return;
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        handle_disconnect(std::strerror(errno));
        return;
    }
    if (fd_ >= 0) {
        reactor_.set_write(fd_, Reactor::Callback());
    }
}

void TcpSoupConnector::on_writable() {
    reactor_.set_write(fd_, Reactor::Callback());
    flush();
}

void TcpSoupConnector::reconnect() {
    handle_disconnect("forced reconnect");
}

void TcpSoupConnector::handle_disconnect(const char* why) {
    if (stopped_) return;
    LOG_WARN(COMP) << "connection lost (" << why << "); retrying in "
                   << backoff_ns_ / 1000000ULL << " ms";
    close_fd();
    if (tick_timer_) {
        reactor_.cancel(tick_timer_);
        tick_timer_ = 0;
    }
    if (on_disconnect) on_disconnect();
    schedule_retry();
}

void TcpSoupConnector::schedule_retry() {
    if (stopped_) return;
    TcpSoupConnector* self = this;
    retry_timer_ = reactor_.call_later(backoff_ns_, [self]() {
        self->retry_timer_ = 0;
        self->attempt_connect();
    });
    backoff_ns_ *= 2;
    if (backoff_ns_ > 10000000000ULL) {
        backoff_ns_ = 10000000000ULL; // cap 10 s
    }
}

void TcpSoupConnector::schedule_tick() {
    if (stopped_ || fd_ < 0) return;
    TcpSoupConnector* self = this;
    tick_timer_ = reactor_.call_later(250000000ULL, [self]() {
        self->tick_timer_ = 0;
        if (self->fd_ < 0) return;
        self->session_.on_tick(mono_ns());
        self->flush();
        self->schedule_tick();
    });
}

} // namespace jnx
