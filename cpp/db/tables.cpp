// tables.cpp — see tables.h.
#include "db/tables.h"

#include <cstring>

#include "common/log.h"

namespace jnx {

namespace {
const char* COMP = "jnxdb.tables";
}

BookRow::BookRow()
    : round_lot(0),
      tick_table_id(0),
      price_decimals(0),
      upper_limit(0),
      lower_limit(0),
      flags(0),
      trading_state('\0'),
      short_sell_restriction('\0'),
      reference_price(0),
      last_system_event('\0'),
      short_sell_price(0),
      last_exch_seq(0),
      last_update_ns(0),
      level_count_bid(0),
      level_count_ask(0),
      total_bid_qty(0),
      total_ask_qty(0),
      total_bid_orders(0),
      total_ask_orders(0),
      last_price(0),
      last_qty(0),
      last_match_number(0),
      last_trade_ns(0),
      cum_qty(0),
      cum_turnover(0),
      trade_count(0) {}

Meta::Meta()
    : last_exch_seq(0),
      epoch(0),
      updates_applied(0),
      dups_dropped(0),
      orders_applied(0),
      ticks_applied(0),
      syncs_completed(0),
      syncs_discarded(0) {}

bool Tables::apply_update(const UpdateRecord& rec, bool in_sync) {
    if (!in_sync) {
        // Dup guard (safety net — impossible in normal operation): same
        // epoch AND not newer than what we already applied -> drop.
        if (rec.epoch == meta_.epoch && rec.exch_seq <= meta_.last_exch_seq &&
            meta_.updates_applied > 0) {
            ++meta_.dups_dropped;
            if (meta_.dups_dropped == 1 || meta_.dups_dropped % 1000 == 0) {
                LOG_WARN(COMP) << "duplicate UPDATE dropped (epoch="
                               << rec.epoch << " exch_seq=" << rec.exch_seq
                               << " <= last=" << meta_.last_exch_seq
                               << "), total dropped=" << meta_.dups_dropped;
            }
            return false;
        }
    }

    Key key(rec.ticker, rec.group);
    BookRow& row = books_[key];

    // T1 static — wholesale
    row.isin = rec.isin;
    row.round_lot = rec.round_lot;
    row.tick_table_id = rec.tick_table_id;
    row.price_decimals = rec.price_decimals;
    row.upper_limit = rec.upper_limit;
    row.lower_limit = rec.lower_limit;
    row.flags = rec.flags;

    // T2 state — wholesale (last_exch_seq/last_update_ns from envelope)
    row.trading_state = rec.trading_state;
    row.short_sell_restriction = rec.short_sell_restriction;
    row.reference_price = rec.reference_price;
    row.last_system_event = rec.last_system_event;
    row.short_sell_price = rec.short_sell_price;
    row.last_exch_seq = rec.exch_seq;
    row.last_update_ns = rec.exch_ns;

    // T4 book_agg — wholesale
    row.level_count_bid = rec.level_count_bid;
    row.level_count_ask = rec.level_count_ask;
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        row.bids[i] = rec.bids[i];
        row.asks[i] = rec.asks[i];
    }
    row.total_bid_qty = rec.total_bid_qty;
    row.total_ask_qty = rec.total_ask_qty;
    row.total_bid_orders = rec.total_bid_orders;
    row.total_ask_orders = rec.total_ask_orders;

    // T5 trades summary — wholesale
    row.last_price = rec.last_price;
    row.last_qty = rec.last_qty;
    row.last_match_number = rec.last_match_number;
    row.last_trade_ns = rec.last_trade_ns;
    row.cum_qty = rec.cum_qty;
    row.cum_turnover = rec.cum_turnover;
    row.trade_count = rec.trade_count;

    // T5 tape ring — one entry per execution trigger.
    if (rec.trigger == 'E') {
        TapeEntry t;
        t.ns = rec.last_trade_ns;
        t.price = rec.last_price;
        t.qty = rec.last_qty;
        t.match_number = rec.last_match_number;
        row.tape.push_back(t);
        while (row.tape.size() > TAPE_CAP) {
            row.tape.pop_front();
        }
    }

    // T3 orders — delta section
    apply_delta(rec);

    ++meta_.updates_applied;
    if (!in_sync) {
        meta_.session = rec.session;
        meta_.epoch = rec.epoch;
        meta_.last_exch_seq = rec.exch_seq;
    }
    return true;
}

void Tables::apply_delta(const UpdateRecord& rec) {
    switch (rec.delta_op) {
        case 'A': {
            OrderRecord o;
            o.order_number = rec.delta_order_number;
            std::strncpy(o.ticker, rec.ticker, sizeof(o.ticker) - 1);
            o.ticker[sizeof(o.ticker) - 1] = '\0';
            std::strncpy(o.group, rec.group, sizeof(o.group) - 1);
            o.group[sizeof(o.group) - 1] = '\0';
            o.side = rec.delta_side;
            o.price = rec.delta_price;
            o.qty_remaining = rec.delta_qty;
            o.order_type = rec.delta_order_type;
            orders_[o.order_number] = o;
            break;
        }
        case 'E': {
            OrderMap::iterator it = orders_.find(rec.delta_order_number);
            if (it == orders_.end()) {
                break;  // unknown order — count-free ignore (sync rows may
                        // legitimately race dumps; the FH is authoritative)
            }
            if (rec.delta_qty == 0) {
                orders_.erase(it);
            } else {
                it->second.qty_remaining = rec.delta_qty;
                it->second.price = rec.delta_price;
            }
            break;
        }
        case 'D':
            orders_.erase(rec.delta_order_number);
            break;
        case 'U': {
            orders_.erase(rec.delta_orig_order_number);
            OrderRecord o;
            o.order_number = rec.delta_order_number;
            std::strncpy(o.ticker, rec.ticker, sizeof(o.ticker) - 1);
            o.ticker[sizeof(o.ticker) - 1] = '\0';
            std::strncpy(o.group, rec.group, sizeof(o.group) - 1);
            o.group[sizeof(o.group) - 1] = '\0';
            o.side = rec.delta_side;
            o.price = rec.delta_price;
            o.qty_remaining = rec.delta_qty;
            o.order_type = rec.delta_order_type;
            orders_[o.order_number] = o;
            break;
        }
        case '#':
        default:
            break;  // no order mutation
    }
}

void Tables::apply_order(const OrderRecord& rec) {
    orders_[rec.order_number] = rec;
    ++meta_.orders_applied;
}

void Tables::apply_tick(const TickRecord& rec) {
    ticks_[rec.table_id][rec.price_start] = rec.tick_size;
    ++meta_.ticks_applied;
}

void Tables::adopt_meta(const SyncEndRecord& rec) {
    meta_.session = rec.session;
    meta_.last_exch_seq = rec.last_exch_seq;
    meta_.epoch = rec.epoch;
}

void Tables::reset() {
    books_.clear();
    orders_.clear();
    ticks_.clear();
    Meta fresh;
    // Preserve lifetime counters across a reset? No: RESET means "wipe all
    // tables"; counters describe the current epoch of data. Keep only the
    // sync counters so a discarded partial sync remains observable.
    fresh.syncs_completed = meta_.syncs_completed;
    fresh.syncs_discarded = meta_.syncs_discarded;
    meta_ = fresh;
}

size_t Tables::tick_row_count() const {
    size_t n = 0;
    for (TickMap::const_iterator it = ticks_.begin(); it != ticks_.end();
         ++it) {
        n += it->second.size();
    }
    return n;
}

UpdateRecord Tables::make_dump_update(const Key& key,
                                      const BookRow& row) const {
    UpdateRecord u;
    u.epoch = meta_.epoch;
    u.pub_seq = 0;  // dump rows are not publications
    std::strncpy(u.session, meta_.session.c_str(), sizeof(u.session) - 1);
    u.session[sizeof(u.session) - 1] = '\0';
    u.exch_seq = row.last_exch_seq;
    u.exch_ns = row.last_update_ns;
    u.trigger = '#';
    std::strncpy(u.ticker, key.first.c_str(), sizeof(u.ticker) - 1);
    u.ticker[sizeof(u.ticker) - 1] = '\0';
    std::strncpy(u.group, key.second.c_str(), sizeof(u.group) - 1);
    u.group[sizeof(u.group) - 1] = '\0';

    std::strncpy(u.isin, row.isin.c_str(), sizeof(u.isin) - 1);
    u.isin[sizeof(u.isin) - 1] = '\0';
    u.round_lot = row.round_lot;
    u.tick_table_id = row.tick_table_id;
    u.price_decimals = row.price_decimals;
    u.upper_limit = row.upper_limit;
    u.lower_limit = row.lower_limit;
    u.flags = row.flags;

    u.trading_state = row.trading_state;
    u.short_sell_restriction = row.short_sell_restriction;
    u.reference_price = row.reference_price;
    u.last_system_event = row.last_system_event;
    u.short_sell_price = row.short_sell_price;

    u.level_count_bid = row.level_count_bid;
    u.level_count_ask = row.level_count_ask;
    for (int i = 0; i < BOOK_DEPTH; ++i) {
        u.bids[i] = row.bids[i];
        u.asks[i] = row.asks[i];
    }
    u.total_bid_qty = row.total_bid_qty;
    u.total_ask_qty = row.total_ask_qty;
    u.total_bid_orders = row.total_bid_orders;
    u.total_ask_orders = row.total_ask_orders;

    u.last_price = row.last_price;
    u.last_qty = row.last_qty;
    u.last_match_number = row.last_match_number;
    u.last_trade_ns = row.last_trade_ns;
    u.cum_qty = row.cum_qty;
    u.cum_turnover = row.cum_turnover;
    u.trade_count = row.trade_count;

    u.delta_op = '#';  // all other delta fields stay zero
    return u;
}

void Tables::dump_state(
    const std::function<void(const unsigned char*, size_t)>& sink) const {
    unsigned char buf[MAX_RECORD_WIRE_SIZE];

    sink(buf, encode_control(KIND_SYNC_BEGIN, buf));

    for (TickMap::const_iterator t = ticks_.begin(); t != ticks_.end(); ++t) {
        for (std::map<uint32_t, uint32_t>::const_iterator r =
                 t->second.begin();
             r != t->second.end(); ++r) {
            TickRecord tick;
            tick.table_id = t->first;
            tick.price_start = r->first;
            tick.tick_size = r->second;
            sink(buf, encode_tick(tick, buf));
        }
    }

    for (OrderMap::const_iterator o = orders_.begin(); o != orders_.end();
         ++o) {
        sink(buf, encode_order(o->second, buf));
    }

    for (BookMap::const_iterator b = books_.begin(); b != books_.end(); ++b) {
        UpdateRecord u = make_dump_update(b->first, b->second);
        sink(buf, encode_update(u, buf));
    }

    SyncEndRecord se;
    std::strncpy(se.session, meta_.session.c_str(), sizeof(se.session) - 1);
    se.session[sizeof(se.session) - 1] = '\0';
    se.last_exch_seq = meta_.last_exch_seq;
    se.epoch = meta_.epoch;
    sink(buf, encode_sync_end(se, buf));
}

} // namespace jnx
