// soupclient.h — SoupBinTCP client:
//
// - SoupClientSession: sans-I/O session state machine, a direct port of
//   jnxfeed/soup/session.py. Fed received bytes + a periodic clock tick;
//   produces output bytes; reports events through std::function callbacks.
//   Sequence numbering is self-counted from the Login-Accepted value
//   (Soup carries no per-message seq).
// - TcpSoupConnector: reactor-driven TCP glue with reconnect (1 s backoff
//   doubling, capped 10 s), re-logging-in with the SAME session id at the
//   next expected seq (lossless resume). Login-Rejected is terminal (no
//   retry storm) — surfaced via the session's on_login_rejected.
#ifndef JNX_FH_SOUPCLIENT_H
#define JNX_FH_SOUPCLIENT_H

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "fh/reactor.h"
#include "soup/soup.h"

namespace jnx {

const uint64_t SOUP_HEARTBEAT_NS = 1000000000ULL;        // 1 s outbound idle
const uint64_t SOUP_SILENCE_TIMEOUT_NS = 15000000000ULL; // 15 s inbound idle

class SoupClientSession {
public:
    enum State { ST_CONNECTED, ST_LOGIN_SENT, ST_LIVE, ST_ENDED, ST_FAILED };

    SoupClientSession(const std::string& username,
                      const std::string& password,
                      const std::string& requested_session = "",
                      uint64_t requested_seq = 1);

    // Event callbacks (any may be left empty).
    std::function<void(const std::string&, uint64_t)> on_login_accepted;
    std::function<void(char)> on_login_rejected;
    std::function<void(uint64_t, const unsigned char*, size_t)> on_message;
    std::function<void()> on_end_of_session;
    std::function<void()> on_peer_silent;

    // Prepare for reuse on a fresh TCP connection: keeps the resume point
    // (session_id/next_seq), clears framing/output/timing state.
    void reset();

    // Queue the Login Request (call once the TCP connection is up). Uses
    // the maintained resume point.
    void start(uint64_t now_ns);

    // Override the resume point before start() (bootstrap paths).
    void set_resume(const std::string& session_id, uint64_t next_seq);

    void logout(uint64_t now_ns);

    // Feed received bytes; fires callbacks synchronously.
    void on_bytes(const unsigned char* data, size_t len, uint64_t now_ns);

    // Periodic clock: client heartbeat after 1 s outbound silence; fires
    // on_peer_silent after 15 s inbound silence.
    void on_tick(uint64_t now_ns);

    // Append pending output bytes to out and clear the buffer. Returns
    // true if anything was appended.
    bool take_output(std::vector<unsigned char>& out);
    bool has_output() const { return !out_.empty(); }

    State state() const { return state_; }
    char reject_code() const { return reject_code_; }
    const std::string& session_id() const { return session_id_; }
    uint64_t next_seq() const { return next_seq_; }

private:
    void handle_packet(const SoupPacket& pkt, uint64_t now_ns);

    std::string username_;
    std::string password_;
    std::string session_id_; // resume point, survives reset()
    uint64_t next_seq_;      // resume point, survives reset()

    State state_;
    char reject_code_;
    SoupFramer framer_;
    std::vector<unsigned char> out_;
    uint64_t last_sent_ns_;     // 0 = never
    uint64_t last_received_ns_; // 0 = never
};

// Reactor-driven TCP transport for a SoupClientSession.
class TcpSoupConnector {
public:
    TcpSoupConnector(Reactor& reactor, const std::string& host, int port,
                     SoupClientSession& session);

    // Called after every unplanned connection loss (before the retry is
    // scheduled) — informational.
    std::function<void()> on_disconnect;

    void start();  // first connect attempt
    void stop();   // close + cancel timers (terminal)
    // Push session output to the socket now (call after queuing packets).
    void flush();
    // Force a reconnect cycle (e.g. peer-silent).
    void reconnect();

    bool connected() const { return fd_ >= 0 && established_; }

private:
    void attempt_connect();
    void on_connect_writable();
    void on_readable();
    void on_writable();
    void handle_disconnect(const char* why);
    void schedule_retry();
    void schedule_tick();
    void close_fd();

    Reactor& reactor_;
    std::string host_;
    int port_;
    SoupClientSession& session_;
    int fd_;
    bool established_;
    bool stopped_;
    uint64_t backoff_ns_;
    uint64_t retry_timer_;
    uint64_t tick_timer_;
    std::vector<unsigned char> sendbuf_; // unwritten tail
};

} // namespace jnx

#endif // JNX_FH_SOUPCLIENT_H
