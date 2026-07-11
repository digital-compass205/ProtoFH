// book_dump — replay a .itch file through the C++ Market and write the
// canonical state dump. MUST stay byte-identical to
// tools/proto_state_dump.py (the format contract is documented there —
// keep the two in sync).
//
// Usage: book_dump <file.itch> <outdir>
#include <sys/stat.h>

#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "itch/itch.h"
#include "market/market.h"

namespace {

// None-style optional int: -1 renders as empty.
std::string opt64(int64_t v) {
    if (v < 0) {
        return std::string();
    }
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(v));
    return std::string(buf);
}

std::string u64s(uint64_t v) {
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%llu", static_cast<unsigned long long>(v));
    return std::string(buf);
}

std::string group_of(const jnx::Market& market, const std::string& oid) {
    std::map<std::string, jnx::Instrument>::const_iterator it =
        market.refdata.instruments().find(oid);
    if (it == market.refdata.instruments().end()) {
        return std::string();
    }
    return it->second.group;
}

} // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::fprintf(stderr, "usage: book_dump <file.itch> <outdir>\n");
        return 2;
    }
    std::FILE* in = std::fopen(argv[1], "rb");
    if (in == NULL) {
        std::fprintf(stderr, "book_dump: cannot open %s\n", argv[1]);
        return 2;
    }

    jnx::Market market;
    // orderbook_id -> match number of its last execution (tracked from
    // apply results, exactly like the Python dumper does).
    std::map<std::string, uint64_t> last_match;

    unsigned char lenbuf[2];
    unsigned char msgbuf[65536];
    for (;;) {
        size_t got = std::fread(lenbuf, 1, 2, in);
        if (got == 0) {
            break;
        }
        if (got != 2) {
            std::fprintf(stderr, "book_dump: truncated length prefix\n");
            std::fclose(in);
            return 1;
        }
        size_t mlen = (static_cast<size_t>(lenbuf[0]) << 8) |
                      static_cast<size_t>(lenbuf[1]);
        if (mlen == 0 || std::fread(msgbuf, 1, mlen, in) != mlen) {
            std::fprintf(stderr, "book_dump: truncated message body\n");
            std::fclose(in);
            return 1;
        }
        jnx::ItchMsg msg;
        const char* err = NULL;
        if (!jnx::decode(msgbuf, mlen, msg, &err)) {
            std::fprintf(stderr, "book_dump: decode error: %s\n",
                         err ? err : "?");
            std::fclose(in);
            return 1;
        }
        jnx::ApplyResult res = market.apply(msg);
        if (res.has_trade) {
            last_match[res.trade.orderbook_id] = res.trade.match_number;
        }
    }
    std::fclose(in);

    std::string outdir(argv[2]);
    ::mkdir(outdir.c_str(), 0777); // best-effort, single level like makedirs

    // --- refdata.csv ------------------------------------------------------
    {
        std::string path = outdir + "/refdata.csv";
        std::FILE* f = std::fopen(path.c_str(), "w");
        if (f == NULL) {
            std::fprintf(stderr, "book_dump: cannot write %s\n", path.c_str());
            return 1;
        }
        std::fputs(
            "orderbook_id,isin,group,round_lot,tick_table_id,price_decimals,"
            "upper_limit,lower_limit,trading_state,short_sell_state,"
            "reference_price,directory_missing\n",
            f);
        const std::map<std::string, jnx::Instrument>& insts =
            market.refdata.instruments();
        for (std::map<std::string, jnx::Instrument>::const_iterator it =
                 insts.begin();
             it != insts.end(); ++it) {
            const jnx::Instrument& i = it->second;
            std::fprintf(f, "%s,%s,%s,%s,%s,%s,%s,%s,%c,%c,%s,%d\n",
                         i.orderbook_id.c_str(), i.isin.c_str(),
                         i.group.c_str(), opt64(i.round_lot).c_str(),
                         opt64(i.tick_table_id).c_str(),
                         opt64(i.price_decimals).c_str(),
                         opt64(i.upper_limit).c_str(),
                         opt64(i.lower_limit).c_str(), i.trading_state,
                         i.short_sell_state,
                         opt64(i.reference_price).c_str(),
                         i.directory_missing ? 1 : 0);
        }
        std::fclose(f);
    }

    // Per-(book, side, price) live order counts and per-(book, side)
    // totals, derived from the order store (matches the Python dumper).
    typedef std::pair<std::string, char> SideKey;
    std::map<std::pair<SideKey, uint32_t>, uint64_t> level_orders;
    std::map<SideKey, uint64_t> side_orders;
    {
        const std::unordered_map<uint64_t, jnx::Order>& orders =
            market.books.orders();
        for (std::unordered_map<uint64_t, jnx::Order>::const_iterator it =
                 orders.begin();
             it != orders.end(); ++it) {
            const jnx::Order& o = it->second;
            SideKey sk(o.orderbook_id, o.side);
            ++level_orders[std::make_pair(sk, o.price)];
            ++side_orders[sk];
        }
    }

    // --- books.csv --------------------------------------------------------
    {
        std::string path = outdir + "/books.csv";
        std::FILE* f = std::fopen(path.c_str(), "w");
        if (f == NULL) return 1;
        std::fputs("orderbook_id,group,kind,side,price,qty,order_count\n", f);
        const std::map<std::string, jnx::Book>& books = market.books.books();
        for (std::map<std::string, jnx::Book>::const_iterator bit =
                 books.begin();
             bit != books.end(); ++bit) {
            const std::string& oid = bit->first;
            const jnx::Book& book = bit->second;
            std::string grp = group_of(market, oid);
            // Bid levels best-first (descending price).
            const jnx::SideLevels::LevelMap& bids = book.bids().ascending();
            for (jnx::SideLevels::LevelMap::const_reverse_iterator it =
                     bids.rbegin();
                 it != bids.rend(); ++it) {
                uint64_t oc = level_orders[std::make_pair(SideKey(oid, 'B'),
                                                          it->first)];
                std::fprintf(f, "%s,%s,level,B,%u,%s,%s\n", oid.c_str(),
                             grp.c_str(), it->first, u64s(it->second).c_str(),
                             u64s(oc).c_str());
            }
            // Ask levels best-first (ascending price).
            const jnx::SideLevels::LevelMap& asks = book.asks().ascending();
            for (jnx::SideLevels::LevelMap::const_iterator it = asks.begin();
                 it != asks.end(); ++it) {
                uint64_t oc = level_orders[std::make_pair(SideKey(oid, 'S'),
                                                          it->first)];
                std::fprintf(f, "%s,%s,level,S,%u,%s,%s\n", oid.c_str(),
                             grp.c_str(), it->first, u64s(it->second).c_str(),
                             u64s(oc).c_str());
            }
            std::fprintf(f, "%s,%s,total,B,,%s,%s\n", oid.c_str(), grp.c_str(),
                         u64s(book.bids().total_qty()).c_str(),
                         u64s(side_orders[SideKey(oid, 'B')]).c_str());
            std::fprintf(f, "%s,%s,total,S,,%s,%s\n", oid.c_str(), grp.c_str(),
                         u64s(book.asks().total_qty()).c_str(),
                         u64s(side_orders[SideKey(oid, 'S')]).c_str());
        }
        std::fclose(f);
    }

    // --- orders.csv -------------------------------------------------------
    {
        std::string path = outdir + "/orders.csv";
        std::FILE* f = std::fopen(path.c_str(), "w");
        if (f == NULL) return 1;
        std::fputs("order_number,orderbook_id,group,side,price,"
                   "remaining_qty\n",
                   f);
        // Sort by order number explicitly (store is unordered).
        std::map<uint64_t, const jnx::Order*> sorted;
        const std::unordered_map<uint64_t, jnx::Order>& orders =
            market.books.orders();
        for (std::unordered_map<uint64_t, jnx::Order>::const_iterator it =
                 orders.begin();
             it != orders.end(); ++it) {
            sorted[it->first] = &it->second;
        }
        for (std::map<uint64_t, const jnx::Order*>::const_iterator it =
                 sorted.begin();
             it != sorted.end(); ++it) {
            const jnx::Order& o = *it->second;
            std::fprintf(f, "%s,%s,%s,%c,%u,%u\n",
                         u64s(o.order_number).c_str(), o.orderbook_id.c_str(),
                         o.group.c_str(), o.side, o.price, o.remaining_qty);
        }
        std::fclose(f);
    }

    // --- trades.csv -------------------------------------------------------
    {
        std::string path = outdir + "/trades.csv";
        std::FILE* f = std::fopen(path.c_str(), "w");
        if (f == NULL) return 1;
        std::fputs("orderbook_id,group,trade_count,cum_qty,cum_turnover,"
                   "last_price,last_qty,last_match_number\n",
                   f);
        const std::map<std::string, jnx::BookStats>& stats =
            market.tape.stats();
        for (std::map<std::string, jnx::BookStats>::const_iterator it =
                 stats.begin();
             it != stats.end(); ++it) {
            const jnx::BookStats& s = it->second;
            std::map<std::string, uint64_t>::const_iterator lm =
                last_match.find(it->first);
            std::fprintf(f, "%s,%s,%s,%s,%s,%s,%s,%s\n", it->first.c_str(),
                         group_of(market, it->first).c_str(),
                         u64s(s.trade_count).c_str(), u64s(s.volume).c_str(),
                         u64s(s.notional).c_str(), opt64(s.last_price).c_str(),
                         opt64(s.last_qty).c_str(),
                         lm == last_match.end() ? ""
                                                : u64s(lm->second).c_str());
        }
        std::fclose(f);
    }

    // --- stats.csv --------------------------------------------------------
    {
        std::string path = outdir + "/stats.csv";
        std::FILE* f = std::fopen(path.c_str(), "w");
        if (f == NULL) return 1;
        std::fputs("key,value\n", f);
        uint64_t applied = 0;
        for (std::map<char, uint64_t>::const_iterator it =
                 market.message_counts.begin();
             it != market.message_counts.end(); ++it) {
            applied += it->second;
        }
        uint64_t auto_created = 0;
        const std::map<std::string, jnx::Instrument>& insts =
            market.refdata.instruments();
        for (std::map<std::string, jnx::Instrument>::const_iterator it =
                 insts.begin();
             it != insts.end(); ++it) {
            if (it->second.directory_missing) {
                ++auto_created;
            }
        }
        std::fprintf(f, "messages_applied,%s\n", u64s(applied).c_str());
        std::fprintf(f, "unknown,%s\n", u64s(market.unknown_count).c_str());
        for (std::map<char, uint64_t>::const_iterator it =
                 market.message_counts.begin();
             it != market.message_counts.end(); ++it) {
            std::fprintf(f, "msg_%c,%s\n", it->first,
                         u64s(it->second).c_str());
        }
        std::fprintf(f, "instruments,%zu\n", insts.size());
        std::fprintf(f, "auto_created_books,%s\n", u64s(auto_created).c_str());
        std::fprintf(f, "books,%zu\n", market.books.books().size());
        std::fprintf(f, "live_orders,%zu\n", market.books.orders().size());
        std::fprintf(f, "collisions,%s\n",
                     u64s(market.books.collisions).c_str());
        std::fprintf(f, "orphan_executes,%s\n",
                     u64s(market.books.orphan_executes).c_str());
        std::fprintf(f, "orphan_deletes,%s\n",
                     u64s(market.books.orphan_deletes).c_str());
        std::fprintf(f, "orphan_replaces,%s\n",
                     u64s(market.books.orphan_replaces).c_str());
        std::fprintf(f, "ref_price_ignored,%s\n",
                     u64s(market.books.ref_price_ignored).c_str());
        std::fprintf(f, "execution_count,%s\n",
                     u64s(market.books.execution_count).c_str());
        std::fprintf(f, "executed_volume,%s\n",
                     u64s(market.books.executed_volume).c_str());
        std::fprintf(f, "trade_count,%s\n",
                     u64s(market.tape.trade_count).c_str());
        std::fprintf(f, "total_volume,%s\n",
                     u64s(market.tape.total_volume).c_str());
        std::fclose(f);
    }

    return 0;
}
