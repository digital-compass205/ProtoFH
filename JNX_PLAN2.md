# Japannext ITCH Feed Handler — Phase 2 Plan (C++ FH + in-memory DB + web client)

**Status of Phase 1:** the Python prototype (`jnxfeed/`, plan in `JNX_PLAN.md`) is complete and validated: SoupBinTCP connectivity, full ITCH v2.02 decode, GLIMPSE snapshot sync, order books, trade tape, exchange simulator, golden fixtures from the official pcaps (~379k msgs/s replay). **Its data-handling logic is the reference implementation** — Phase 2 ports it to C++ and must match it bit-for-bit on the golden sample.

**Phase 2 goal:** a production-shaped, three-process system:

1. **`jnxdb`** (C++11) — in-memory database process holding current market state in 5 tables keyed by ticker. No disk persistence; restart = full reset.
2. **`jnxfh`** (C++11) — feed handler: connects to the exchange (SoupBinTCP), decodes ITCH, applies each message as **one atomic update** to `jnxdb`, and **multicasts** a full-state record per update. On restart it resumes from the last message it saw (not start of day).
3. **`jnxweb`** (Python 3.6, stdlib only) — subscribes to the multicast, exposes a web GUI (auto-updating via WebSocket) to inspect any ticker.

Everything is testable offline against the Phase-1 Python simulator and golden fixtures — no test line needed.

---

## 0. Environment ground rules (EMBED IN EVERY TASK PROMPT)

### 0.1 Target vs. development environment — verified facts

| | Target (production) | Dev container (here) |
|---|---|---|
| OS | RHEL 8.10, x86_64 | Ubuntu 26.04, x86_64 |
| C++ | gcc **8.5** (RHEL 8 default), glibc 2.28 | gcc **15.2** |
| Python | **3.6** (RHEL 8 platform python) | **3.14.4** (`python3.6` NOT available) |
| Build tools | make; **no cmake assumed** | GNU make 4.4.1; no cmake |
| Containers | n/a | **no docker, no podman** |
| 3rd-party libs | **FORBIDDEN** (locked env, no pip, no yum extras) | don't use any — parity with target |

Consequences — these are hard rules:

- **C++: `-std=c++11` strictly.** gcc 15 accepts far more than gcc 8.5; anything from C++14/17/20 may compile here and fail on target. Forbidden: `std::string_view`, `std::optional`, `std::variant`, `<filesystem>`, `<charconv>`, structured bindings, `auto` return-type deduction without trailing type, generic lambdas' C++14 extensions, `std::make_unique` (C++14 — write the 3-line helper or use `unique_ptr(new T(...))`), init-statements in `if`, digit separators are fine (C++14? no — forbidden), inline variables. Allowed: C++11 only — `auto`, range-for, lambdas, `std::thread`/`mutex` (link `-pthread`), `std::unordered_map`, `std::chrono`, `std::atomic`, `constexpr` (C++11 rules: single return).
- **Compile flags (all C++ tasks):** `g++ -std=c++11 -Wall -Wextra -Werror -pedantic -O2 -pthread`. `-pedantic -std=c++11` is the guard against accidental newer-standard usage on gcc 15.
- **No external C++ dependencies at all** — not even header-only, not even for tests. Tests use the tiny assert harness in `cpp/test/minitest.h` (created in F0).
- **Linux-only APIs are fine** (both OSes are Linux): `epoll`/`poll`, `socket`, UNIX domain sockets, `eventfd`, `clock_gettime`. Do NOT use anything from kernels newer than RHEL 8 (no `io_uring`).
- **Python: code must run on BOTH 3.6.x and 3.14.x** (dev runs it on 3.14; production runs 3.6). Follow `JNX_PLAN.md` §0 verbatim (no dataclasses, no asyncio, no walrus, no f-string `=`, `subprocess` with `universal_newlines=`, etc.). Additionally, since we cannot run 3.6 here: every Python task must pass `python3 tools/py36check.py <files>` (AST lint built in F0) — treat its failures as build failures.
- **Byte order:** both machines are x86-64 little-endian; all wire formats (ITCH and ours) are **big-endian**. Never `memcpy` a struct to the wire; always go through the `be_*` helpers in `cpp/common/endian.h`. Never rely on C struct layout for wire formats.
- **No cmake:** plain GNU make, one top-level `cpp/Makefile`.
- **Final gate:** everything must compile and pass unit tests on a real RHEL 8.10 box before production (checklist in §8) — but all development/verification here is on the dev container.

### 0.2 Protocol reference

All exchange-protocol knowledge (ITCH message tables, SoupBinTCP framing, GLIMPSE, semantics gotchas) is in **`JNX_PLAN.md` §3** — embed that section in every task that touches exchange bytes. Do not re-read the PDFs. Key traps repeated because worker models WILL get them wrong otherwise:

- Price = unsigned BE u32, **1 implied decimal** (tenths of yen); `0x7FFFFFFF` = NO_PRICE sentinel.
- Orderbook Id (ticker) = **4-byte Alpha** (SICC code, a string like `"8306"`), NOT an integer.
- An `A` message with `order_number == 0` is a **reference-price carrier**, not an order. Never put it in the book.
- `E`/`D`/`U` carry **no ticker/side/price** — resolve via the order store keyed by order number. Trade price on `E` = the stored passive order's price. `U` = delete original + insert new order inheriting ticker/group/side.
- SoupBinTCP: client counts sequence numbers itself starting from the Login-Accepted value; one ITCH message per `S` packet; heartbeat each side >1 s idle; declare dead after 15 s silence; reconnect + re-login with same session and next expected seq = lossless resume.
- Books referenced before their `R` directory message must auto-create (`directory_missing` flag) — the golden UDP sample starts mid-session at seq 12562 with no `R`/`L` at all.

### 0.3 Working rules

- One git commit per completed task, subject starting with its task number (e.g. `F2.1: ...`), in this repo (`/workspace/jnx-feed`).
- A task is DONE only when its Verification Criteria commands pass, run from the repo root, and nothing previously green broke (`make -C cpp test` + `python3 -m pytest -q` stay green).
- Never modify Phase-1 Python code under `jnxfeed/` except where a task explicitly says so (new tools/scripts go in `tools/` or `jnxweb/`).
- Log format everywhere (C++ and Python): `YYYY-MM-DDTHH:MM:SS.mmm LEVEL component: message` to stderr. Levels: DEBUG/INFO/WARN/ERROR.

---

## 1. Architecture

```
                    Exchange (or Phase-1 Python simulator: sim/exchange.py)
                        │ SoupBinTCP :15001 (ITCH)   :15002 (GLIMPSE)
                        ▼
        ┌──────────────────────────────────────┐
        │  jnxfh (C++)                         │
        │  soup client ─ itch decode ─ market  │
        │  core (order store, books, tape,     │
        │  refdata) ─ record builder           │
        └───────┬──────────────────────┬───────┘
                │ UDS stream           │ UDP multicast 239.192.1.1:26400
                │ (framed records,     │ (UPDATE records, full state,
                │  UPDATE/ORDER/SYNC)  │  1 datagram per exchange msg)
                ▼                      ▼
        ┌───────────────────┐   ┌───────────────────────────────┐
        │  jnxdb (C++)      │   │  jnxweb (Python 3.6)          │
        │  5 tables         │   │  mcast rx ─ state cache ─     │
        │  UDS ingest       │   │  http.server + WebSocket ─    │
        │  TCP query :26401 │   │  browser GUI  :8080           │
        └───────────────────┘   └───────────────────────────────┘
```

**Responsibilities and state ownership**

- `jnxfh` is the *engine*: it owns the live market state (a C++ port of the prototype's `Market`: refdata + order store + price levels + tape). For every exchange message it (1) applies it to its own state, (2) builds ONE `UPDATE` record containing the *full merged row* for the affected ticker (static + state + top-10 aggregated book + trade summary + the order-level delta), (3) writes that record to the `jnxdb` UDS socket (one `write()`, atomic by framing), (4) sends the same bytes as one multicast datagram. One exchange message → exactly one record → one atomic DB mutation → one publication.
- `jnxdb` is the *queryable mirror and the FH's recovery source*: single-threaded ingest applies records to the 5 tables; a query listener serves operators and the FH recovery protocol. It keeps the order-level table precisely so a restarted `jnxfh` can re-download its order store and resume from the last applied exchange sequence — no start-of-day replay.
- `jnxweb` is loss-tolerant by design: because every multicast record carries full ticker state, a missed datagram is healed by the next update for that ticker. No recovery protocol, no DB access.

**Restart matrix (the design's core invariants)**

| Event | Behavior |
|---|---|
| `jnxfh` restarts, `jnxdb` alive | FH connects to DB, `GET_STATE` → receives meta (session, last exch seq, epoch), tick tables, all static/state/trade rows, all live orders → rebuilds its market core → logs in to exchange with same session, requested seq = last+1 → continues. **Zero replay, nothing lost** (SoupBinTCP resume). |
| `jnxdb` restarts (or is started after FH) | DB comes up empty. FH's UDS write fails → FH enters DB-reconnect loop (1 s backoff) while STAYING live on the exchange and still multicasting. On reconnect, HELLO exchange shows DB is empty/epoch-mismatched → FH pushes a full **SYNC dump** from its own memory (orders + rows + tick tables + meta), then resumes streaming updates. DB restart = full reset, as specified. |
| Both restart / cold start | DB empty, FH has nothing → bootstrap from exchange: GLIMPSE snapshot then live from seq N (or full replay from seq 1, configurable — the simulator supports both), `RESET` + SYNC dump to DB, then stream. |
| Exchange TCP drops | Soup reconnect + re-login same session at next expected seq (Phase-1 logic, §3.4/§3.6 of JNX_PLAN.md). No data loss, no duplicate records emitted. |

**IPC choice (simplicity first):** a single **UNIX domain stream socket** (`/run/jnx/db.sock`, path configurable) carrying length-framed binary records — same big-endian fixed-width style as ITCH itself, one codec (`F3`) shared by every component including the Python client. Rejected alternatives: shared memory (fastest but complex lifecycle/synchronization for worker models to get right), pipes/FIFOs (no bidirectional recovery), TCP localhost (works, but UDS is simpler to secure and can't collide with real ports). Throughput is a non-issue: the whole golden day is 222k messages ≈ 95 MB of records; UDS does GB/s.

**Multicast:** UDP, one datagram per UPDATE record (~430 bytes, far under MTU — never fragment). Default group `239.192.1.1:26400`, TTL 1, `IP_MULTICAST_LOOP=1` (required for same-host testing). Records carry `epoch` + `pub_seq` so clients can *count* losses (stats only — no recovery needed by design).

---

## 2. Database schema (the 5 tables)

Primary key everywhere: **`ticker`** = 4-char SICC orderbook id (e.g. `8306`). Japannext runs one book per (ticker, group) where group ∈ {`DAY `, `NGHT`, `DAYX`, `DAYU`}; a single feed/session carries one group in practice, but the schema keys on **(ticker, group)** with ticker as the primary lookup — a plain ticker query returns all groups (usually one).

**F2 outcome notes (prototype semantics won, schema reads accordingly):** the C++ market core (like the prototype) keys books/tape by **ticker alone** — the `group` column is sourced from refdata and may be blank for auto-created books; per-level `order_count` is **derived from the live-order store**, not tracked in levels; an order-number collision **replaces** the stale order (and increments the collision counter). The DB tables below simply mirror what UPDATE records carry — these notes matter to anyone reasoning about semantics, not to the DB implementation.

### T1 `static` — reference data (from `R` directory + Stock Master enrichment later)
| field | type | source |
|---|---|---|
| ticker | char[4] | `R.orderbook_id` |
| group | char[4] | `R.group` |
| isin | char[12] | `R.isin` |
| round_lot | u32 | `R.round_lot` |
| tick_table_id | u32 | `R.tick_table_id` (tick-size rows themselves live in DB-internal `tick_tables` from `L`, exposed via query) |
| price_decimals | u8 | `R.price_decimals` (always 1) |
| upper_limit / lower_limit | price u32 | `R` |
| directory_seen | bool | false for auto-created books (mid-session join) |

### T2 `state` — current per-ticker dynamic state
| field | type | source |
|---|---|---|
| ticker, group | keys | |
| trading_state | char | `H`: `T` trading, `V` suspended, `?` unknown-yet; absence semantics per JNX_PLAN.md §3.3(4) |
| short_sell_restriction | char | `Y`: `0` none, `1` in effect, `?` unknown |
| reference_price | price u32 | ref-price `A` (order_number 0); may be NO_PRICE |
| last_system_event | char | latest `S.event` for this group (O/S/Q/M/E/C) |
| last_exch_seq | u64 | seq of last message that touched this ticker |
| last_update_ns | u64 | exchange timestamp (T-seconds×1e9 + ns) of that message |

### T3 `orders` — live order-level book (keyed by order_number, indexed by ticker)
| field | type | notes |
|---|---|---|
| order_number | u64 | key; collisions across groups counted + WARNed (JNX_PLAN.md §3.3(3)) |
| ticker, group | char[4] | inherited on `U` |
| side | char | `B`/`S` |
| price | price u32 | |
| qty_remaining | u32 | `E` decrements; row deleted at 0 or on `D`/`U` |
| order_type | char | `Q` = DLP (`F` messages), else space |

### T4 `book_agg` — aggregated price levels per (ticker, group)
| field | type | notes |
|---|---|---|
| ticker, group | keys | |
| bids / asks | array[10] of (price u32, qty u32, order_count u32) | best-first (bids desc, asks asc); depth 10 = published depth; DB stores what the record carries |
| total_bid_qty / total_ask_qty | u64 | across ALL levels, not just top 10 |
| total_bid_orders / total_ask_orders | u32 | |

### T5 `trades` — per-ticker trade summary + recent tape
| field | type | notes |
|---|---|---|
| ticker, group | keys | |
| last_price, last_qty | u32 | from `E` (passive-price rule) |
| last_match_number | u64 | |
| last_trade_ns | u64 | |
| cum_qty | u64 | day cumulative |
| cum_turnover | u64 | Σ price×qty in tenth-yen×shares units → VWAP = cum_turnover / cum_qty (÷10 for yen) |
| trade_count | u32 | |
| tape | ring of last 50 (ns, price, qty, match_number) | DB-side only, built from UPDATE records with trigger `E` |

Plus internal (not a table): `meta` — exchange session id (char[10]), last applied exch_seq (u64), FH epoch (u64), record/drop counters; `tick_tables` — map tick_table_id → sorted rows (price_start, tick_size) from `L`.

---

## 3. Wire format (one codec for UDS **and** multicast — task F3 owns this)

All integers big-endian, all alpha fields space-padded ASCII. Every record:

```
header: magic u16 = 0x4A58 ("JX") | version u8 = 1 | kind u8 | body_len u16
```

Kinds:

| kind | dir | purpose |
|---|---|---|
| `U` UPDATE | FH→DB, FH→mcast, DB→FH (recovery) | full ticker state + order delta; THE record |
| `O` ORDER | FH→DB (sync dump), DB→FH (recovery) | one live order row (T3 fields) |
| `K` TICK | FH→DB, DB→FH | one tick-table row (table_id, price_start, tick_size) |
| `B` SYNC_BEGIN / `E` SYNC_END | both dirs | bracket a dump; SYNC_END carries meta: session char[10], last_exch_seq u64, epoch u64 |
| `G` GET_STATE | FH→DB | request full recovery dump |
| `H` HELLO | FH→DB on connect | epoch u64 (0 = fresh FH); DB replies HELLO with its epoch + last_exch_seq (0/0 = empty) |
| `R` RESET | FH→DB | wipe all tables (precedes a bootstrap sync) |

**UPDATE body** (fixed size; exact offsets to be tabulated in `docs/wire_spec.md` by F3 and frozen with a `static_assert` + golden test):
`epoch u64, pub_seq u64, session char[10], exch_seq u64, exch_ns u64, trigger char (ITCH type, or '#' for sync-dump rows), ticker char[4], group char[4]` — then the T1 static fields, T2 state fields (+ `flags u8`: bit0 directory_seen, bit1 order-collision-seen), T4 aggregate section (level_count u8 ×2, 10×(price,qty,count)×2 sides, totals), T5 summary fields (no tape), and the **delta section**: `op char (A/E/D/U/'#'), order_number u64, orig_order_number u64 (U only), side char, price u32, qty u32, order_type char`. DB uses the delta to maintain T3 and the rest to upsert T1/T2/T4/T5; multicast clients ignore the delta. Total ≈ 430 bytes.

**Atomicity & ordering:** one UPDATE per exchange message; DB applies each record fully under a single-threaded loop before reading the next. DB **rejects** (counts + WARNs, does not apply) any UPDATE with `exch_seq ≤ meta.last_exch_seq` and same epoch — duplicates are impossible in normal operation, this is a safety net.

---

## 4. Repository layout (additions)

```
jnx-feed/
  JNX_PLAN2.md                    # this file
  docs/wire_spec.md               # frozen byte layout (F3 deliverable)
  cpp/
    Makefile                      # all: jnxfh jnxdb tools tests; test: build+run all test bins
    common/   endian.h log.h cfg.h time.h minitest.h        (F0)
    itch/     itch.h itch.cpp soup.h soup.cpp               (F1)
    market/   refdata.* orders.* levels.* tape.* market.*   (F2)
    wire/     record.h record.cpp                           (F3)
    db/       tables.* ingest.* query.* jnxdb_main.cpp      (F4)
    fh/       reactor.* soupclient.* glimpse.* publish.*
              recover.* jnxfh_main.cpp                      (F5)
    tools/    itch_replay.cpp  book_dump.cpp                (F1/F2 harnesses)
    test/     test_*.cpp  (one binary per module)
  jnxweb/                         # Python 3.6 client package        (F7)
    __init__.py __main__.py mcast.py records.py state.py
    httpd.py wsock.py static_page.py
  tools/
    py36check.py                  # AST lint: forbid >3.6 syntax     (F0)
    gen_golden_vectors.py         # prototype → JSON byte vectors    (F1)
    proto_state_dump.py           # prototype Market → canonical CSV (F2)
    dbquery.py                    # query client for jnxdb           (F4)
    mcast_spy.py                  # decode+print multicast           (F5)
    run_e2e.py                    # process orchestration for tests  (F6)
```

---

## 5. Phases

Every task prompt = **§0 of this plan + JNX_PLAN.md §3 + the task text**. Sizes: S ≤ 200 LoC, M ≤ 500, L bigger.

---

### Phase F0 — Toolchain, scaffolding, guardrails

**Objective:** a `cpp/` build that enforces C++11 strictness on gcc 15, a micro test harness, shared helpers, and the Python-3.6 lint — the rails every later task runs on.

**Implementation steps:**
1. `cpp/Makefile`: variables `CXX?=g++`, `CXXFLAGS=-std=c++11 -Wall -Wextra -Werror -pedantic -O2 -g -pthread`; targets `all`, `test` (build every `cpp/test/test_*.cpp` into `cpp/build/`, run each, fail on first non-zero), `clean`. Out-of-tree objects under `cpp/build/`.
2. `cpp/common/minitest.h`: header-only ~60-line harness: `TEST(name){...}` auto-registration, `CHECK(expr)`, `CHECK_EQ(a,b)` printing values on failure, `main()` running all, exit code = #failures.
3. `cpp/common/endian.h`: `be_u16/u32/u64` read/write from/to `unsigned char*`; `cpp/common/log.h`: the §0.3 format, level from env `JNX_LOG=INFO`; `cpp/common/time.h`: `now_ns()` via `clock_gettime(CLOCK_REALTIME)` + monotonic variant; `cpp/common/cfg.h`: tiny `key=value` file parser + `--key=value` argv override (no getopt_long dependency games — hand-roll).
4. `tools/py36check.py` (runs on 3.14, checks 3.6-compat): `ast`-walk rejecting walrus, f-string `=`, positional-only params, `match`, forbidden imports (`dataclasses`, `asyncio`, `contextvars`), calls `subprocess.run` with `capture_output`/`text` kwargs, `time.time_ns`, `breakpoint`, dict-merge `|` operators on 3.9+ (detect `BinOp` with dict displays is imperfect — reject `|` between calls to `dict`? Simpler and adequate: forbid nothing here but add pattern-list), plus a regex pass for banned tokens. Emits file:line reasons; exit 1 on any hit. Include self-test with bad-snippet fixtures.
5. Smoke test `cpp/test/test_common.cpp` covering endian round-trips, cfg parsing, minitest itself.

**Boundary conditions:** endian helpers must work at unaligned offsets (use byte ops, never pointer casts — that's UB and will also matter for records); cfg parser: missing file, blank lines, comments `#`, unknown keys warn-not-die.

**Verification criteria:**
```
make -C cpp test                     # builds clean with -Werror -pedantic, all tests pass
python3 tools/py36check.py tools/py36check.py        # self-clean
python3 tools/py36check.py tools/tests_bad_py36/*.py # exits 1, lists every planted violation
```

**Pitfalls to avoid:** gcc 15 + `-Werror` will surface warnings gcc 8.5 wouldn't — good, keep them fixed rather than silenced. Do NOT add `-march=native` (target CPU unknown). Don't use designated initializers (C++20) or `enum class` forward declarations in ways C++11 disallows. In `minitest.h`, avoid static-init-order tricks beyond a simple registry vector in a function-local static.

---

### Phase F1 — C++ ITCH + SoupBinTCP codecs, proven against Python golden vectors

**Objective:** decode/encode for all 12 ITCH types and all 10 SoupBinTCP packet types in C++, byte-identical to the Phase-1 Python codec.

**Implementation steps:**
1. `tools/gen_golden_vectors.py` (may import `jnxfeed`): for every ITCH type, emit JSON: `{type, hex_bytes, fields{...}}` — at least 3 vectors per type including edge values (NO_PRICE, order_number 0 ref-price `A`, blank group `S`, max qty), generated via the prototype **encoder** and re-verified with its decoder. Same for Soup packets (login request/accepted exact padding!). Output `cpp/test/vectors/itch.json`, `soup.json`. Commit the JSON (deterministic).
2. `cpp/itch/itch.h/.cpp`: message structs (POD, host-order fields, `char[5]`-style NUL-terminated alphas), `enum class MsgType : char`, `decode(const unsigned char*, size_t, ItchMsg&) -> bool`, `encode(const ItchMsg&, unsigned char*) -> size_t`, `expected_len(char type)` table mirroring JNX_PLAN.md §3.2. Strict: wrong length or unknown type → decode fails with reason (no exceptions across the API; return codes + `last_error`).
3. `cpp/soup/`: framing state machine `SoupFramer::feed(bytes) -> vector<Packet>` handling arbitrary fragmentation (byte-at-a-time safe), plus packet builders (login request with exact left/right padding rules, heartbeats, logout).
4. `cpp/test/test_itch.cpp`, `test_soup.cpp`: hand-rolled minimal JSON reader (only what the vector files need: flat objects, strings, ints — ~80 lines; NO external json lib) driving decode/encode round-trips against every vector.
5. `cpp/tools/itch_replay.cpp`: reads a `.itch` file (2-byte-length framing, JNX_PLAN.md §3.7), decodes every message, prints per-type counts + first error. This is the golden-sample gate.

**Boundary conditions:** truncated buffer at every possible length; alpha fields with all-spaces; price sentinel `0x7FFFFFFF` must survive round-trip untouched; Soup framer must tolerate a length prefix split across reads and packets of length 1 (bare type byte, e.g. heartbeats).

**Verification criteria:**
```
make -C cpp test
cpp/build/itch_replay tests/fixtures/sample_udp.itch
# MUST print exactly: total=222189  A=128366 E=67902 D=10772 U=9287 T=5843 Y=16 S=2 H=1  errors=0
python3 tools/gen_golden_vectors.py --check   # vectors regenerate identically (determinism)
```

**Pitfalls to avoid:** Orderbook id is ALPHA not integer — a worker model porting "id" fields will reach for u32; the vectors catch it, don't "fix" the vectors. Left-vs-right padding on Soup login fields differs per field (§3.4). Don't decode by `reinterpret_cast` of packed structs — byte-wise reads via `endian.h` only. `E` is 25 bytes and has NO price field — do not invent one.

---

### Phase F2 — C++ market core, parity-proven against the prototype

**Objective:** port `jnxfeed/book/` (refdata, order store, price levels, tape, market facade) to C++ with **identical semantics**, proven by a canonical-state-dump diff over the full golden sample.

**Implementation steps:**
1. `tools/proto_state_dump.py`: replays a `.itch` file through the *prototype* `Market` and writes a **canonical dump**: `refdata.csv`, `books.csv` (every ticker: all price levels best-first with qty+order_count, totals), `orders.csv` (every live order sorted by order number), `trades.csv` (per-ticker cum_qty, cum_turnover, trade_count, last price/qty/match), `stats.csv` (orphan counters, collision count, auto-created book count). Fixed column order, LF, no floats (prices as raw ints).
2. `cpp/market/refdata.*`: consumes `R/L/H/Y/S` + ref-price `A`; absence semantics; auto-create with `directory_seen=false`; tick tables.
3. `cpp/market/orders.*` + `levels.*`: `unordered_map<u64, Order>` store; per-(ticker,group) books as `std::map<u32, Level>` (bids: `std::greater` comparator) — correctness first, optimize only in F8 if the bench says so; `A/F` insert, `E` cumulative exec + passive-price trade emit + erase at zero, `D` erase, `U` erase+insert inheriting ticker/group/side; orphan `E/D/U` (unknown order number) counted, never fatal; collision counter.
4. `cpp/market/tape.*`: per-ticker cum qty/turnover/count + last trade; `cpp/market/market.*`: `Market::apply(const ItchMsg&) -> ApplyResult` where ApplyResult names the affected (ticker, group) + which sections changed + the order-delta — exactly what F3/F5 need to build an UPDATE.
5. `cpp/tools/book_dump.cpp`: replay `.itch` through C++ Market, write the same canonical dump files.
6. Unit tests for every §3.3 gotcha, plus a randomized self-check (invariant: per-book level totals == sum of live orders in store).

**Boundary conditions:** `U` to an unknown original order (orphan) — count, ignore, do NOT insert the new order; `E` with executed_qty > remaining (clamp to zero + WARN); ref-price `A` with NO_PRICE; messages for auto-created books; empty book dump (all levels consumed); multiple `E` on one order (cumulative, one trade each).

**Verification criteria:**
```
make -C cpp test
python3 tools/proto_state_dump.py tests/fixtures/sample_udp.itch /tmp/dump_py
cpp/build/book_dump           tests/fixtures/sample_udp.itch /tmp/dump_cpp
diff -r /tmp/dump_py /tmp/dump_cpp        # MUST be empty — bit-identical state
```
(Also run both on `sample_udp_head.itch` for a fast iteration loop.)

**Pitfalls to avoid:** THE phase where subtle divergence creeps in. Trade price comes from the **stored passive order**, not the `E` message. `U` inherits side/ticker from the ORIGINAL order, price/qty from the message. Do not remove an order on `E` until remaining hits exactly 0. Canonical dumps must not use `std::unordered_map` iteration order — sort explicitly before writing. Match the prototype's orphan/collision counting semantics exactly (read its code, `jnxfeed/book/orderbook.py`, before writing C++).

---

### Phase F3 — Wire format: record codec (C++ + Python) and frozen spec

**Objective:** the single record codec of §3 implemented in C++ (`cpp/wire/`) and Python (`jnxweb/records.py`), with a frozen byte-layout document and cross-language golden vectors.

**Implementation steps:**
1. Write `docs/wire_spec.md`: exact offset/size/type table for the header and every kind's body (§3 fields in the order listed). This document is the contract; both codecs cite it.
2. `cpp/wire/record.h/.cpp`: `struct UpdateRecord` (host-order POD mirroring §3) + encode/decode via `endian.h`; same for ORDER/TICK/HELLO/SYNC_END; a `RecordFramer::feed()` for the UDS stream (reuses the Soup framer pattern); builder `make_update(const Market&, const ApplyResult&, meta...)` assembling a full UPDATE from market core state (top-10 extraction from levels).
3. `jnxweb/records.py`: decode-only Python port (struct.Struct, 3.6-safe) for UPDATE + header; used by the web client, `tools/mcast_spy.py`, and tests.
4. Golden vectors: a C++ test tool emits `cpp/test/vectors/records.bin` (one of each kind with edge values: NO_PRICE, empty book, full 10 levels, `U` delta); a pytest (`tests/unit/test_records_py.py`) decodes it with `jnxweb/records.py` and asserts every field; `python3 tools/py36check.py jnxweb/records.py` clean.
5. `static_assert` (or runtime CHECK) pinning encoded UPDATE size; version byte checked on decode, mismatch → reject with reason.

**Boundary conditions:** book with <10 levels (level_count + zero-fill); ticker with no static yet (`directory_seen=0`, zeroed static section); no trades yet (all-zero T5 section); delta op `'#'` (sync rows — no order fields); body_len must match kind's expected size or decode fails.

**Verification criteria:**
```
make -C cpp test                                  # includes record round-trip + framer fragmentation tests
python3 -m pytest tests/unit/test_records_py.py -q
python3 tools/py36check.py jnxweb/records.py
grep -c '^|' docs/wire_spec.md                    # spec table exists (sanity)
```

**Pitfalls to avoid:** after this phase the layout is FROZEN — any later change bumps `version` and updates both codecs + vectors in one commit. Python `struct` format strings must be big-endian (`'>'`) everywhere. Don't let the C++ struct's in-memory layout leak into encode (no `sizeof(struct)` on the wire). Zero-fill ALL padding — records go on the network; no uninitialized bytes (valgrind check in F8).

---

### Phase F4 — `jnxdb`: the in-memory database process

**Objective:** a standalone C++ process: UDS ingest applying records atomically to the 5 tables (§2), the HELLO/GET_STATE/SYNC recovery protocol, and a human/tool query interface.

**Implementation steps:**
1. `cpp/db/tables.*`: the 5 tables + meta + tick_tables as plain structs over `std::map`/`unordered_map`; `apply_update(const UpdateRecord&)` = upsert T1/T2/T4/T5 wholesale from the record + mutate T3 from the delta + append tape ring; `apply_order/tick`, `reset()`, plus `dump_state(sink)` streaming SYNC_BEGIN → all TICK/ORDER records → one UPDATE per (ticker,group) with trigger `'#'` → SYNC_END(meta).
2. `cpp/db/ingest.*`: UDS listener (single FH connection at a time; a second connect kicks the first with a WARN), `poll()` loop, RecordFramer, protocol: HELLO handshake (reply with epoch+last_seq), GET_STATE → `dump_state`, RESET, SYNC_BEGIN/END bracketing, UPDATE dup-check (`exch_seq <= last && same epoch` → drop+count).
3. `cpp/db/query.*`: TCP listener on `127.0.0.1:26401` (configurable), line protocol, one thread or same poll loop (keep it in the same poll loop — single-threaded process, zero locking): `GET <ticker>` (all tables merged, key=value lines), `BOOK <ticker>`, `ORDERS <ticker>`, `TRADES <ticker>`, `TABLE static|state|trades` (CSV), `STATS` (msg counts, dup drops, last seq/session/epoch, table sizes), `PING`. Terminate responses with a lone `.` line.
4. `jnxdb_main.cpp`: cfg (sock path, query port), signal handling (SIGINT/SIGTERM → clean exit), startup/shutdown INFO logs.
5. `tools/dbquery.py` (3.6-safe): `python3 tools/dbquery.py [--port] CMD...` prints the response — the standard verification probe.
6. Tests: `cpp/test/test_tables.cpp` (apply semantics, dup rejection, dump/re-apply round-trip: dump into a second Tables instance → identical); integration pytest `tests/integration/test_jnxdb.py` that starts `jnxdb`, feeds records built by a small python encoder (extend `jnxweb/records.py` with encode for test use), queries, kills.

**Boundary conditions:** FH connection drop mid-record (framer discards partial, next connect starts clean); UPDATE for never-seen ticker (upsert creates rows); ORDER delta `D`/`E`-to-zero for unknown order (count, ignore); query for unknown ticker (`ERR unknown` + `.`); oversized/garbage frame (log, close ingest connection — a corrupt stream is unrecoverable by design); dump while ingest connected = same connection, sequential (single loop makes this trivially atomic).

**Verification criteria:**
```
make -C cpp test
python3 -m pytest tests/integration/test_jnxdb.py -q
# Manual smoke:
cpp/build/jnxdb --sock=/tmp/db.sock --query-port=26401 &
python3 tools/dbquery.py PING            # -> PONG
python3 tools/dbquery.py STATS           # -> zeroed counters, empty tables
```

**Pitfalls to avoid:** keep it **single-threaded** — one `poll()` over {ingest listener, ingest conn, query listener, query conns}; the moment a worker model adds threads+mutexes, atomicity bugs follow. Query responses can be large (TABLE) — write with a per-connection output buffer drained on POLLOUT, don't block the ingest path on a slow query client. `SO_REUSEADDR` on the query port. Unlink the UDS path on startup and shutdown.

---

### Phase F5 — `jnxfh`: the feed handler process

**Objective:** the C++ FH: Soup client with reconnect-resume, GLIMPSE bootstrap, market core application, one UPDATE per exchange message to DB + multicast — proven live against the Phase-1 Python simulator.

**Implementation steps:**
1. `cpp/fh/reactor.*`: minimal `poll()` loop with monotonic timers (port of the prototype's T4.0 design; ~150 LoC).
2. `cpp/fh/soupclient.*`: sans-I/O session state machine (login, self-counted seq from Login-Accepted, 1 s client heartbeats, 15 s dead-man) + TCP glue with reconnect (1 s→10 s capped backoff) re-logging-in with same session at next expected seq. Mirror `jnxfeed/soup/session.py` behavior exactly.
3. `cpp/fh/glimpse.*`: Soup session to the GLIMPSE port (blank requested session, seq 1), apply snapshot msgs to Market until `G`, return next live seq.
4. `cpp/fh/publish.*`: DB link (UDS connect, HELLO, RESET/SYNC dump from local Market, blocking framed writes, on failure → background reconnect loop while live flow continues and a `db_connected=false` gauge; on reconnect → HELLO, epoch mismatch/empty → full SYNC dump, then resume) + multicast sender (one `sendto` per UPDATE; `IP_MULTICAST_LOOP=1`, configurable TTL/interface).
5. `cpp/fh/recover.*`: startup decision tree of §1's restart matrix: connect DB → HELLO/GET_STATE → if DB has state: rebuild Market from dump, login at last_seq+1, same session; else per `--bootstrap=glimpse|replay|resume-only`.
6. `jnxfh_main.cpp`: cfg (itch host/port, glimpse host/port, user/pass, session, db sock, mcast group/port/ttl/interface, bootstrap mode), main flow: bootstrap → live loop: soup packet → itch decode → `market.apply` → `make_update` → DB write + mcast send → periodic (5 s) STATS log line (msgs/s, seq, books, orders, db_connected, pub_seq).
7. `tools/mcast_spy.py` (3.6-safe): join group, decode UPDATEs via `jnxweb/records.py`, print one line each (`seq ticker trigger best_bid/best_ask last_trade`), `--stats` mode for counts+gaps.
8. Tests: unit (soup session state machine vs scripted byte sequences — reuse F1 framer); integration `tests/integration/test_jnxfh.py`: start Python simulator (`python3 -m jnxfeed sim ...` — check exact prototype invocation in `jnxfeed/sim/exchange.py`) on the head fixture + `jnxdb` + `jnxfh`, then assert via `dbquery.py` and `mcast_spy.py`.

**Boundary conditions:** exchange disconnect mid-stream (resume at next seq, no duplicate UPDATE — assert pub gap-free); Soup login reject codes A/S (log + exit nonzero, no retry storm); `Z`/system-event `C` (clean shutdown: final stats, logout, exit 0); DB down at startup (retry loop with WARN, `--require-db` to fail fast); DB dies mid-session (keep multicasting, resync on return); multicast send failure (count, never fatal); GLIMPSE snapshot containing ref-price `A`s and `F` orders.

**Verification criteria:**
```
make -C cpp test
python3 -m pytest tests/integration/test_jnxfh.py -q
# Manual end-to-end on the full golden sample (exact numbers must match):
cpp/build/jnxdb --sock=/tmp/db.sock &
python3 -m jnxfeed sim --itch tests/fixtures/sample_udp.itch --port 15001 --glimpse-port 15002 &
cpp/build/jnxfh --itch-host=127.0.0.1 --itch-port=15001 --db-sock=/tmp/db.sock \
                --bootstrap=replay --mcast=239.192.1.1:26400 &
python3 tools/mcast_spy.py --stats --until-idle   # -> updates=216346, gaps=0
python3 tools/dbquery.py STATS                    # -> last_seq=234751, updates=216346, dups=0
# (216346 = 222189 total msgs − 5843 'T' timestamp msgs, which update the clock but publish nothing)
# DB state equals direct replay:
python3 tools/dbquery.py TABLE state > /tmp/db_state.csv   # (+ orders/trades tables)
# compare against cpp/build/book_dump output via tools/compare_db_dump.py (write it in this task)
```

**Pitfalls to avoid:** every exchange message must produce exactly one UPDATE — including `H`/`Y`/`S`/`L`/`R`/ref-price-`A` (they change state/static sections; trigger char tells consumers why). Do NOT publish during GLIMPSE snapshot application or state recovery as if live — those rows go out as the RESET+SYNC dump / with trigger `'#'`, then live publishing starts. `T` (timestamp) messages update the clock but publish nothing (decision: no UPDATE for `T` — nothing ticker-visible changed): published count = 216346, while `itch_replay` still sees 222189 decoded. Multicast on loopback needs `IP_MULTICAST_LOOP` AND binding/joining on the right interface — test flakiness here is environmental, add a `--mcast-if=127.0.0.1` option. Blocking UDS writes are fine (DB is fast) but wrap with a 5 s SO_SNDTIMEO so a wedged DB surfaces as reconnect, not a hung FH.

---

### Phase F6 — Restart & recovery, end-to-end proof

**Objective:** demonstrate the §1 restart matrix holds: kill anything mid-day and final state is identical to an uninterrupted run.

**Implementation steps:**
1. `tools/run_e2e.py` (3.14 OK — dev-only): orchestrates sim + jnxdb + jnxfh with scripted actions at message counts/timestamps (`kill_fh@N`, `kill_db@N`, `drop_exchange@N` via the simulator's scripted-disconnect feature), paced replay mode, collects logs, ends by dumping DB tables + mcast spy stats to a results dir.
2. Baseline: uninterrupted paced run over the head fixture → canonical DB dump = `expected/`.
3. Scenarios (each a pytest in `tests/integration/test_recovery.py`, comparing final DB dump to baseline):
   a. FH killed (SIGKILL) at ~40%, restarted: must GET_STATE from DB, resume at last_seq+1 (assert via logs: `resume seq=<N>` where N-1 = DB's last seq at kill), no dup drops in DB, final dump identical.
   b. DB killed at ~40%, restarted after 2 s: FH stays live (mcast gap count 0 during outage), resyncs; final dump identical.
   c. Exchange dropped at ~40% (simulator disconnect): soup resume; final dump identical; pub_seq contiguous.
   d. Cold start with GLIMPSE bootstrap at a mid-file cut: final dump equals a from-seq-1 replay's dump (extends the prototype's T6.2 invariant across the C++ stack).
   e. FH killed AND DB killed: restart both → bootstrap path (GLIMPSE) → final dump identical.
4. Add `RECOVERY.md` runbook section: what operators do per failure, expected log lines.

**Boundary conditions:** FH killed *between* DB write and mcast send (acceptable: that one mcast datagram may be lost — clients self-heal; DB is authoritative — document this ordering guarantee: DB write happens FIRST); FH restart when DB's epoch is newer than exchange session (stale DB from a previous session → detect session mismatch in HELLO meta → RESET + bootstrap); kill during initial SYNC dump (DB discards partial sync: SYNC_BEGIN without SYNC_END on disconnect → wipe + WARN).

**Verification criteria:**
```
python3 -m pytest tests/integration/test_recovery.py -q     # all 5 scenarios green
python3 tools/run_e2e.py --scenario kill_fh --fixture tests/fixtures/sample_udp_head.itch
# prints PASS + paths to identical dumps
```

**Pitfalls to avoid:** flaky timing — trigger scripted events on *message counts* observed via DB STATS polling, not sleeps. SIGKILL only (SIGTERM tests graceful path separately) — recovery must work from an unclean death. Don't "fix" a failing comparison by relaxing the diff; state divergence here is a real bug, bisect it with smaller fixtures. The partial-SYNC wipe rule (SYNC_BEGIN w/o END) must not trigger on the *live-update* path.

---

### Phase F7 — `jnxweb`: Python 3.6 web GUI client

**Objective:** stdlib-only Python 3.6 process: joins the multicast, caches latest state per ticker, serves a self-contained web page where an operator picks a ticker and watches book/trades/state update live over WebSocket.

**Implementation steps:**
1. `jnxweb/mcast.py`: multicast receiver (socket + `IP_ADD_MEMBERSHIP` via `struct.pack('4s4s', ...)`, SO_REUSEADDR, bind to group port), non-blocking, registered in a `selectors` loop shared with the HTTP server (single thread; mirror the prototype's reactor pattern).
2. `jnxweb/state.py`: `dict ticker -> decoded UpdateRecord` + per-ticker recent-trades ring (last 50, appended when trigger==`'E'`) + global stats (updates, gaps via pub_seq, last epoch — on epoch change, clear all state: FH restarted with fresh session).
3. `jnxweb/httpd.py`: hand-rolled HTTP on the selectors loop (do NOT use `http.server`'s threading model — keep one loop): routes `GET /` (the page), `GET /tickers` (JSON list), `GET /snap/<ticker>` (JSON full state), `GET /ws` → WebSocket upgrade.
4. `jnxweb/wsock.py`: minimal RFC 6455 **server**: handshake (`Sec-WebSocket-Accept` = base64(sha1(key + GUID))), server→client text frames (no masking server-side), client frame parsing only for close/ping (reply pong), fragmentation not supported (documented). Per-client subscription: client sends `{"sub": "8306"}`; server pushes that ticker's JSON on every update (coalesce: at most 10 pushes/s per client, latest wins).
5. `jnxweb/static_page.py`: one embedded HTML+JS+CSS string (no external assets): ticker input + list, static/state panel, 10-level book table (bid qty/price | price/ask qty), last-50 trades, stats footer (updates/s, gaps, connection state), auto-reconnecting WebSocket.
6. `jnxweb/__main__.py`: args `--mcast 239.192.1.1:26400 --http-port 8080 --mcast-if 127.0.0.1`.
7. Tests (run on 3.14, code 3.6-safe): pytest driving a real socket pair — feed canned UPDATE records into the mcast socket path (or a `--test-feed` UDS injection mode), then: HTTP GET assertions via `urllib.request`; WebSocket handshake + first frames via a raw-socket test helper (hand-roll the client side in the test: send handshake, unmask/mask per RFC — client frames MUST be masked); `tools/py36check.py` over the whole package.

**Boundary conditions:** ticker never seen (snap → 404 JSON, ws sub → `{"error":"unknown"}` until first update arrives, then data flows); epoch change mid-session (clear + banner "feed restarted"); slow/gone browser (send buffer cap 256 KB → drop client with WARN, never block the loop); multiple browsers on different tickers; datagram with bad magic/version (count + ignore); pub_seq gaps (stats only — full-state records self-heal).

**Verification criteria:**
```
python3 tools/py36check.py jnxweb/*.py            # clean — 3.6-compat gate
python3 -m pytest tests/unit/test_jnxweb*.py -q
# Live demo against the full stack (F5 commands running):
python3 -m jnxweb --http-port 8080 --mcast-if 127.0.0.1 &
curl -s localhost:8080/tickers | head -c 200      # JSON ticker list appears as sim replays
curl -s localhost:8080/snap/<some-ticker>         # full state JSON with book levels
python3 tools/ws_probe.py localhost:8080 <ticker> --frames 5   # (test helper) prints 5 live ws pushes
```

**Pitfalls to avoid:** the #1 risk is 3.7+ idioms — `asyncio` is banned entirely; `websockets`/`aiohttp` don't exist here (no pip). The WebSocket accept-key GUID is `258EAFA5-E914-47DA-95CA-C5AB0DC85B11` — hardcode and unit-test the RFC example value. Frames from browser are masked, frames to browser are NOT. Don't buffer unbounded JSON per slow client. `SO_REUSEADDR` before bind. All JSON via `json` module with sorted keys (stable tests). Prices: render raw int ÷ 10 with one decimal in JS, keep JSON as raw ints.

---

### Phase F8 — System hardening, benchmark, ops, target validation

**Objective:** the assembled system is measured, leak-checked, documented, and packaged with an exact RHEL 8.10 validation procedure.

**Implementation steps:**
1. Benchmark: `make -C cpp bench` — as-fast-as-possible full-sample run through sim→jnxfh→jnxdb, report msgs/s at each stage (decode-only via `itch_replay`, apply-only via `book_dump`, full pipeline) into `README.md` table next to the Python 379k/s figure. Acceptance floor: full pipeline ≥ 500k msgs/s on the dev box (expect millions on decode).
2. `valgrind --leak-check=full --error-exitcode=1` (if valgrind present in dev container — else `-fsanitize=address,undefined` build target `make -C cpp test-asan`) over unit tests and a head-fixture pipeline run; fix everything.
3. 8-hour soak target (run at least 30 min in CI-style check): paced sim on loop, assert RSS of jnxdb+jnxfh plateaus (orders table churns; tape rings are bounded; there must be NO per-message allocation growth) — add `STATS` RSS reporting.
4. `OPERATIONS.md`: start order (any — components tolerate absence), config files (`etc/jnxfh.cfg`, `etc/jnxdb.cfg`, `etc/jnxweb.cfg` samples), log/monitoring cheatsheet (the 5 s stats lines, dbquery STATS fields), failure playbook (from F6), simulator-based dry-run instructions.
5. Source tarball target `make -C cpp dist` (sources + Makefile + configs + docs; built ON the target, not shipped as binaries).
6. `TARGET_VALIDATION.md` — the RHEL checklist (§8 below) as a copy-paste script.

**Boundary conditions:** bench must exclude sim pacing (as-fast-as-possible mode); ASAN build without `-Werror`-breaking interactions (separate flags var); soak with simulated day rollover = restart-both path (reuse F6 scenario e).

**Verification criteria:**
```
make -C cpp bench                    # table printed; pipeline ≥ 500k msg/s
make -C cpp test-asan                # zero ASAN/UBSAN reports
python3 tools/run_e2e.py --scenario soak --minutes 30    # PASS, RSS delta < 5% after warmup
ls dist/jnx-fh2-*.tar.gz
```

**Pitfalls to avoid:** don't chase micro-optimizations before ASAN is clean. RSS plateau ≠ first 5 minutes flat — orders peak intraday; use the fixture-loop steady state. Keep `-O2` for bench builds, ASAN builds separate (`build-asan/`). The dist tarball must build with `make -C cpp all` on a machine with ONLY gcc-c++ and make installed — no git, no python needed for the C++ build.

---

## 6. Dependency graph & parallelism

```
F0 → F1 → F2 ─┬→ F5 → F6 → F8
F0 → F3 ──────┤        ↑
F3 → F4 ──────┘        │
F3 → F7 (independent of F4/F5 until live demo) ──┘
```
F1‖F3 after F0; F2‖F4 after their inputs; F7 can start right after F3. Each phase is a user approval gate: run its Verification Criteria, review, commit, proceed.

## 7. Risks

- **C++ semantic drift from the proven prototype** — mitigated by the F2 bit-identical dump diff (the single most important gate in this plan) and F1 golden vectors generated *by* the prototype.
- **gcc 15 vs gcc 8.5 divergence** — `-std=c++11 -pedantic -Werror` catches syntax/library use; behavior differences are unlikely in this codebase's C++ subset; final compile on target (§8) is the backstop.
- **Python 3.6 vs 3.14 drift in `jnxweb`** — `py36check.py` gate + JNX_PLAN.md §0 rules embedded in F7's prompt; residual risk is stdlib behavior differences (small for socket/selectors/json/struct); first run on the target box is a §8 item.
- **No test line** — everything runs against the Phase-1 simulator + golden fixtures; the F5/F6 assertions on exact message counts make regressions loud. Real-line differences remain possible; the Phase-1 CONNECTIVITY.md probes still apply (open questions Q1–Q5 there remain open).
- **Multicast in production network** — group/TTL/interface are config; validate with `tools/mcast_spy.py` on the target LAN early (it's pure Python 3.6, runs anywhere).
- **Order-number collisions across groups** (JNX_PLAN.md §3.3(3)) — same mitigation as prototype: counter + WARN, surfaced in dbquery STATS and the F5 flags bit.

## 8. Target-machine validation checklist (run on RHEL 8.10 before go-live)

1. `gcc --version` → 8.5.x; `python3 --version` → 3.6.x.
2. Unpack dist tarball; `make -C cpp all test` → clean build (`-Werror`), all tests pass.
3. `python3 tools/py36check.py jnxweb/*.py && python3 -m jnxweb --help` → runs on real 3.6.
4. Loopback dry run: copy `tests/fixtures/sample_udp_head.itch` + prototype sim (needs python3.6 — the prototype targets it) → run the F5 manual smoke → dbquery STATS numbers match dev.
5. Multicast reachability test between FH host and client host with `mcast_spy.py`.
6. Then and only then: `CONNECTIVITY.md` probes against the Japannext UAT line.
