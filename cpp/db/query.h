// query.h — jnxdb TCP query interface: line-based text protocol on
// 127.0.0.1 for operators (tools/dbquery.py) and diagnostics.
//
// One command per line; every response is terminated by a lone "." line.
// Commands: PING, GET <ticker>, BOOK <ticker>, ORDERS <ticker>,
// TRADES <ticker>, TABLE static|state|trades, STATS, SNAP.
//
// SNAP is the bulk current-image snapshot: a header line
// "SNAP epoch=<> last_exch_seq=<> session=<> count=<n>" followed by <n>
// base64-encoded binary UPDATE records (one per book, the same frozen wire
// format as the multicast feed) so a reconnecting client (jnxweb) can seed
// its whole state in one round-trip and reconcile against live UDP by
// (epoch, exch_seq). See cpp/db/query.cpp.
//
// Non-blocking, owned by the jnxdb poll loop. Each connection has its own
// output buffer drained on POLLOUT: a slow query client never stalls the
// ingest path. Buffers are capped (a client that won't read a large TABLE
// response gets dropped rather than growing memory unboundedly).
#ifndef JNX_DB_QUERY_H
#define JNX_DB_QUERY_H

#include <cstdint>
#include <string>
#include <vector>

#include "db/tables.h"

namespace jnx {

class QueryServer {
public:
    struct Conn {
        int fd;
        std::string inbuf;
        std::string outbuf;

        Conn() : fd(-1) {}
    };

    explicit QueryServer(Tables& tables)
        : tables_(tables), listen_fd_(-1), port_(0) {}

    // Binds 127.0.0.1:<port> with SO_REUSEADDR; returns false on failure.
    bool open(int port);
    void close_all();

    int listen_fd() const { return listen_fd_; }
    int port() const { return port_; }
    const std::vector<Conn>& conns() const { return conns_; }

    void on_listen_ready();
    void on_conn_readable(size_t idx);
    void on_conn_writable(size_t idx);
    void on_conn_error(size_t idx);

    // Removes closed connections (fd == -1) — call once per loop pass.
    void reap();

    // Builds the full response (terminated by ".\n") for one command
    // line. Public for unit testing without sockets.
    std::string respond(const std::string& line) const;

private:
    void close_conn(size_t idx, const char* why);

    Tables& tables_;
    int listen_fd_;
    int port_;
    std::vector<Conn> conns_;
};

} // namespace jnx

#endif // JNX_DB_QUERY_H
