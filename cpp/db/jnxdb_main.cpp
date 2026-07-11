// jnxdb_main.cpp — the jnxdb in-memory database process (JNX_PLAN2.md F4).
//
// SINGLE-THREADED: one poll() loop over {UDS ingest listener, the one FH
// connection, TCP query listener, query connections}. No threads, no
// locks — every record is applied fully before the next fd is serviced,
// which is the atomicity guarantee.
//
// Config (file via --config=PATH, overridden by --key=value):
//   sock=/tmp/jnx-db.sock   UDS ingest path
//   query_port=26401        TCP query port (127.0.0.1)
//
// Signals: SIGINT/SIGTERM -> clean shutdown (close fds, unlink socket).
#include <csignal>
#include <cstring>
#include <vector>

#include <poll.h>
#include <unistd.h>

#include "common/cfg.h"
#include "common/log.h"
#include "common/procstat.h"
#include "common/time.h"
#include "db/ingest.h"
#include "db/query.h"
#include "db/tables.h"

namespace {

const char* COMP = "jnxdb";

volatile sig_atomic_t g_stop = 0;

void on_signal(int) {
    g_stop = 1;
}

} // namespace

int main(int argc, char** argv) {
    jnx::Cfg cfg;
    // Optional config file first, then --key=value overrides.
    for (int i = 1; i < argc; ++i) {
        std::string arg(argv[i]);
        if (arg.compare(0, 9, "--config=") == 0) {
            std::string path = arg.substr(9);
            if (!cfg.load_file(path)) {
                LOG_WARN(COMP) << "config file not readable: " << path;
            }
        }
    }
    cfg.apply_args(argc, argv);

    std::string sock_path = cfg.get("sock", "/tmp/jnx-db.sock");
    int query_port = static_cast<int>(cfg.get_int("query_port", 26401));

    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    jnx::Tables tables;
    jnx::IngestServer ingest(tables);
    jnx::QueryServer query(tables);

    if (!ingest.open(sock_path)) {
        return 1;
    }
    if (!query.open(query_port)) {
        ingest.close_all();
        return 1;
    }
    LOG_INFO(COMP) << "started (sock=" << sock_path
                   << " query_port=" << query_port << ")";

    uint64_t last_stats_ns = jnx::mono_ns();
    const uint64_t STATS_PERIOD_NS = 5000000000ULL;  // 5 s

    while (!g_stop) {
        std::vector<pollfd> fds;
        // [0] ingest listener
        pollfd p;
        p.fd = ingest.listen_fd();
        p.events = POLLIN;
        p.revents = 0;
        fds.push_back(p);
        // [1] ingest connection (may be absent)
        int ingest_conn_idx = -1;
        if (ingest.conn_fd() >= 0) {
            ingest_conn_idx = static_cast<int>(fds.size());
            p.fd = ingest.conn_fd();
            p.events = POLLIN;
            if (ingest.want_write()) {
                p.events |= POLLOUT;
            }
            p.revents = 0;
            fds.push_back(p);
        }
        // [2] query listener
        int query_listen_idx = static_cast<int>(fds.size());
        p.fd = query.listen_fd();
        p.events = POLLIN;
        p.revents = 0;
        fds.push_back(p);
        // [3..] query connections
        int query_conn_base = static_cast<int>(fds.size());
        for (size_t i = 0; i < query.conns().size(); ++i) {
            p.fd = query.conns()[i].fd;
            p.events = POLLIN;
            if (!query.conns()[i].outbuf.empty()) {
                p.events |= POLLOUT;
            }
            p.revents = 0;
            fds.push_back(p);
        }

        int rc = ::poll(&fds[0], fds.size(), 1000 /* ms */);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;  // signal — loop condition decides
            }
            LOG_ERROR(COMP) << "poll: " << std::strerror(errno);
            break;
        }

        if (rc > 0) {
            // Query connections first is fine — but service ingest before
            // accepting new query work per iteration to keep the data path
            // fresh. Order within one poll round doesn't affect atomicity:
            // each handler runs to completion.
            if (ingest_conn_idx >= 0) {
                short re = fds[ingest_conn_idx].revents;
                if (re & (POLLERR | POLLHUP)) {
                    ingest.on_conn_error();
                } else {
                    if (re & POLLIN) {
                        ingest.on_conn_readable();
                    }
                    if ((re & POLLOUT) && ingest.conn_fd() >= 0) {
                        ingest.on_conn_writable();
                    }
                }
            }
            if (fds[0].revents & POLLIN) {
                ingest.on_listen_ready();
            }
            if (fds[query_listen_idx].revents & POLLIN) {
                query.on_listen_ready();
            }
            size_t nconns = query.conns().size();
            for (size_t i = 0;
                 i < nconns &&
                 query_conn_base + i < fds.size();
                 ++i) {
                short re = fds[query_conn_base + i].revents;
                if (re & (POLLERR | POLLHUP)) {
                    query.on_conn_error(i);
                } else {
                    if (re & POLLIN) {
                        query.on_conn_readable(i);
                    }
                    if ((re & POLLOUT) && query.conns()[i].fd >= 0) {
                        query.on_conn_writable(i);
                    }
                }
            }
            query.reap();
        }

        uint64_t now = jnx::mono_ns();
        if (now - last_stats_ns >= STATS_PERIOD_NS) {
            last_stats_ns = now;
            const jnx::Meta& m = tables.meta();
            LOG_INFO(COMP) << "stats: session=" << m.session
                           << " epoch=" << m.epoch
                           << " last_exch_seq=" << m.last_exch_seq
                           << " updates=" << m.updates_applied
                           << " dups=" << m.dups_dropped
                           << " books=" << tables.books().size()
                           << " orders=" << tables.orders().size()
                           << " ticks=" << tables.tick_row_count()
                           << " fh_connected="
                           << (ingest.conn_fd() >= 0 ? 1 : 0)
                           << " query_clients=" << query.conns().size()
                           << " rss_kb=" << jnx::rss_kb();
        }
    }

    LOG_INFO(COMP) << "shutting down (signal received)";
    query.close_all();
    ingest.close_all();
    LOG_INFO(COMP) << "clean shutdown complete";
    return 0;
}
