// ingest.cpp — see ingest.h.
#include "db/ingest.h"

#include <cerrno>
#include <cstring>

#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "common/endian.h"
#include "common/log.h"

namespace jnx {

namespace {

const char* COMP = "jnxdb.ingest";

bool set_nonblocking(int fd) {
    int flags = ::fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return false;
    }
    return ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}

// The typed decoders take a full record (header + body); the framer hands
// out kind + body. Re-synthesize the 8-byte header in front of the body —
// cheaper than a parallel body-only decode API and keeps one code path.
void assemble(char kind, const std::vector<unsigned char>& body,
              std::vector<unsigned char>& out) {
    out.resize(RECORD_HEADER_SIZE + body.size());
    be_put_u16(&out[0], RECORD_MAGIC);
    out[2] = RECORD_VERSION;
    out[3] = static_cast<unsigned char>(kind);
    be_put_u16(&out[4], static_cast<uint16_t>(body.size()));
    be_put_u16(&out[6], 0);
    if (!body.empty()) {
        std::memcpy(&out[RECORD_HEADER_SIZE], &body[0], body.size());
    }
}

} // namespace

bool IngestServer::open(const std::string& sock_path) {
    sock_path_ = sock_path;

    sockaddr_un addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (sock_path.size() >= sizeof(addr.sun_path)) {
        LOG_ERROR(COMP) << "socket path too long: " << sock_path;
        return false;
    }
    std::strncpy(addr.sun_path, sock_path.c_str(), sizeof(addr.sun_path) - 1);

    ::unlink(sock_path.c_str());  // stale socket from an unclean death

    listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        LOG_ERROR(COMP) << "socket(AF_UNIX): " << std::strerror(errno);
        return false;
    }
    if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr),
               sizeof(addr)) != 0) {
        LOG_ERROR(COMP) << "bind(" << sock_path
                        << "): " << std::strerror(errno);
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    if (::listen(listen_fd_, 4) != 0) {
        LOG_ERROR(COMP) << "listen: " << std::strerror(errno);
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    set_nonblocking(listen_fd_);
    LOG_INFO(COMP) << "listening on " << sock_path;
    return true;
}

void IngestServer::close_all() {
    if (conn_fd_ >= 0) {
        ::close(conn_fd_);
        conn_fd_ = -1;
    }
    if (listen_fd_ >= 0) {
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
    if (!sock_path_.empty()) {
        ::unlink(sock_path_.c_str());
    }
}

void IngestServer::on_listen_ready() {
    int fd = ::accept(listen_fd_, 0, 0);
    if (fd < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            LOG_WARN(COMP) << "accept: " << std::strerror(errno);
        }
        return;
    }
    if (conn_fd_ >= 0) {
        LOG_WARN(COMP)
            << "new FH connection kicks the existing one (one at a time)";
        drop_connection("replaced by new connection");
    }
    set_nonblocking(fd);
    conn_fd_ = fd;
    in_sync_ = false;
    framer_ = RecordFramer();
    outbuf_.clear();
    LOG_INFO(COMP) << "FH connected";
}

void IngestServer::drop_connection(const char* why) {
    if (conn_fd_ < 0) {
        return;
    }
    ::close(conn_fd_);
    conn_fd_ = -1;
    outbuf_.clear();
    framer_ = RecordFramer();
    if (in_sync_) {
        // Partial sync must never survive: BEGIN without END -> wipe.
        LOG_WARN(COMP)
            << "connection lost inside SYNC bracket — discarding partial "
               "sync (tables reset)";
        tables_.reset();
        tables_.count_sync_discarded();
        in_sync_ = false;
    }
    LOG_INFO(COMP) << "FH disconnected (" << why << ")";
}

void IngestServer::on_conn_error() {
    drop_connection("socket error/hangup");
}

void IngestServer::on_conn_readable() {
    unsigned char buf[65536];
    for (;;) {
        ssize_t n = ::recv(conn_fd_, buf, sizeof(buf), 0);
        if (n > 0) {
            framer_.feed(buf, static_cast<size_t>(n));
            RawRecord rec;
            while (conn_fd_ >= 0 && framer_.next(rec)) {
                handle_record(rec);
            }
            if (conn_fd_ >= 0 && framer_.corrupt()) {
                LOG_ERROR(COMP) << "corrupt record stream ("
                                << framer_.corrupt_reason()
                                << ") — closing ingest connection";
                drop_connection("corrupt stream");
                return;
            }
            continue;
        }
        if (n == 0) {
            drop_connection("EOF");
            return;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return;
        }
        if (errno == EINTR) {
            continue;
        }
        LOG_WARN(COMP) << "recv: " << std::strerror(errno);
        drop_connection("recv error");
        return;
    }
}

void IngestServer::handle_record(const RawRecord& rec) {
    std::vector<unsigned char> whole;
    const char* err = 0;

    switch (rec.kind) {
        case KIND_HELLO: {
            assemble(rec.kind, rec.body, whole);
            HelloRecord in;
            if (!decode_hello(&whole[0], whole.size(), in, &err)) {
                LOG_ERROR(COMP) << "bad HELLO: " << err;
                drop_connection("bad HELLO");
                return;
            }
            LOG_INFO(COMP) << "HELLO from FH: epoch=" << in.epoch
                           << " last_exch_seq=" << in.last_exch_seq
                           << " — replying epoch=" << tables_.meta().epoch
                           << " last_exch_seq="
                           << tables_.meta().last_exch_seq;
            HelloRecord reply;
            reply.epoch = tables_.meta().epoch;
            reply.last_exch_seq = tables_.meta().last_exch_seq;
            unsigned char out[HELLO_WIRE_SIZE];
            queue_write(out, encode_hello(reply, out));
            break;
        }
        case KIND_GET_STATE: {
            LOG_INFO(COMP) << "GET_STATE — dumping "
                           << tables_.books().size() << " book row(s), "
                           << tables_.orders().size() << " order(s), "
                           << tables_.tick_row_count() << " tick row(s)";
            IngestServer* self = this;
            tables_.dump_state(
                [self](const unsigned char* data, size_t len) {
                    self->queue_write(data, len);
                });
            break;
        }
        case KIND_RESET:
            LOG_INFO(COMP) << "RESET — wiping all tables";
            tables_.reset();
            break;
        case KIND_SYNC_BEGIN:
            if (in_sync_) {
                LOG_WARN(COMP) << "nested SYNC_BEGIN — staying in bracket";
            } else {
                LOG_INFO(COMP) << "SYNC_BEGIN";
                in_sync_ = true;
            }
            break;
        case KIND_SYNC_END: {
            assemble(rec.kind, rec.body, whole);
            SyncEndRecord in;
            if (!decode_sync_end(&whole[0], whole.size(), in, &err)) {
                LOG_ERROR(COMP) << "bad SYNC_END: " << err;
                drop_connection("bad SYNC_END");
                return;
            }
            if (!in_sync_) {
                LOG_WARN(COMP) << "SYNC_END without SYNC_BEGIN — ignored";
                break;
            }
            tables_.adopt_meta(in);
            tables_.count_sync_completed();
            in_sync_ = false;
            LOG_INFO(COMP) << "SYNC_END — adopted session=" << in.session
                           << " last_exch_seq=" << in.last_exch_seq
                           << " epoch=" << in.epoch;
            break;
        }
        case KIND_UPDATE: {
            assemble(rec.kind, rec.body, whole);
            UpdateRecord in;
            if (!decode_update(&whole[0], whole.size(), in, &err)) {
                LOG_ERROR(COMP) << "bad UPDATE: " << err;
                drop_connection("bad UPDATE");
                return;
            }
            tables_.apply_update(in, in_sync_);
            break;
        }
        case KIND_ORDER: {
            assemble(rec.kind, rec.body, whole);
            OrderRecord in;
            if (!decode_order(&whole[0], whole.size(), in, &err)) {
                LOG_ERROR(COMP) << "bad ORDER: " << err;
                drop_connection("bad ORDER");
                return;
            }
            tables_.apply_order(in);
            break;
        }
        case KIND_TICK: {
            assemble(rec.kind, rec.body, whole);
            TickRecord in;
            if (!decode_tick(&whole[0], whole.size(), in, &err)) {
                LOG_ERROR(COMP) << "bad TICK: " << err;
                drop_connection("bad TICK");
                return;
            }
            tables_.apply_tick(in);
            break;
        }
        default:
            // decode_header in the framer only passes known kinds; keep a
            // guard anyway.
            LOG_WARN(COMP) << "unhandled record kind '" << rec.kind << "'";
            break;
    }
}

void IngestServer::queue_write(const unsigned char* data, size_t len) {
    if (conn_fd_ < 0) {
        return;
    }
    outbuf_.insert(outbuf_.end(), data, data + len);
    try_drain();
}

void IngestServer::try_drain() {
    while (conn_fd_ >= 0 && !outbuf_.empty()) {
        ssize_t n = ::send(conn_fd_, &outbuf_[0], outbuf_.size(),
                           MSG_NOSIGNAL);
        if (n > 0) {
            outbuf_.erase(outbuf_.begin(), outbuf_.begin() + n);
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return;  // poll loop will call on_conn_writable() later
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        LOG_WARN(COMP) << "send: " << std::strerror(errno);
        drop_connection("send error");
        return;
    }
}

void IngestServer::on_conn_writable() {
    try_drain();
}

} // namespace jnx
