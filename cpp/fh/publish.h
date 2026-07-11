// publish.h — FH output side:
//
// - PubContext: publisher-side per-ticker/group state that the market core
//   deliberately does not track (last system event per group, last trade
//   timestamp per ticker) — fed from ApplyResults and from recovery rows.
// - make_update(): the F3-deferred record builder — assembles one full
//   UpdateRecord (static+state from refdata, top-10 book from the levels,
//   trade summary from the tape, delta from the ApplyResult).
// - build_sync_dump(): RESET-less full dump of a Market as encoded
//   records (SYNC_BEGIN, TICKs, ORDERs, one '#' UPDATE per book, SYNC_END).
// - DbLink: blocking UDS link to jnxdb (SO_SNDTIMEO 5 s) with HELLO
//   handshake; failure marks it disconnected (owner reconnects via a
//   reactor timer — no threads).
// - McastSender: one sendto per UPDATE; IP_MULTICAST_LOOP=1.
#ifndef JNX_FH_PUBLISH_H
#define JNX_FH_PUBLISH_H

#include <netinet/in.h>

#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

#include "market/market.h"
#include "wire/record.h"

namespace jnx {

struct PubContext {
    // Latest S.event per group; "" key = system-wide (blank-group S).
    std::map<std::string, char> last_sys_event;
    // Exchange timestamp of the last trade per ticker.
    std::map<std::string, uint64_t> last_trade_ns;

    // Record an applied message's contribution (trade ns). S events go
    // through note_event (ApplyResult doesn't carry the event char).
    void note(const ApplyResult& res, uint64_t exch_ns);
    void note_event(const std::string& group, char event) {
        last_sys_event[group] = event;
    }

    // Latest event applicable to `group` (group-specific wins over
    // system-wide); '\0' = none.
    char event_for(const std::string& group) const;
};

struct Envelope {
    uint64_t epoch;
    uint64_t pub_seq;
    std::string session;
    uint64_t exch_seq;
    uint64_t exch_ns;
    char trigger;

    Envelope() : epoch(0), pub_seq(0), exch_seq(0), exch_ns(0),
                 trigger('\0') {}
};

// Build the full UPDATE for the (ticker, group) named by `res` (empty
// ticker allowed: S/L/orphan triggers produce a state-only record).
UpdateRecord make_update(const Market& market, const PubContext& ctx,
                         const ApplyResult& res, const Envelope& env);

// Encode a full sync dump of `market` into `out` (appended):
// SYNC_BEGIN, all TICK rows, all ORDER rows, one UPDATE per book
// (trigger '#', delta '#'), SYNC_END carrying (session, last_exch_seq,
// epoch). Deterministic order (sorted maps).
void build_sync_dump(const Market& market, const PubContext& ctx,
                     const std::string& session, uint64_t last_exch_seq,
                     uint64_t epoch, std::vector<unsigned char>& out);

class DbLink {
public:
    DbLink() : fd_(-1) {}
    ~DbLink() { close(); }

    // Blocking connect + HELLO handshake. On success fills db_hello with
    // the DB's reply and returns true; on any failure returns false with
    // the link closed.
    bool connect_hello(const std::string& sock_path, uint64_t my_epoch,
                       uint64_t my_last_seq, HelloRecord& db_hello);

    bool connected() const { return fd_ >= 0; }
    int fd() const { return fd_; }
    void close();

    // Blocking write of a full buffer (SO_SNDTIMEO bounds it). On any
    // error: closes the link, returns false.
    bool send(const unsigned char* data, size_t len);

    // Sends GET_STATE and streams the reply records to `cb` until
    // SYNC_END (inclusive). Returns false (link closed) on error/timeout.
    bool get_state(const std::function<void(const RawRecord&)>& cb);

private:
    int fd_;
};

class McastSender {
public:
    McastSender() : fd_(-1), send_errors_(0) {}
    ~McastSender();

    // group: dotted quad (e.g. 239.192.1.1); iface: local interface
    // address ("" = kernel default). Loopback is always enabled.
    bool open(const std::string& group, int port, int ttl,
              const std::string& iface);
    // Best-effort: failures are counted, never fatal.
    void send(const unsigned char* data, size_t len);

    uint64_t send_errors() const { return send_errors_; }

private:
    int fd_;
    struct sockaddr_in dest_;
    uint64_t send_errors_;
};

} // namespace jnx

#endif // JNX_FH_PUBLISH_H
