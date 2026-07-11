# JNX record wire specification — version 1 (FROZEN)

This document is the contract for the record format carried on the
`jnxfh` → `jnxdb` UNIX-domain stream socket, the `jnxdb` → `jnxfh`
recovery stream, and the UDP multicast feed (JNX_PLAN2.md §3). Both
codecs — `cpp/wire/record.{h,cpp}` and `jnxweb/records.py` — implement
exactly this layout and cite this file. **After Phase F3 this layout is
frozen**: any change bumps the header `version` byte and updates both
codecs, their tests, and `cpp/test/vectors/records.bin` in one commit.

## Global conventions

- **All integers are unsigned big-endian**, regardless of host order.
  Encoders/decoders go through byte-wise helpers (`cpp/common/endian.h`
  in C++, `struct` with `'>'` formats in Python); never through struct
  memory casts.
- **Alpha fields** (multi-byte text: `session`, `ticker`, `group`,
  `isin`) are ASCII, left-justified, right-padded with spaces (0x20) to
  their fixed width. Decoders strip trailing spaces; an all-spaces field
  decodes to the empty string.
- **Char fields** (single-byte tags: `trigger`, `trading_state`,
  `short_sell_restriction`, `last_system_event`, `delta_op`,
  `delta_side`, `delta_order_type`, `side`, `order_type`) are one raw
  ASCII byte. The byte 0x00 means "not applicable / no value". Note
  `'?'` (0x3F) is a *value* (the "unknown yet" states of JNX_PLAN2.md
  §2 T2), not a fill byte.
- **Prices** are the ITCH convention: u32, 1 implied decimal (tenths of
  yen); `0x7FFFFFFF` = NO_PRICE sentinel (valid in `reference_price`
  only). Encoded as plain u32.
- **Zero fill**: every byte of every record is defined. Unused level
  slots, unused delta fields, and the header `reserved` field are
  zero-filled by the encoder no matter what the in-memory struct holds.
  No uninitialized memory ever reaches the wire.
- Offsets below are decimal, from the start of the record (header
  included). `size` in bytes.

## Header — 8 bytes, identical for every kind

| offset | field    | size | type  | notes                                              |
|-------:|----------|-----:|-------|----------------------------------------------------|
| 0      | magic    | 2    | u16   | 0x4A58 (ASCII "JX")                                |
| 2      | version  | 1    | u8    | 1. Decoders reject any other value.                |
| 3      | kind     | 1    | char  | one of `U O K B E G H R` (tables below)            |
| 4      | body_len | 2    | u16   | body bytes after the header; MUST equal the fixed size for `kind` (decoders reject otherwise) |
| 6      | reserved | 2    | u16   | always 0 on encode; ignored on decode              |

Total record size = 8 + body_len. Every kind has a fixed body size:

| kind | name       | body_len | total | direction / purpose                                   |
|------|------------|---------:|------:|--------------------------------------------------------|
| `U`  | UPDATE     | 425      | 433   | FH→DB, FH→mcast, DB→FH (recovery); full ticker state + order delta |
| `O`  | ORDER      | 26       | 34    | FH→DB (sync dump), DB→FH (recovery); one live order row |
| `K`  | TICK       | 12       | 20    | FH→DB, DB→FH; one tick-table row                        |
| `B`  | SYNC_BEGIN | 0        | 8     | both directions; opens a dump                           |
| `E`  | SYNC_END   | 26       | 34    | both directions; closes a dump, carries meta            |
| `G`  | GET_STATE  | 0        | 8     | FH→DB; request full recovery dump                       |
| `H`  | HELLO      | 16       | 24    | both directions on connect (see HELLO notes)            |
| `R`  | RESET      | 0        | 8     | FH→DB; wipe all tables                                  |

## `U` UPDATE — body 425 bytes, **total wire size 433 bytes (FROZEN)**

One UPDATE per exchange message: the full merged row for the affected
(ticker, group) plus the order-level delta. Offsets include the header.

### Envelope section (offsets 8–58)

| offset | field    | size | type      | notes                                                   |
|-------:|----------|-----:|-----------|----------------------------------------------------------|
| 8      | epoch    | 8    | u64       | FH incarnation counter                                    |
| 16     | pub_seq  | 8    | u64       | contiguous per-epoch publication counter                  |
| 24     | session  | 10   | alpha     | exchange (SoupBinTCP) session id                          |
| 34     | exch_seq | 8    | u64       | exchange sequence of the triggering message               |
| 42     | exch_ns  | 8    | u64       | exchange timestamp: T-seconds × 1e9 + message ns          |
| 50     | trigger  | 1    | char      | ITCH type that caused this UPDATE, or `'#'` for sync-dump rows |
| 51     | ticker   | 4    | alpha     | SICC orderbook id (a string, NOT a number)                |
| 55     | group    | 4    | alpha     | `DAY `/`NGHT`/`DAYX`/`DAYU` (stored stripped: `"DAY"` etc.) |

### Static section (T1; offsets 59–88)

| offset | field          | size | type  | notes                                             |
|-------:|----------------|-----:|-------|----------------------------------------------------|
| 59     | isin           | 12   | alpha | from `R`; empty for auto-created books             |
| 71     | round_lot      | 4    | u32   | from `R`; 0 if no directory seen                   |
| 75     | tick_table_id  | 4    | u32   | from `R`; 0 if no directory seen                   |
| 79     | price_decimals | 1    | u8    | from `R` (always 1 in practice); 0 if no directory |
| 80     | upper_limit    | 4    | u32   | price; 0 if no directory                            |
| 84     | lower_limit    | 4    | u32   | price; 0 if no directory                            |
| 88     | flags          | 1    | u8    | bit0 = directory_seen, bit1 = order_collision_seen, bits 2–7 reserved (0) |

Flag bit numbering (spec decision, plan was loose): bit0 is the least
significant bit (`flags & 0x01` = directory_seen; `flags & 0x02` =
order_collision_seen). A book auto-created by a mid-session join has
bit0 = 0 and a zeroed static section (empty isin, all numeric statics 0).

### State section (T2; offsets 89–95)

| offset | field                  | size | type | notes                                        |
|-------:|------------------------|-----:|------|----------------------------------------------|
| 89     | trading_state          | 1    | char | `T` trading / `V` suspended / `?` unknown yet |
| 90     | short_sell_restriction | 1    | char | `0` none / `1` in effect / `?` unknown yet    |
| 91     | reference_price        | 4    | u32  | price; may be NO_PRICE (0x7FFFFFFF); 0 = never seen |
| 95     | last_system_event      | 1    | char | latest `S.event` for this group (O/S/Q/M/E/C); 0x00 = none |

T2's `last_exch_seq` / `last_update_ns` are NOT separate body fields:
the DB takes them from this record's `exch_seq` / `exch_ns` (spec
decision — the UPDATE is by definition the last message that touched
the ticker).

### Book section (T4; offsets 96–361)

| offset | field            | size | type | notes                                                  |
|-------:|------------------|-----:|------|---------------------------------------------------------|
| 96     | level_count_bid  | 1    | u8   | number of valid bid levels, 0–10                        |
| 97     | level_count_ask  | 1    | u8   | number of valid ask levels, 0–10                        |
| 98     | bids[10]         | 120  | —    | 10 slots × 12 bytes each (layout below), best (highest) price first |
| 218    | asks[10]         | 120  | —    | 10 slots × 12 bytes each, best (lowest) price first     |
| 338    | total_bid_qty    | 8    | u64  | across ALL levels of the whole book, not just top 10    |
| 346    | total_ask_qty    | 8    | u64  |                                                          |
| 354    | total_bid_orders | 4    | u32  | across ALL levels                                       |
| 358    | total_ask_orders | 4    | u32  |                                                          |

Each 12-byte level slot:

| rel. offset | field       | size | type | notes            |
|------------:|-------------|-----:|------|-------------------|
| +0          | price       | 4    | u32  | price (1 implied decimal) |
| +4          | qty         | 4    | u32  | aggregated shares at this level |
| +8          | order_count | 4    | u32  | live orders at this level |

Slots at index >= the side's level_count are zero-filled (all 12 bytes
0x00) — the encoder enforces this regardless of struct contents.

### Trade summary section (T5, no tape; offsets 362–405)

| offset | field             | size | type | notes                                        |
|-------:|-------------------|-----:|------|-----------------------------------------------|
| 362    | last_price        | 4    | u32  | passive-order price of the last `E`; 0 = no trades |
| 366    | last_qty          | 4    | u32  |                                               |
| 370    | last_match_number | 8    | u64  |                                               |
| 378    | last_trade_ns     | 8    | u64  | exchange timestamp of the last trade          |
| 386    | cum_qty           | 8    | u64  | day cumulative traded qty                     |
| 394    | cum_turnover      | 8    | u64  | Σ price×qty (tenth-yen × shares); VWAP = cum_turnover/cum_qty (÷10 → yen) |
| 402    | trade_count       | 4    | u32  |                                               |

### Delta section (order-level mutation for T3; offsets 406–432)

| offset | field                  | size | type | notes                                            |
|-------:|------------------------|-----:|------|---------------------------------------------------|
| 406    | delta_op               | 1    | char | `A` insert / `E` execute / `D` delete / `U` replace / `#` none (sync rows and non-order triggers) |
| 407    | delta_order_number     | 8    | u64  | the affected (for `U`: the NEW) order number       |
| 415    | delta_orig_order_number| 8    | u64  | `U` only: the replaced order number; else 0        |
| 416    | delta_side             | 1    | char | `B`/`S`; 0x00 when op = `#`                        |
| 417    | delta_price            | 4    | u32  | order price after the op; 0 when op = `#` or `D`   |
| 421    | delta_qty              | 4    | u32  | remaining qty after the op (0 for `D` and a filled `E`) |
| 425    | delta_order_type       | 1    | char | `Q` = DLP, 0x20 (space) = plain order, 0x00 when op = `#` |

Unused-field fill (spec decision): when `delta_op` is `'#'` every other
delta field is zero (numerics 0, chars 0x00). For op `D`, `delta_price`
and `delta_qty` are 0 and `delta_side`/`delta_order_type` reflect the
deleted order. DB semantics per op: `A` insert row; `E` set
qty_remaining = delta_qty, delete row when 0; `D` delete row; `U`
delete `delta_orig_order_number`, insert `delta_order_number`; `#` no
T3 mutation. Multicast clients ignore this section entirely.

## `O` ORDER — body 26 bytes, total 34

One live order row (T3), used in sync dumps and recovery.

| offset | field         | size | type  | notes                          |
|-------:|---------------|-----:|-------|---------------------------------|
| 8      | order_number  | 8    | u64   | key                             |
| 16     | ticker        | 4    | alpha | SICC code                       |
| 20     | group         | 4    | alpha |                                 |
| 24     | side          | 1    | char  | `B`/`S`                         |
| 25     | price         | 4    | u32   |                                 |
| 29     | qty_remaining | 4    | u32   |                                 |
| 33     | order_type    | 1    | char  | `Q` = DLP, 0x20 (space) = plain |

## `K` TICK — body 12 bytes, total 20

One tick-table row (from ITCH `L`).

| offset | field       | size | type | notes                          |
|-------:|-------------|-----:|------|---------------------------------|
| 8      | table_id    | 4    | u32  | tick table id                   |
| 12     | price_start | 4    | u32  | price (1 implied decimal): row applies from this price up |
| 16     | tick_size   | 4    | u32  |                                 |

## `B` SYNC_BEGIN — body 0 bytes, total 8

Opens a dump (FH→DB bootstrap sync, or DB→FH recovery). A SYNC_BEGIN
without a matching SYNC_END before disconnect means the dump is
partial and must be discarded (F6 rule).

## `E` SYNC_END — body 26 bytes, total 34

Closes a dump and carries the meta the receiver adopts atomically.

| offset | field         | size | type  | notes                         |
|-------:|---------------|-----:|-------|--------------------------------|
| 8      | session       | 10   | alpha | exchange session id            |
| 18     | last_exch_seq | 8    | u64   | last applied exchange sequence |
| 26     | epoch         | 8    | u64   | FH epoch that produced the dump |

## `G` GET_STATE — body 0 bytes, total 8

FH→DB: request a full recovery dump (DB answers SYNC_BEGIN … SYNC_END).

## `H` HELLO — body 16 bytes, total 24

Sent by the FH on connect; the DB replies with its own HELLO. One
layout serves both directions (spec decision — the plan gave the FH
epoch-only, but a symmetric layout is simpler and the FH's
last_exch_seq is useful diagnostics):

| offset | field         | size | type | notes                                          |
|-------:|---------------|-----:|------|-------------------------------------------------|
| 8      | epoch         | 8    | u64  | sender's epoch; 0 = fresh (FH) / empty (DB)     |
| 16     | last_exch_seq | 8    | u64  | sender's last applied exchange seq; 0 = none    |

## `R` RESET — body 0 bytes, total 8

FH→DB: wipe all tables (precedes a bootstrap sync dump).

## Stream framing

On the UDS stream, records are simply concatenated — the fixed header
plus its `body_len` delimit each record; there is no extra length
prefix. A receiver reads the 8-byte header, validates
magic/version/kind/body_len, then reads `body_len` more bytes. A
header that fails validation means the stream is corrupt and
unrecoverable by design: close the connection (do not attempt resync).
On multicast, exactly one record (always an UPDATE) per datagram.
