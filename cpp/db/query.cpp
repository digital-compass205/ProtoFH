// query.cpp — see query.h.
#include "db/query.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <sstream>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "common/log.h"
#include "common/procstat.h"

namespace jnx {

namespace {

const char* COMP = "jnxdb.query";

// A client that stops reading gets dropped once its buffered response
// exceeds this (protects the single-threaded loop's memory).
const size_t MAX_OUTBUF = 4 * 1024 * 1024;

bool set_nonblocking(int fd) {
    int flags = ::fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return false;
    }
    return ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}

// Render a price int (1 implied decimal). NO_PRICE -> "-", 0 -> "0.0".
std::string price_str(uint32_t raw) {
    if (raw == 0x7FFFFFFFu) {
        return "-";
    }
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%u.%u", raw / 10, raw % 10);
    return buf;
}

// Render a single-char tag; '\0' (no value) -> "-".
std::string char_str(char c) {
    if (c == '\0') {
        return "-";
    }
    return std::string(1, c);
}

// First word of a line; rest returned via arg (trimmed of one space).
std::string split_cmd(const std::string& line, std::string& arg) {
    size_t sp = line.find(' ');
    if (sp == std::string::npos) {
        arg.clear();
        return line;
    }
    arg = line.substr(sp + 1);
    // trim trailing whitespace from arg (telnet users send \r)
    while (!arg.empty() &&
           (arg[arg.size() - 1] == '\r' || arg[arg.size() - 1] == ' ')) {
        arg.erase(arg.size() - 1);
    }
    return line.substr(0, sp);
}

void row_key_values(std::ostringstream& os, const Tables::Key& key,
                    const BookRow& r) {
    os << "ticker=" << key.first << "\n";
    os << "group=" << key.second << "\n";
    os << "isin=" << r.isin << "\n";
    os << "round_lot=" << r.round_lot << "\n";
    os << "tick_table_id=" << r.tick_table_id << "\n";
    os << "price_decimals=" << static_cast<unsigned>(r.price_decimals)
       << "\n";
    os << "upper_limit=" << price_str(r.upper_limit) << "\n";
    os << "lower_limit=" << price_str(r.lower_limit) << "\n";
    os << "directory_seen="
       << ((r.flags & FLAG_DIRECTORY_SEEN) ? "1" : "0") << "\n";
    os << "order_collision_seen="
       << ((r.flags & FLAG_ORDER_COLLISION_SEEN) ? "1" : "0") << "\n";
    os << "trading_state=" << char_str(r.trading_state) << "\n";
    os << "short_sell_restriction=" << char_str(r.short_sell_restriction)
       << "\n";
    os << "reference_price=" << price_str(r.reference_price) << "\n";
    os << "last_system_event=" << char_str(r.last_system_event) << "\n";
    os << "last_exch_seq=" << r.last_exch_seq << "\n";
    os << "last_update_ns=" << r.last_update_ns << "\n";
    os << "total_bid_qty=" << r.total_bid_qty << "\n";
    os << "total_ask_qty=" << r.total_ask_qty << "\n";
    os << "total_bid_orders=" << r.total_bid_orders << "\n";
    os << "total_ask_orders=" << r.total_ask_orders << "\n";
    os << "last_price=" << price_str(r.last_price) << "\n";
    os << "last_qty=" << r.last_qty << "\n";
    os << "last_match_number=" << r.last_match_number << "\n";
    os << "last_trade_ns=" << r.last_trade_ns << "\n";
    os << "cum_qty=" << r.cum_qty << "\n";
    os << "cum_turnover=" << r.cum_turnover << "\n";
    os << "trade_count=" << r.trade_count << "\n";
}

} // namespace

bool QueryServer::open(int port) {
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        LOG_ERROR(COMP) << "socket: " << std::strerror(errno);
        return false;
    }
    int one = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr),
               sizeof(addr)) != 0) {
        LOG_ERROR(COMP) << "bind(127.0.0.1:" << port
                        << "): " << std::strerror(errno);
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    if (::listen(listen_fd_, 8) != 0) {
        LOG_ERROR(COMP) << "listen: " << std::strerror(errno);
        ::close(listen_fd_);
        listen_fd_ = -1;
        return false;
    }
    set_nonblocking(listen_fd_);
    port_ = port;
    LOG_INFO(COMP) << "query interface on 127.0.0.1:" << port;
    return true;
}

void QueryServer::close_all() {
    for (size_t i = 0; i < conns_.size(); ++i) {
        if (conns_[i].fd >= 0) {
            ::close(conns_[i].fd);
        }
    }
    conns_.clear();
    if (listen_fd_ >= 0) {
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
}

void QueryServer::on_listen_ready() {
    for (;;) {
        int fd = ::accept(listen_fd_, 0, 0);
        if (fd < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                LOG_WARN(COMP) << "accept: " << std::strerror(errno);
            }
            return;
        }
        set_nonblocking(fd);
        Conn c;
        c.fd = fd;
        conns_.push_back(c);
    }
}

void QueryServer::close_conn(size_t idx, const char* why) {
    if (conns_[idx].fd >= 0) {
        ::close(conns_[idx].fd);
        conns_[idx].fd = -1;
        LOG_DEBUG(COMP) << "query client closed (" << why << ")";
    }
}

void QueryServer::reap() {
    size_t w = 0;
    for (size_t i = 0; i < conns_.size(); ++i) {
        if (conns_[i].fd >= 0) {
            if (w != i) {
                conns_[w] = conns_[i];
            }
            ++w;
        }
    }
    conns_.resize(w);
}

void QueryServer::on_conn_readable(size_t idx) {
    Conn& c = conns_[idx];
    char buf[4096];
    for (;;) {
        ssize_t n = ::recv(c.fd, buf, sizeof(buf), 0);
        if (n > 0) {
            c.inbuf.append(buf, static_cast<size_t>(n));
            size_t pos;
            while ((pos = c.inbuf.find('\n')) != std::string::npos) {
                std::string line = c.inbuf.substr(0, pos);
                c.inbuf.erase(0, pos + 1);
                if (!line.empty() && line[line.size() - 1] == '\r') {
                    line.erase(line.size() - 1);
                }
                c.outbuf += respond(line);
            }
            if (c.outbuf.size() > MAX_OUTBUF) {
                LOG_WARN(COMP)
                    << "query client too slow (outbuf > " << MAX_OUTBUF
                    << " bytes) — dropping";
                close_conn(idx, "outbuf overflow");
                return;
            }
            on_conn_writable(idx);
            if (conns_[idx].fd < 0) {
                return;
            }
            continue;
        }
        if (n == 0) {
            close_conn(idx, "EOF");
            return;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return;
        }
        if (errno == EINTR) {
            continue;
        }
        close_conn(idx, "recv error");
        return;
    }
}

void QueryServer::on_conn_writable(size_t idx) {
    Conn& c = conns_[idx];
    while (c.fd >= 0 && !c.outbuf.empty()) {
        ssize_t n = ::send(c.fd, c.outbuf.data(), c.outbuf.size(),
                           MSG_NOSIGNAL);
        if (n > 0) {
            c.outbuf.erase(0, static_cast<size_t>(n));
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return;
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        close_conn(idx, "send error");
        return;
    }
}

void QueryServer::on_conn_error(size_t idx) {
    close_conn(idx, "socket error/hangup");
}

std::string QueryServer::respond(const std::string& line) const {
    std::ostringstream os;
    std::string arg;
    std::string cmd = split_cmd(line, arg);

    if (cmd == "PING") {
        os << "PONG\n";
    } else if (cmd == "STATS") {
        const Meta& m = tables_.meta();
        os << "session=" << m.session << "\n";
        os << "epoch=" << m.epoch << "\n";
        os << "last_exch_seq=" << m.last_exch_seq << "\n";
        os << "updates_applied=" << m.updates_applied << "\n";
        os << "dups_dropped=" << m.dups_dropped << "\n";
        os << "orders_applied=" << m.orders_applied << "\n";
        os << "ticks_applied=" << m.ticks_applied << "\n";
        os << "syncs_completed=" << m.syncs_completed << "\n";
        os << "syncs_discarded=" << m.syncs_discarded << "\n";
        os << "orders_live=" << tables_.orders().size() << "\n";
        os << "books=" << tables_.books().size() << "\n";
        os << "ticks=" << tables_.tick_row_count() << "\n";
        os << "rss_kb=" << rss_kb() << "\n";
    } else if (cmd == "GET" || cmd == "BOOK" || cmd == "ORDERS" ||
               cmd == "TRADES") {
        if (arg.empty()) {
            os << "ERR badcmd\n";
        } else {
            bool found = false;
            if (cmd == "ORDERS") {
                // Live orders for the ticker, sorted by order number (the
                // map iterates sorted). Header always printed if the ticker
                // exists in any table; orders may legitimately be empty.
                bool ticker_known = false;
                for (Tables::BookMap::const_iterator b =
                         tables_.books().begin();
                     b != tables_.books().end(); ++b) {
                    if (b->first.first == arg) {
                        ticker_known = true;
                    }
                }
                std::ostringstream rows;
                size_t nrows = 0;
                for (Tables::OrderMap::const_iterator o =
                         tables_.orders().begin();
                     o != tables_.orders().end(); ++o) {
                    if (arg == o->second.ticker) {
                        rows << o->second.order_number << " "
                             << char_str(o->second.side) << " "
                             << price_str(o->second.price) << " "
                             << o->second.qty_remaining << " "
                             << (o->second.order_type == 'Q' ? "DLP" : "-")
                             << "\n";
                        ++nrows;
                    }
                }
                if (ticker_known || nrows > 0) {
                    os << "order_number side price qty_remaining type\n";
                    os << rows.str();
                    found = true;
                }
            } else {
                for (Tables::BookMap::const_iterator b =
                         tables_.books().begin();
                     b != tables_.books().end(); ++b) {
                    if (b->first.first != arg) {
                        continue;
                    }
                    found = true;
                    const BookRow& r = b->second;
                    if (cmd == "GET") {
                        row_key_values(os, b->first, r);
                    } else if (cmd == "BOOK") {
                        os << "ticker=" << b->first.first
                           << " group=" << b->first.second << "\n";
                        os << "  bid_orders    bid_qty  bid_price | "
                              "ask_price    ask_qty  ask_orders\n";
                        int depth = r.level_count_bid > r.level_count_ask
                                        ? r.level_count_bid
                                        : r.level_count_ask;
                        for (int i = 0; i < depth; ++i) {
                            char lbuf[160];
                            std::string bp, ap;
                            std::string bq, bo, aq, ao;
                            if (i < r.level_count_bid) {
                                bp = price_str(r.bids[i].price);
                                char t[32];
                                std::snprintf(t, sizeof(t), "%u",
                                              r.bids[i].qty);
                                bq = t;
                                std::snprintf(t, sizeof(t), "%u",
                                              r.bids[i].order_count);
                                bo = t;
                            }
                            if (i < r.level_count_ask) {
                                ap = price_str(r.asks[i].price);
                                char t[32];
                                std::snprintf(t, sizeof(t), "%u",
                                              r.asks[i].qty);
                                aq = t;
                                std::snprintf(t, sizeof(t), "%u",
                                              r.asks[i].order_count);
                                ao = t;
                            }
                            std::snprintf(lbuf, sizeof(lbuf),
                                          "  %10s %10s %10s | %-10s %10s %10s\n",
                                          bo.c_str(), bq.c_str(), bp.c_str(),
                                          ap.c_str(), aq.c_str(), ao.c_str());
                            os << lbuf;
                        }
                        os << "  totals: bid_qty=" << r.total_bid_qty
                           << " ask_qty=" << r.total_ask_qty
                           << " bid_orders=" << r.total_bid_orders
                           << " ask_orders=" << r.total_ask_orders << "\n";
                    } else {  // TRADES
                        os << "ticker=" << b->first.first
                           << " group=" << b->first.second << "\n";
                        os << "last_price=" << price_str(r.last_price)
                           << " last_qty=" << r.last_qty
                           << " last_match_number=" << r.last_match_number
                           << " last_trade_ns=" << r.last_trade_ns << "\n";
                        os << "cum_qty=" << r.cum_qty
                           << " cum_turnover=" << r.cum_turnover
                           << " trade_count=" << r.trade_count << "\n";
                        os << "tape (newest first, up to " << TAPE_CAP
                           << "):\n";
                        for (std::deque<TapeEntry>::const_reverse_iterator t =
                                 r.tape.rbegin();
                             t != r.tape.rend(); ++t) {
                            os << "  " << t->ns << " "
                               << price_str(t->price) << " " << t->qty
                               << " " << t->match_number << "\n";
                        }
                    }
                }
            }
            if (!found) {
                os.str("");
                os << "ERR unknown\n";
            }
        }
    } else if (cmd == "TABLE") {
        if (arg == "static") {
            os << "ticker,group,isin,round_lot,tick_table_id,price_decimals,"
                  "upper_limit,lower_limit,directory_seen\n";
            for (Tables::BookMap::const_iterator b = tables_.books().begin();
                 b != tables_.books().end(); ++b) {
                const BookRow& r = b->second;
                os << b->first.first << "," << b->first.second << ","
                   << r.isin << "," << r.round_lot << "," << r.tick_table_id
                   << "," << static_cast<unsigned>(r.price_decimals) << ","
                   << r.upper_limit << "," << r.lower_limit << ","
                   << ((r.flags & FLAG_DIRECTORY_SEEN) ? 1 : 0) << "\n";
            }
        } else if (arg == "state") {
            os << "ticker,group,trading_state,short_sell_restriction,"
                  "reference_price,last_system_event,last_exch_seq,"
                  "last_update_ns\n";
            for (Tables::BookMap::const_iterator b = tables_.books().begin();
                 b != tables_.books().end(); ++b) {
                const BookRow& r = b->second;
                os << b->first.first << "," << b->first.second << ","
                   << char_str(r.trading_state) << ","
                   << char_str(r.short_sell_restriction) << ","
                   << r.reference_price << ","
                   << char_str(r.last_system_event) << "," << r.last_exch_seq
                   << "," << r.last_update_ns << "\n";
            }
        } else if (arg == "trades") {
            os << "ticker,group,last_price,last_qty,last_match_number,"
                  "last_trade_ns,cum_qty,cum_turnover,trade_count\n";
            for (Tables::BookMap::const_iterator b = tables_.books().begin();
                 b != tables_.books().end(); ++b) {
                const BookRow& r = b->second;
                os << b->first.first << "," << b->first.second << ","
                   << r.last_price << "," << r.last_qty << ","
                   << r.last_match_number << "," << r.last_trade_ns << ","
                   << r.cum_qty << "," << r.cum_turnover << ","
                   << r.trade_count << "\n";
            }
        } else {
            os << "ERR badcmd\n";
        }
    } else {
        os << "ERR badcmd\n";
    }

    os << ".\n";
    return os.str();
}

} // namespace jnx
