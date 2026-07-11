// reactor.h — minimal single-threaded poll() event loop with monotonic
// timers (C++ port of jnxfeed/net/reactor.py semantics):
//
// - set_read(fd, cb) / set_write(fd, cb) — independent read/write interest
//   per fd; a null function clears that interest; remove(fd) drops both.
// - call_later(delay_ns, cb) -> id; cancel(id). Timers fire in deadline
//   order (ties in scheduling order), based on CLOCK_MONOTONIC.
// - run() loops until stop(); stop() is safe from within any callback.
#ifndef JNX_FH_REACTOR_H
#define JNX_FH_REACTOR_H

#include <cstdint>
#include <functional>
#include <map>
#include <utility>

namespace jnx {

class Reactor {
public:
    typedef std::function<void()> Callback;

    Reactor() : next_timer_id_(1), running_(false) {}

    // Register/replace the read (write) callback for fd; an empty
    // std::function clears the interest.
    void set_read(int fd, Callback cb);
    void set_write(int fd, Callback cb);
    void remove(int fd);

    // Schedule cb to run once after delay_ns (monotonic). Returns a
    // cancellation id (never 0).
    uint64_t call_later(uint64_t delay_ns, Callback cb);
    void cancel(uint64_t timer_id);

    void run();
    void stop() { running_ = false; }

private:
    struct FdInterest {
        Callback on_read;
        Callback on_write;
    };
    // key: (deadline_ns, timer_id) — deadline order, scheduling order ties.
    typedef std::map<std::pair<uint64_t, uint64_t>, Callback> TimerMap;

    std::map<int, FdInterest> fds_;
    TimerMap timers_;
    uint64_t next_timer_id_;
    bool running_;
};

} // namespace jnx

#endif // JNX_FH_REACTOR_H
