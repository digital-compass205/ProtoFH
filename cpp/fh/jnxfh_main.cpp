// jnxfh_main.cpp — the jnxfh feed handler process (JNX_PLAN2.md F5).
//
// Flow: cfg -> DB connect + HELLO -> recovery or bootstrap -> live loop
// (reactor): soup packet -> itch decode -> market.apply -> one UPDATE per
// applied message (except 'T') -> DB write FIRST, then multicast send.
// Single-threaded: DB reconnect is a reactor timer, never a thread.
//
// Config keys (file via --config=..., overridden by --key=value):
//   itch_host itch_port glimpse_host glimpse_port user pass
//   db_sock mcast_group mcast_port mcast_ttl mcast_if
//   bootstrap (replay|glimpse)  require_db (0|1)
#include <signal.h>
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "common/cfg.h"
#include "common/log.h"
#include "common/procstat.h"
#include "common/time.h"
#include "fh/glimpse.h"
#include "fh/publish.h"
#include "fh/reactor.h"
#include "fh/recover.h"
#include "fh/soupclient.h"
#include "itch/itch.h"
#include "market/market.h"
#include "wire/record.h"

namespace {

const char* COMP = "jnxfh";

volatile sig_atomic_t g_stop = 0;

void on_signal(int) {
    g_stop = 1;
}

struct Fh {
    // config
    std::string db_sock;
    bool require_db;

    // state
    jnx::Market market;
    jnx::PubContext ctx;
    jnx::DbLink db;
    jnx::McastSender mcast;
    uint64_t epoch;
    uint64_t pub_seq;         // last published pub_seq (starts 0, first is 1)
    uint64_t last_exch_seq;   // last APPLIED exchange seq (incl. T)
    std::string session;      // current exchange session id
    bool db_connected;

    // stats
    uint64_t published;
    uint64_t db_write_fails;
    uint64_t decode_errors;
    uint64_t msgs_since_stats;

    int exit_code;

    Fh() : require_db(false), epoch(0), pub_seq(0), last_exch_seq(0),
           db_connected(false), published(0), db_write_fails(0),
           decode_errors(0), msgs_since_stats(0), exit_code(0) {}

    // RESET + full SYNC dump from our own market state.
    bool push_full_sync() {
        std::vector<unsigned char> out;
        unsigned char ctl[jnx::CONTROL_WIRE_SIZE];
        size_t n = jnx::encode_control(jnx::KIND_RESET, ctl);
        out.insert(out.end(), ctl, ctl + n);
        jnx::build_sync_dump(market, ctx, session, last_exch_seq, epoch, out);
        if (!db.send(&out[0], out.size())) {
            db_connected = false;
            return false;
        }
        LOG_INFO(COMP) << "pushed RESET + full sync to db (" << out.size()
                       << " bytes, last_seq=" << last_exch_seq << ")";
        return true;
    }

    // (Re)establish the DB link; on success sync if needed. Returns
    // db_connected.
    bool try_db_connect() {
        jnx::HelloRecord db_hello;
        if (!db.connect_hello(db_sock, epoch, last_exch_seq, db_hello)) {
            return false;
        }
        db_connected = true;
        LOG_INFO(COMP) << "db connected (db epoch=" << db_hello.epoch
                       << " last_seq=" << db_hello.last_exch_seq << ")";
        // Anything other than an exact match of our position means the DB
        // is empty/stale/from another life: rebase it from our state.
        if (db_hello.epoch != epoch ||
            db_hello.last_exch_seq != last_exch_seq) {
            if (!push_full_sync()) {
                return false;
            }
        }
        return db_connected;
    }
};

} // namespace

int main(int argc, char** argv) {
    jnx::Cfg cfg;
    // Optional config file first, then --key=value overrides.
    for (int i = 1; i < argc; ++i) {
        if (std::strncmp(argv[i], "--config=", 9) == 0) {
            if (!cfg.load_file(argv[i] + 9)) {
                std::fprintf(stderr, "jnxfh: cannot read config %s\n",
                             argv[i] + 9);
                return 2;
            }
        }
    }
    cfg.apply_args(argc, argv);

    const std::string itch_host = cfg.get("itch_host", "127.0.0.1");
    const int itch_port = static_cast<int>(cfg.get_int("itch_port", 15001));
    const std::string glimpse_host =
        cfg.get("glimpse_host", cfg.get("itch_host", "127.0.0.1"));
    const int glimpse_port =
        static_cast<int>(cfg.get_int("glimpse_port", 15002));
    const std::string user = cfg.get("user", "TEST");
    const std::string pass = cfg.get("pass", "SECRET");
    const std::string bootstrap = cfg.get("bootstrap", "replay");
    const std::string mcast_group = cfg.get("mcast_group", "239.192.1.1");
    const int mcast_port = static_cast<int>(cfg.get_int("mcast_port", 26400));
    const int mcast_ttl = static_cast<int>(cfg.get_int("mcast_ttl", 1));
    const std::string mcast_if = cfg.get("mcast_if", "");

    Fh fh;
    fh.db_sock = cfg.get("db_sock", "/tmp/jnx-db.sock");
    fh.require_db = cfg.get_int("require_db", 0) != 0;
    fh.epoch = jnx::now_ns(); // provisional; recovery may adopt the DB's

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    if (!fh.mcast.open(mcast_group, mcast_port, mcast_ttl, mcast_if)) {
        return 2;
    }

    // ---- startup decision tree (restart matrix) --------------------------
    std::string login_session; // requested session for the ITCH login
    uint64_t login_seq = 1;

    jnx::HelloRecord db_hello;
    bool db_up = fh.db.connect_hello(fh.db_sock, 0, 0, db_hello);
    if (!db_up && fh.require_db) {
        LOG_ERROR(COMP) << "db unavailable at " << fh.db_sock
                        << " and require_db=1";
        return 3;
    }

    if (db_up && (db_hello.epoch != 0 || db_hello.last_exch_seq != 0)) {
        // DB has state: recover and resume (zero replay).
        jnx::RecoveredMeta meta;
        if (recover_from_db(fh.db, fh.market, fh.ctx, meta)) {
            fh.session = meta.session;
            fh.last_exch_seq = meta.last_exch_seq;
            fh.epoch = meta.epoch;
            fh.db_connected = true;
            login_session = meta.session;
            login_seq = meta.last_exch_seq + 1;
            LOG_INFO(COMP) << "resume mode: session='" << login_session
                           << "' seq=" << login_seq;
        } else {
            LOG_ERROR(COMP) << "db recovery failed; falling back to "
                            << bootstrap << " bootstrap with a fresh market";
            fh.market = jnx::Market();
            fh.ctx = jnx::PubContext();
            db_up = fh.db.connect_hello(fh.db_sock, fh.epoch,
                                        fh.last_exch_seq, db_hello);
        }
    }

    if (login_seq == 1 && login_session.empty()) {
        // No recovered state: cold bootstrap.
        if (bootstrap == "glimpse") {
            jnx::GlimpseResult gr;
            const char* err = NULL;
            if (!jnx::glimpse_bootstrap(glimpse_host, glimpse_port, user,
                                        pass, fh.market, gr, &err)) {
                LOG_ERROR(COMP) << "glimpse bootstrap failed: "
                                << (err ? err : "?");
                return 4;
            }
            fh.session = gr.session;
            fh.last_exch_seq = gr.next_live_seq - 1;
            login_session = ""; // blank = current session (spec-safe)
            login_seq = gr.next_live_seq;
        } else if (bootstrap == "replay") {
            login_session = "";
            login_seq = 1;
            fh.last_exch_seq = 0;
        } else {
            LOG_ERROR(COMP) << "unknown bootstrap mode: " << bootstrap;
            return 2;
        }
        if (db_up) {
            fh.db_connected = true;
            if (!fh.push_full_sync()) {
                LOG_WARN(COMP) << "initial sync failed; will retry";
            }
        }
    }

    // ---- live loop --------------------------------------------------------
    jnx::Reactor reactor;
    jnx::SoupClientSession session(user, pass, login_session, login_seq);
    jnx::TcpSoupConnector connector(reactor, itch_host, itch_port, session);

    session.on_login_accepted = [&](const std::string& sid, uint64_t seq) {
        fh.session = sid;
        LOG_INFO(COMP) << "logged in: session='" << sid << "' next seq "
                       << seq;
    };
    session.on_login_rejected = [&](char code) {
        LOG_ERROR(COMP) << "login rejected with code '" << code
                        << "' — terminating (no retry)";
        fh.exit_code = 5;
        connector.stop();
        reactor.stop();
    };
    session.on_end_of_session = [&]() {
        LOG_INFO(COMP) << "end of session (Z): published=" << fh.published
                       << " last_seq=" << fh.last_exch_seq
                       << " pub_seq=" << fh.pub_seq;
        session.logout(jnx::mono_ns());
        connector.flush();
        connector.stop();
        reactor.stop();
    };
    session.on_peer_silent = [&]() {
        LOG_WARN(COMP) << "peer silent for 15 s; reconnecting";
        connector.reconnect();
    };
    session.on_message = [&](uint64_t seq, const unsigned char* payload,
                             size_t len) {
        jnx::ItchMsg msg;
        const char* err = NULL;
        if (!jnx::decode(payload, len, msg, &err)) {
            ++fh.decode_errors;
            LOG_WARN(COMP) << "itch decode error at seq " << seq << ": "
                           << (err ? err : "?");
            return;
        }
        jnx::ApplyResult res = fh.market.apply(msg);
        fh.last_exch_seq = seq;
        ++fh.msgs_since_stats;
        if (!res.applied || res.trigger == 'T') {
            return; // T updates the clock but publishes nothing (pinned)
        }
        uint64_t exch_ns =
            jnx::make_timestamp(fh.market.seconds, msg.type == 'T' ? 0 : msg.ns);
        if (res.trigger == 'S') {
            fh.ctx.note_event(res.group, msg.event);
        }
        fh.ctx.note(res, exch_ns);

        jnx::Envelope env;
        env.epoch = fh.epoch;
        env.pub_seq = ++fh.pub_seq;
        env.session = fh.session;
        env.exch_seq = seq;
        env.exch_ns = exch_ns;
        env.trigger = res.trigger;
        jnx::UpdateRecord rec = jnx::make_update(fh.market, fh.ctx, res, env);
        unsigned char buf[jnx::UPDATE_WIRE_SIZE];
        size_t n = jnx::encode_update(rec, buf);
        // DB write FIRST (authoritative), then multicast.
        if (fh.db_connected) {
            if (!fh.db.send(buf, n)) {
                fh.db_connected = false;
                ++fh.db_write_fails;
            }
        }
        fh.mcast.send(buf, n);
        ++fh.published;
    };

    // DB reconnect loop: a 1 s reactor timer, never a thread.
    std::function<void()> db_timer_fn;
    db_timer_fn = [&]() {
        if (!fh.db_connected) {
            if (fh.try_db_connect()) {
                LOG_INFO(COMP) << "db link restored";
            }
        }
        reactor.call_later(1000000000ULL, db_timer_fn);
    };
    reactor.call_later(1000000000ULL, db_timer_fn);

    // Signal watcher + clean shutdown.
    std::function<void()> sig_timer_fn;
    sig_timer_fn = [&]() {
        if (g_stop) {
            LOG_INFO(COMP) << "signal received; shutting down cleanly";
            session.logout(jnx::mono_ns());
            connector.flush();
            connector.stop();
            reactor.stop();
            return;
        }
        reactor.call_later(200000000ULL, sig_timer_fn);
    };
    reactor.call_later(200000000ULL, sig_timer_fn);

    // 5 s stats line.
    std::function<void()> stats_timer_fn;
    stats_timer_fn = [&]() {
        LOG_INFO(COMP) << "stats: msgs/s=" << fh.msgs_since_stats / 5
                       << " exch_seq=" << fh.last_exch_seq
                       << " pub_seq=" << fh.pub_seq
                       << " books=" << fh.market.books.books().size()
                       << " orders=" << fh.market.books.orders().size()
                       << " db_connected=" << (fh.db_connected ? 1 : 0)
                       << " mcast_errors=" << fh.mcast.send_errors()
                       << " rss_kb=" << jnx::rss_kb();
        fh.msgs_since_stats = 0;
        reactor.call_later(5000000000ULL, stats_timer_fn);
    };
    reactor.call_later(5000000000ULL, stats_timer_fn);

    LOG_INFO(COMP) << "starting live loop: itch " << itch_host << ":"
                   << itch_port << ", db " << fh.db_sock << " (connected="
                   << (fh.db_connected ? 1 : 0) << "), epoch=" << fh.epoch;
    connector.start();
    reactor.run();

    LOG_INFO(COMP) << "exit: published=" << fh.published
                   << " pub_seq=" << fh.pub_seq << " last_exch_seq="
                   << fh.last_exch_seq << " decode_errors="
                   << fh.decode_errors << " db_write_fails="
                   << fh.db_write_fails;
    return fh.exit_code;
}
