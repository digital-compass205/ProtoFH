// recover.cpp — see recover.h.
#include "fh/recover.h"

#include <cstring>

#include "common/log.h"
#include "wire/record.h"

namespace jnx {

static const char* COMP = "recover";

namespace {

struct RecoveryState {
    Market* market;
    PubContext* ctx;
    RecoveredMeta* meta;
    bool saw_begin;
    bool saw_end;
    uint64_t orders;
    uint64_t ticks;
    uint64_t rows;
    bool error;

    RecoveryState()
        : market(NULL), ctx(NULL), meta(NULL), saw_begin(false),
          saw_end(false), orders(0), ticks(0), rows(0), error(false) {}
};

void apply_record(RecoveryState& st, const RawRecord& raw) {
    const unsigned char* body = raw.body.empty() ? NULL : &raw.body[0];
    // Re-assemble a full wire record for the typed decoders (they take
    // header + body). Cheap and keeps one decode path.
    unsigned char wire[MAX_RECORD_WIRE_SIZE];
    const char* err = NULL;
    switch (raw.kind) {
        case KIND_SYNC_BEGIN:
            st.saw_begin = true;
            return;
        case KIND_TICK: {
            TickRecord t;
            size_t n = RECORD_HEADER_SIZE + raw.body.size();
            encode_tick(t, wire); // writes a valid header
            std::memcpy(wire + RECORD_HEADER_SIZE, body, raw.body.size());
            if (!decode_tick(wire, n, t, &err)) {
                st.error = true;
                return;
            }
            st.market->restore_tick(t.table_id, t.price_start, t.tick_size);
            ++st.ticks;
            return;
        }
        case KIND_ORDER: {
            OrderRecord o;
            size_t n = RECORD_HEADER_SIZE + raw.body.size();
            encode_order(o, wire);
            std::memcpy(wire + RECORD_HEADER_SIZE, body, raw.body.size());
            if (!decode_order(wire, n, o, &err)) {
                st.error = true;
                return;
            }
            st.market->restore_order(o.order_number, o.ticker, o.group,
                                     o.side, o.price, o.qty_remaining,
                                     o.order_type);
            ++st.orders;
            return;
        }
        case KIND_UPDATE: {
            UpdateRecord u;
            size_t n = RECORD_HEADER_SIZE + raw.body.size();
            encode_update(u, wire);
            std::memcpy(wire + RECORD_HEADER_SIZE, body, raw.body.size());
            if (!decode_update(wire, n, u, &err)) {
                st.error = true;
                return;
            }
            std::string ticker(u.ticker);
            std::string group(u.group);
            if (ticker.empty()) {
                return; // S/L pseudo-rows: nothing per-ticker to restore
            }
            bool directory_seen = (u.flags & FLAG_DIRECTORY_SEEN) != 0;
            bool any_state = u.trading_state != '?' ||
                             u.short_sell_restriction != '?' ||
                             u.reference_price != 0;
            if (directory_seen || any_state) {
                st.market->restore_instrument(
                    ticker, group, u.isin, u.round_lot, u.tick_table_id,
                    u.price_decimals, u.upper_limit, u.lower_limit,
                    directory_seen, u.trading_state,
                    u.short_sell_restriction,
                    u.reference_price != 0
                        ? static_cast<int64_t>(u.reference_price)
                        : -1);
            }
            if (u.trade_count > 0) {
                st.market->restore_trades(
                    ticker, u.trade_count, u.cum_qty, u.cum_turnover,
                    static_cast<int64_t>(u.last_price),
                    static_cast<int64_t>(u.last_qty), u.last_match_number);
                if (u.last_trade_ns != 0) {
                    st.ctx->last_trade_ns[ticker] = u.last_trade_ns;
                }
            }
            if (u.last_system_event != '\0') {
                st.ctx->note_event(group, u.last_system_event);
            }
            ++st.rows;
            return;
        }
        case KIND_SYNC_END: {
            SyncEndRecord e;
            size_t n = RECORD_HEADER_SIZE + raw.body.size();
            encode_sync_end(e, wire);
            std::memcpy(wire + RECORD_HEADER_SIZE, body, raw.body.size());
            if (!decode_sync_end(wire, n, e, &err)) {
                st.error = true;
                return;
            }
            st.meta->session = e.session;
            st.meta->last_exch_seq = e.last_exch_seq;
            st.meta->epoch = e.epoch;
            st.saw_end = true;
            return;
        }
        default:
            LOG_WARN(COMP) << "unexpected record kind '" << raw.kind
                           << "' in recovery stream (ignored)";
            return;
    }
}

} // namespace

bool recover_from_db(DbLink& db, Market& market, PubContext& ctx,
                     RecoveredMeta& meta) {
    RecoveryState st;
    st.market = &market;
    st.ctx = &ctx;
    st.meta = &meta;

    bool ok = db.get_state([&st](const RawRecord& rec) {
        if (!st.error) {
            apply_record(st, rec);
        }
    });
    if (!ok || st.error || !st.saw_begin || !st.saw_end) {
        LOG_ERROR(COMP) << "recovery dump failed (ok=" << ok
                        << " error=" << st.error << " begin=" << st.saw_begin
                        << " end=" << st.saw_end << ")";
        return false;
    }
    LOG_INFO(COMP) << "recovered from db: " << st.orders << " orders, "
                   << st.ticks << " tick rows, " << st.rows
                   << " book rows; session='" << meta.session << "' last_seq="
                   << meta.last_exch_seq << " epoch=" << meta.epoch;
    return true;
}

} // namespace jnx
