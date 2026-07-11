// reactor.cpp — see reactor.h.
#include "fh/reactor.h"

#include <poll.h>

#include <vector>

#include "common/time.h"

namespace jnx {

void Reactor::set_read(int fd, Callback cb) {
    if (!cb && fds_.count(fd) && !fds_[fd].on_write) {
        fds_.erase(fd);
        return;
    }
    fds_[fd].on_read = cb;
}

void Reactor::set_write(int fd, Callback cb) {
    if (!cb && fds_.count(fd) && !fds_[fd].on_read) {
        fds_.erase(fd);
        return;
    }
    fds_[fd].on_write = cb;
}

void Reactor::remove(int fd) {
    fds_.erase(fd);
}

uint64_t Reactor::call_later(uint64_t delay_ns, Callback cb) {
    uint64_t id = next_timer_id_++;
    timers_[std::make_pair(mono_ns() + delay_ns, id)] = cb;
    return id;
}

void Reactor::cancel(uint64_t timer_id) {
    for (TimerMap::iterator it = timers_.begin(); it != timers_.end(); ++it) {
        if (it->first.second == timer_id) {
            timers_.erase(it);
            return;
        }
    }
}

void Reactor::run() {
    running_ = true;
    std::vector<struct pollfd> pfds;
    std::vector<int> fd_order;
    while (running_) {
        if (fds_.empty() && timers_.empty()) {
            break; // nothing left to drive
        }
        // Timeout: until the earliest timer, capped at 200 ms so stop()
        // from a signal-checking timer stays responsive.
        int timeout_ms = 200;
        if (!timers_.empty()) {
            uint64_t now = mono_ns();
            uint64_t deadline = timers_.begin()->first.first;
            uint64_t wait_ms =
                deadline > now ? (deadline - now) / 1000000ULL : 0;
            if (wait_ms < static_cast<uint64_t>(timeout_ms)) {
                timeout_ms = static_cast<int>(wait_ms);
            }
        }

        pfds.clear();
        fd_order.clear();
        for (std::map<int, FdInterest>::const_iterator it = fds_.begin();
             it != fds_.end(); ++it) {
            struct pollfd p;
            p.fd = it->first;
            p.events = 0;
            p.revents = 0;
            if (it->second.on_read) p.events |= POLLIN;
            if (it->second.on_write) p.events |= POLLOUT;
            pfds.push_back(p);
            fd_order.push_back(it->first);
        }

        int n = ::poll(pfds.empty() ? NULL : &pfds[0],
                       static_cast<nfds_t>(pfds.size()), timeout_ms);
        if (n > 0) {
            for (size_t i = 0; i < pfds.size() && running_; ++i) {
                short re = pfds[i].revents;
                if (re == 0) continue;
                std::map<int, FdInterest>::iterator it =
                    fds_.find(fd_order[i]);
                if (it == fds_.end()) continue; // removed by a callback
                if ((re & (POLLIN | POLLERR | POLLHUP)) && it->second.on_read) {
                    Callback cb = it->second.on_read;
                    cb();
                    it = fds_.find(fd_order[i]);
                    if (it == fds_.end()) continue;
                }
                if ((re & (POLLOUT | POLLERR)) && it->second.on_write) {
                    Callback cb = it->second.on_write;
                    cb();
                }
            }
        }

        // Fire due timers (copy out first: callbacks may schedule/cancel).
        uint64_t now = mono_ns();
        while (running_ && !timers_.empty() &&
               timers_.begin()->first.first <= now) {
            Callback cb = timers_.begin()->second;
            timers_.erase(timers_.begin());
            cb();
        }
    }
    running_ = false;
}

} // namespace jnx
