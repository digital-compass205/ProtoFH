// ingest.h — jnxdb UDS ingest: one FH connection at a time feeding records
// into Tables, plus the HELLO / GET_STATE / RESET / SYNC bracket protocol
// (JNX_PLAN2.md §3, docs/wire_spec.md).
//
// Non-blocking throughout; owned by the single jnxdb poll loop
// (jnxdb_main.cpp), which asks for the fds to poll and dispatches events
// back in. Replies (HELLO, GET_STATE dumps) go through a per-connection
// output buffer drained on POLLOUT so a slow reader never blocks the loop.
#ifndef JNX_DB_INGEST_H
#define JNX_DB_INGEST_H

#include <cstdint>
#include <string>
#include <vector>

#include "db/tables.h"
#include "wire/record.h"

namespace jnx {

class IngestServer {
public:
    explicit IngestServer(Tables& tables)
        : tables_(tables), listen_fd_(-1), conn_fd_(-1), in_sync_(false) {}

    // Creates + binds + listens on the UDS path (unlinking a stale socket
    // file first). Returns false (after logging) on failure.
    bool open(const std::string& sock_path);

    // Closes everything and unlinks the socket path (clean shutdown).
    void close_all();

    int listen_fd() const { return listen_fd_; }
    int conn_fd() const { return conn_fd_; }
    bool want_write() const { return !outbuf_.empty(); }

    // Event dispatch (called by the poll loop).
    void on_listen_ready();     // accept; kicks any existing connection
    void on_conn_readable();    // read + frame + apply protocol
    void on_conn_writable();    // drain outbuf
    void on_conn_error();       // POLLERR/POLLHUP

private:
    void handle_record(const RawRecord& rec);
    void drop_connection(const char* why);
    // Appends bytes to outbuf and attempts an immediate drain.
    void queue_write(const unsigned char* data, size_t len);
    void try_drain();

    Tables& tables_;
    std::string sock_path_;
    int listen_fd_;
    int conn_fd_;
    bool in_sync_;  // inside a SYNC_BEGIN..SYNC_END bracket
    RecordFramer framer_;
    std::vector<unsigned char> outbuf_;
};

} // namespace jnx

#endif // JNX_DB_INGEST_H
