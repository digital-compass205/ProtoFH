// soup.h — SoupBinTCP packet codec + incremental stream framer.
//
// Wire format (JNX_PLAN.md §3.4): logical packet =
//   length:2:num (big-endian, EXCLUDES the length field itself)
//   + type:1:char + payload.
//
// Packet types:
//   server->client: 'A' LoginAccepted, 'J' LoginRejected, 'S' SequencedData,
//                   'H' server heartbeat, 'Z' EndOfSession, '+' Debug
//   client->server: 'L' LoginRequest, 'U' UnsequencedData,
//                   'R' client heartbeat, 'O' LogoutRequest
//
// Padding rules (pinned by the golden vectors):
//   LoginRequest:  username(6)/password(10) RIGHT-padded with spaces;
//                  requested_session(10) and requested_sequence(20, ASCII
//                  digits) LEFT-padded with spaces.
//   LoginAccepted: session(10) and sequence(20, ASCII digits) LEFT-padded
//                  with spaces.
#ifndef JNX_SOUP_H
#define JNX_SOUP_H

#include <cstddef>
#include <cstdint>
#include <vector>

namespace jnx {

// One framed logical packet. `payload` is the bytes after the type char
// (owned copy — valid independent of the framer; simplicity first).
struct SoupPacket {
    char type;
    std::vector<unsigned char> payload;

    SoupPacket() : type('\0') {}
};

// --- typed payload structs (fields host-order / stripped strings) -------

struct SoupLoginRequest {
    char username[7];           // <= 6 chars
    char password[11];          // <= 10 chars
    char requested_session[11]; // <= 10 chars ("" = current session)
    uint64_t requested_sequence;

    SoupLoginRequest() : requested_sequence(0) {
        username[0] = '\0';
        password[0] = '\0';
        requested_session[0] = '\0';
    }
};

struct SoupLoginAccepted {
    char session[11]; // <= 10 chars
    uint64_t sequence;

    SoupLoginAccepted() : sequence(0) { session[0] = '\0'; }
};

struct SoupLoginRejected {
    char reject_code; // 'A' not authorized, 'S' session unavailable

    SoupLoginRejected() : reject_code('\0') {}
};

// --- payload parsers (from SoupPacket.payload) ---------------------------
// Return false + static *err on wrong payload length / non-numeric seq.

bool soup_parse_login_request(const unsigned char* payload, size_t len,
                              SoupLoginRequest& out, const char** err);
bool soup_parse_login_accepted(const unsigned char* payload, size_t len,
                               SoupLoginAccepted& out, const char** err);
bool soup_parse_login_rejected(const unsigned char* payload, size_t len,
                               SoupLoginRejected& out, const char** err);

// --- packet builders ------------------------------------------------------
// Each writes a COMPLETE wire packet (2-byte length prefix + type +
// payload) into buf and returns total bytes written. Callers size buf via
// the constants below (payload-carrying builders need 3 + payload_len).

const size_t SOUP_LOGIN_REQUEST_WIRE = 2 + 1 + 6 + 10 + 10 + 20; // 49
const size_t SOUP_LOGIN_ACCEPTED_WIRE = 2 + 1 + 10 + 20;         // 33
const size_t SOUP_LOGIN_REJECTED_WIRE = 2 + 1 + 1;               // 4
const size_t SOUP_BARE_WIRE = 2 + 1; // heartbeats / logout / end-of-session

size_t soup_build_login_request(const SoupLoginRequest& in,
                                unsigned char* buf);
size_t soup_build_login_accepted(const SoupLoginAccepted& in,
                                 unsigned char* buf);
size_t soup_build_login_rejected(const SoupLoginRejected& in,
                                 unsigned char* buf);
// Generic builder for payload-carrying ('S','U','+') and bare
// ('H','Z','R','O') packets. payload may be NULL when payload_len == 0.
size_t soup_build_packet(char type, const unsigned char* payload,
                         size_t payload_len, unsigned char* buf);

// --- stream framer --------------------------------------------------------
// Incremental: tolerates byte-at-a-time input and a length prefix split
// across feed() calls. Packets are returned in order via next(), which
// copies the payload into the caller's SoupPacket (the returned packet
// stays valid after further feed()/next() calls).
class SoupFramer {
public:
    SoupFramer() : bad_frames_(0) {}

    void feed(const unsigned char* data, size_t len);

    // Extracts the next complete packet; returns false if none buffered.
    bool next(SoupPacket& out);

    // Count of malformed frames skipped (declared length 0 — a packet must
    // contain at least the type char).
    uint64_t bad_frames() const { return bad_frames_; }

private:
    std::vector<unsigned char> buf_;
    uint64_t bad_frames_;
};

} // namespace jnx

#endif // JNX_SOUP_H
