// publish.cpp — see publish.h.
#include "fh/publish.h"

#include <arpa/inet.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include <cstring>

#include "common/log.h"

namespace jnx {

static const char* COMP = "publish";

// --- PubContext --------------------------------------------------------------

void PubContext::note(const ApplyResult& res, uint64_t exch_ns) {
    // S events go through note_event (ApplyResult lacks the event char).
    if (res.has_trade) {
        last_trade_ns[res.trade.orderbook_id] = exch_ns;
    }
}

char PubContext::event_for(const std::string& group) const {
    std::map<std::string, char>::const_iterator it =
        last_sys_event.find(group);
    if (it != last_sys_event.end()) {
        return it->second;
    }
    it = last_sys_event.find(std::string());
    if (it != last_sys_event.end()) {
        return it->second;
    }
    return '\0';
}

// --- make_update ---------------------------------------------------------------

UpdateRecord make_update(const Market& market, const PubContext& ctx,
                         const ApplyResult& res, const Envelope& env) {
    UpdateRecord rec;
    rec.epoch = env.epoch;
    rec.pub_seq = env.pub_seq;
    std::snprintf(rec.session, sizeof(rec.session), "%s",
                  env.session.c_str());
    rec.exch_seq = env.exch_seq;
    rec.exch_ns = env.exch_ns;
    rec.trigger = env.trigger;
    std::snprintf(rec.ticker, sizeof(rec.ticker), "%s", res.ticker.c_str());

    // Group: prefer the message's group; fall back to refdata's record.
    std::string group = res.group;

    const std::map<std::string, Instrument>& insts =
        market.refdata.instruments();
    std::map<std::string, Instrument>::const_iterator ii =
        res.ticker.empty() ? insts.end() : insts.find(res.ticker);
    if (ii != insts.end()) {
        const Instrument& inst = ii->second;
        if (group.empty()) {
            group = inst.group;
        }
        std::snprintf(rec.isin, sizeof(rec.isin), "%s", inst.isin.c_str());
        if (!inst.directory_missing) {
            rec.flags |= FLAG_DIRECTORY_SEEN;
            rec.round_lot = static_cast<uint32_t>(inst.round_lot);
            rec.tick_table_id = static_cast<uint32_t>(inst.tick_table_id);
            rec.price_decimals = static_cast<uint8_t>(inst.price_decimals);
            rec.upper_limit = static_cast<uint32_t>(inst.upper_limit);
            rec.lower_limit = static_cast<uint32_t>(inst.lower_limit);
        }
        rec.trading_state = inst.trading_state;
        rec.short_sell_restriction = inst.short_sell_state;
        rec.reference_price = inst.reference_price >= 0
                                  ? static_cast<uint32_t>(inst.reference_price)
                                  : 0;

        // Short Sell Price (SSP): the minimum accepted short-sell order
        // price, computed from JNX's restriction flag (never a price
        // itself) plus this book's own last-two-trades tick classification
        // (see refdata.h compute_ssp() / tape.h BookStats::uptick).
        const std::map<std::string, BookStats>& stats = market.tape.stats();
        std::map<std::string, BookStats>::const_iterator si =
            stats.find(res.ticker);
        int64_t last_price = si != stats.end() ? si->second.last_price : -1;
        bool has_last = si != stats.end() && si->second.has_last;
        bool uptick = si != stats.end() && si->second.uptick;
        const TickTable* ticks = NULL;
        if (inst.tick_table_id >= 0) {
            std::map<uint32_t, TickTable>::const_iterator ti =
                market.refdata.tick_tables().find(
                    static_cast<uint32_t>(inst.tick_table_id));
            if (ti != market.refdata.tick_tables().end()) {
                ticks = &ti->second;
            }
        }
        rec.short_sell_price =
            compute_ssp(inst.short_sell_state, inst.reference_price,
                       last_price, has_last, uptick, ticks);
    } else {
        // No refdata record at all: unknown-yet states ('?'), zero statics.
        rec.trading_state = '?';
        rec.short_sell_restriction = '?';
        rec.reference_price = 0;
        rec.short_sell_price = NO_PRICE;
    }
    std::snprintf(rec.group, sizeof(rec.group), "%s", group.c_str());
    if (market.books.collisions > 0) {
        rec.flags |= FLAG_ORDER_COLLISION_SEEN;
    }
    rec.last_system_event = ctx.event_for(group);

    // Book section.
    const std::map<std::string, Book>& books = market.books.books();
    std::map<std::string, Book>::const_iterator bi =
        res.ticker.empty() ? books.end() : books.find(res.ticker);
    if (bi != books.end()) {
        const SideLevels& bids = bi->second.bids();
        const SideLevels& asks = bi->second.asks();
        int n = 0;
        for (SideLevels::LevelMap::const_reverse_iterator it =
                 bids.ascending().rbegin();
             it != bids.ascending().rend() && n < BOOK_DEPTH; ++it, ++n) {
            rec.bids[n].price = it->first;
            rec.bids[n].qty = static_cast<uint32_t>(it->second.qty);
            rec.bids[n].order_count = it->second.orders;
        }
        rec.level_count_bid = static_cast<uint8_t>(n);
        n = 0;
        for (SideLevels::LevelMap::const_iterator it =
                 asks.ascending().begin();
             it != asks.ascending().end() && n < BOOK_DEPTH; ++it, ++n) {
            rec.asks[n].price = it->first;
            rec.asks[n].qty = static_cast<uint32_t>(it->second.qty);
            rec.asks[n].order_count = it->second.orders;
        }
        rec.level_count_ask = static_cast<uint8_t>(n);
        rec.total_bid_qty = bids.total_qty();
        rec.total_ask_qty = asks.total_qty();
        rec.total_bid_orders = bids.total_orders();
        rec.total_ask_orders = asks.total_orders();
    }

    // Trade summary.
    const std::map<std::string, BookStats>& stats = market.tape.stats();
    std::map<std::string, BookStats>::const_iterator ti =
        res.ticker.empty() ? stats.end() : stats.find(res.ticker);
    if (ti != stats.end()) {
        const BookStats& s = ti->second;
        rec.last_price =
            s.last_price >= 0 ? static_cast<uint32_t>(s.last_price) : 0;
        rec.last_qty = s.last_qty >= 0 ? static_cast<uint32_t>(s.last_qty) : 0;
        rec.last_match_number = s.last_match_number;
        rec.cum_qty = s.volume;
        rec.cum_turnover = s.notional;
        rec.trade_count = static_cast<uint32_t>(s.trade_count);
        std::map<std::string, uint64_t>::const_iterator li =
            ctx.last_trade_ns.find(res.ticker);
        rec.last_trade_ns = li != ctx.last_trade_ns.end() ? li->second : 0;
    }

    // Delta section.
    if (res.has_delta) {
        rec.delta_op = res.delta_op == 'F' ? 'A' : res.delta_op;
        rec.delta_order_number = res.order_number;
        rec.delta_orig_order_number = res.orig_order_number;
        rec.delta_side = res.side;
        rec.delta_order_type = res.order_type;
        if (res.delta_op == 'D') {
            rec.delta_price = 0;
            rec.delta_qty = 0;
        } else {
            rec.delta_price = res.price;
            // Remaining qty AFTER the op (spec): for A/F/U the inserted
            // order's qty == res.qty; for E look the order up (gone = 0).
            if (res.delta_op == 'E') {
                std::unordered_map<uint64_t, Order>::const_iterator oi =
                    market.books.orders().find(res.order_number);
                rec.delta_qty = oi != market.books.orders().end()
                                    ? oi->second.remaining_qty
                                    : 0;
            } else {
                rec.delta_qty = res.qty;
            }
        }
    } else {
        rec.delta_op = '#';
    }
    return rec;
}

// --- build_sync_dump -------------------------------------------------------------

void build_sync_dump(const Market& market, const PubContext& ctx,
                     const std::string& session, uint64_t last_exch_seq,
                     uint64_t epoch, std::vector<unsigned char>& out) {
    unsigned char buf[MAX_RECORD_WIRE_SIZE];
    size_t n = encode_control(KIND_SYNC_BEGIN, buf);
    out.insert(out.end(), buf, buf + n);

    // TICK rows.
    const std::map<uint32_t, TickTable>& tts = market.refdata.tick_tables();
    for (std::map<uint32_t, TickTable>::const_iterator t = tts.begin();
         t != tts.end(); ++t) {
        for (std::map<uint32_t, uint32_t>::const_iterator r =
                 t->second.rows().begin();
             r != t->second.rows().end(); ++r) {
            TickRecord tick;
            tick.table_id = t->first;
            tick.price_start = r->first;
            tick.tick_size = r->second;
            n = encode_tick(tick, buf);
            out.insert(out.end(), buf, buf + n);
        }
    }

    // ORDER rows (sorted by order number for determinism).
    std::map<uint64_t, const Order*> sorted_orders;
    for (std::unordered_map<uint64_t, Order>::const_iterator o =
             market.books.orders().begin();
         o != market.books.orders().end(); ++o) {
        sorted_orders[o->first] = &o->second;
    }
    for (std::map<uint64_t, const Order*>::const_iterator o =
             sorted_orders.begin();
         o != sorted_orders.end(); ++o) {
        OrderRecord orec;
        orec.order_number = o->first;
        std::snprintf(orec.ticker, sizeof(orec.ticker), "%s",
                      o->second->orderbook_id.c_str());
        std::snprintf(orec.group, sizeof(orec.group), "%s",
                      o->second->group.c_str());
        orec.side = o->second->side;
        orec.price = o->second->price;
        orec.qty_remaining = o->second->remaining_qty;
        orec.order_type = o->second->order_type;
        n = encode_order(orec, buf);
        out.insert(out.end(), buf, buf + n);
    }

    // One '#' UPDATE per known ticker: union of refdata instruments and
    // order books (either may know tickers the other does not).
    std::map<std::string, bool> tickers;
    for (std::map<std::string, Instrument>::const_iterator i =
             market.refdata.instruments().begin();
         i != market.refdata.instruments().end(); ++i) {
        tickers[i->first] = true;
    }
    for (std::map<std::string, Book>::const_iterator b =
             market.books.books().begin();
         b != market.books.books().end(); ++b) {
        tickers[b->first] = true;
    }
    Envelope env;
    env.epoch = epoch;
    env.pub_seq = 0;
    env.session = session;
    env.exch_seq = last_exch_seq;
    env.exch_ns = market.seconds * 1000000000ULL;
    env.trigger = '#';
    for (std::map<std::string, bool>::const_iterator t = tickers.begin();
         t != tickers.end(); ++t) {
        ApplyResult res;
        res.ticker = t->first;
        // group resolved inside make_update via refdata; if unknown there,
        // take it from any live order of this ticker.
        std::map<std::string, Instrument>::const_iterator ii =
            market.refdata.instruments().find(t->first);
        bool group_known = ii != market.refdata.instruments().end() &&
                           !ii->second.group.empty();
        if (!group_known) {
            for (std::map<uint64_t, const Order*>::const_iterator o =
                     sorted_orders.begin();
                 o != sorted_orders.end(); ++o) {
                if (o->second->orderbook_id == t->first) {
                    res.group = o->second->group;
                    group_known = true;
                    break;
                }
            }
        }
        if (!group_known) {
            // Neither refdata nor any currently-live order can tell us
            // this ticker's group (it was only ever touched by orders
            // that have since fully closed, with no directory/state
            // record ever seen -- an inherent Market/refdata blind spot
            // shared with the Python prototype, see refdata.cpp: group is
            // backfilled on H/Y/S/reference-price-A but not on plain
            // order adds, matching jnxfeed/book/refdata.py exactly).
            // Emitting a '#' row with an empty group would create a
            // SECOND, wrongly-keyed DB row for this ticker (empty group)
            // alongside whatever correctly-grouped row a later live
            // UPDATE creates -- a duplicate-key bug, not a legitimate
            // system-wide pseudo-row. Skip the dump row instead: if the
            // ticker is ever touched live again, that message carries
            // its own correct group and (re)creates the row properly;
            // if it never is, its (already-published, DB-authoritative)
            // prior state is lost on this resync, same as any other
            // full RESET+SYNC — documented in RECOVERY.md.
            continue;
        }
        UpdateRecord rec = make_update(market, ctx, res, env);
        n = encode_update(rec, buf);
        out.insert(out.end(), buf, buf + n);
    }

    // Mirror the ticker="" system-wide pseudo-rows a live 'S' message
    // produces (Market::apply's 'S' case leaves res.ticker blank and
    // sets res.group = msg.group, "" meaning system-wide): one per
    // group this session has ever logged a system event for. These are
    // NOT reachable via refdata instruments or order books, so without
    // this loop a resync silently drops them (an earlier, now-fixed
    // version of this function also dropped groups with unresolvable
    // per-ticker group info the same way -- see the loop above).
    for (std::map<std::string, char>::const_iterator e =
             ctx.last_sys_event.begin();
         e != ctx.last_sys_event.end(); ++e) {
        ApplyResult res;
        res.ticker = "";
        res.group = e->first;
        UpdateRecord rec = make_update(market, ctx, res, env);
        n = encode_update(rec, buf);
        out.insert(out.end(), buf, buf + n);
    }

    SyncEndRecord end;
    std::snprintf(end.session, sizeof(end.session), "%s", session.c_str());
    end.last_exch_seq = last_exch_seq;
    end.epoch = epoch;
    n = encode_sync_end(end, buf);
    out.insert(out.end(), buf, buf + n);
}

// --- DbLink --------------------------------------------------------------------

bool DbLink::connect_hello(const std::string& sock_path, uint64_t my_epoch,
                           uint64_t my_last_seq, HelloRecord& db_hello) {
    close();
    fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd_ < 0) {
        return false;
    }
    struct timeval tv;
    tv.tv_sec = 5;
    tv.tv_usec = 0;
    ::setsockopt(fd_, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_un addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s",
                  sock_path.c_str());
    if (::connect(fd_, reinterpret_cast<struct sockaddr*>(&addr),
                  sizeof(addr)) != 0) {
        close();
        return false;
    }

    HelloRecord mine;
    mine.epoch = my_epoch;
    mine.last_exch_seq = my_last_seq;
    unsigned char buf[HELLO_WIRE_SIZE];
    size_t n = encode_hello(mine, buf);
    if (!send(buf, n)) {
        return false;
    }

    // Read the DB's HELLO reply.
    unsigned char rbuf[HELLO_WIRE_SIZE];
    size_t got = 0;
    while (got < HELLO_WIRE_SIZE) {
        ssize_t r = ::recv(fd_, rbuf + got, HELLO_WIRE_SIZE - got, 0);
        if (r <= 0) {
            close();
            return false;
        }
        got += static_cast<size_t>(r);
    }
    const char* err = NULL;
    if (!decode_hello(rbuf, HELLO_WIRE_SIZE, db_hello, &err)) {
        LOG_WARN(COMP) << "bad HELLO reply from db: " << (err ? err : "?");
        close();
        return false;
    }
    return true;
}

void DbLink::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

bool DbLink::send(const unsigned char* data, size_t len) {
    if (fd_ < 0) {
        return false;
    }
    size_t off = 0;
    while (off < len) {
        ssize_t n = ::send(fd_, data + off, len - off, MSG_NOSIGNAL);
        if (n > 0) {
            off += static_cast<size_t>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        LOG_WARN(COMP) << "db write failed (" << std::strerror(errno)
                       << "); marking db disconnected";
        close();
        return false;
    }
    return true;
}

bool DbLink::get_state(const std::function<void(const RawRecord&)>& cb) {
    unsigned char buf[CONTROL_WIRE_SIZE];
    size_t n = encode_control(KIND_GET_STATE, buf);
    if (!send(buf, n)) {
        return false;
    }
    RecordFramer framer;
    unsigned char rbuf[65536];
    for (;;) {
        ssize_t r = ::recv(fd_, rbuf, sizeof(rbuf), 0);
        if (r <= 0) {
            LOG_WARN(COMP) << "db recovery stream ended prematurely";
            close();
            return false;
        }
        framer.feed(rbuf, static_cast<size_t>(r));
        RawRecord rec;
        while (framer.next(rec)) {
            cb(rec);
            if (rec.kind == KIND_SYNC_END) {
                return true;
            }
        }
        if (framer.corrupt()) {
            LOG_ERROR(COMP) << "db recovery stream corrupt: "
                            << framer.corrupt_reason();
            close();
            return false;
        }
    }
}

// --- McastSender ----------------------------------------------------------------

McastSender::~McastSender() {
    if (fd_ >= 0) {
        ::close(fd_);
    }
}

bool McastSender::open(const std::string& group, int port, int ttl,
                       const std::string& iface) {
    fd_ = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (fd_ < 0) {
        LOG_ERROR(COMP) << "mcast socket: " << std::strerror(errno);
        return false;
    }
    unsigned char ttl_v = static_cast<unsigned char>(ttl);
    ::setsockopt(fd_, IPPROTO_IP, IP_MULTICAST_TTL, &ttl_v, sizeof(ttl_v));
    unsigned char loop = 1; // required for same-host testing
    ::setsockopt(fd_, IPPROTO_IP, IP_MULTICAST_LOOP, &loop, sizeof(loop));
    if (!iface.empty()) {
        struct in_addr ifaddr;
        if (::inet_pton(AF_INET, iface.c_str(), &ifaddr) != 1) {
            LOG_ERROR(COMP) << "bad mcast interface address: " << iface;
            ::close(fd_);
            fd_ = -1;
            return false;
        }
        ::setsockopt(fd_, IPPROTO_IP, IP_MULTICAST_IF, &ifaddr,
                     sizeof(ifaddr));
    }
    std::memset(&dest_, 0, sizeof(dest_));
    dest_.sin_family = AF_INET;
    dest_.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, group.c_str(), &dest_.sin_addr) != 1) {
        LOG_ERROR(COMP) << "bad mcast group address: " << group;
        ::close(fd_);
        fd_ = -1;
        return false;
    }
    LOG_INFO(COMP) << "multicasting to " << group << ":" << port << " ttl "
                   << ttl << (iface.empty() ? "" : " via ") << iface;
    return true;
}

void McastSender::send(const unsigned char* data, size_t len) {
    if (fd_ < 0) {
        ++send_errors_;
        return;
    }
    ssize_t n = ::sendto(fd_, data, len, 0,
                         reinterpret_cast<struct sockaddr*>(&dest_),
                         sizeof(dest_));
    if (n != static_cast<ssize_t>(len)) {
        ++send_errors_;
    }
}

} // namespace jnx
