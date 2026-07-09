# Japannext PTS ITCH Feed Handler — Prototype Plan

**Transport decision:** SoupBinTCP (TCP) only. Japannext offers the same ITCH stream over SoupBinTCP and MoldUDP64; we consume only the TCP service. SoupBinTCP gives guaranteed in-order delivery and loss-free resume (reconnect + login with next expected sequence number), so no gap/re-request machinery is needed. MoldUDP64 appears in this plan only as an *offline* concern: unwrapping the official UDP sample pcap into test fixtures.

**Source specs** (in `/workspace/jnx-specs/`, PDF + `.txt` extractions):
- `JNX_ITCH_Market_Data_Specification_Equities_2.02` — the message set
- `JNX_GLIMPSE_Market_Data_Specification_Equities_2.01` — snapshot service
- `JNX_SoupBinTCP_Specification_1.01` — TCP transport
- `JNX_MoldUDP64_Specification_1.02` — reference only, for unwrapping the UDP sample
- `JNX_Data_File_Formats_2.03` — SFTP daily files incl. **ITCH Binary Data File** (raw full-day ITCH, 2-byte-length framed) and Stock Master CSV
- Official samples: `Japannext_PTS_ITCH_Equities_v1.7.UDP.pcap` (26 MB — richest data), `..._v1.7.TCP.pcap`, `..._GLIMPSE_Equities_v1.4.pcap`

**Goal:** a working prototype that can (1) connect to the Japannext ITCH equities feed over SoupBinTCP with correct login/heartbeat/resume behavior; (2) decode all ITCH v2.02 messages; (3) bootstrap either by full-session replay (login from seq 1) or mid-session via a GLIMPSE snapshot; (4) maintain reference data, per-book price-level order books, and a trade tape; (5) show it all moving in a simple CLI — plus an **exchange simulator** (SoupBinTCP ITCH + GLIMPSE servers) so everything is testable end-to-end offline, and a **connectivity kit** ready to run the day UAT access exists.

**Scope:** Equities only. Bonds specs are near-identical; adding them later is a small delta task (not planned here).

**Language:** pure Python, **stdlib-only at runtime**, targeting **Python 3.6.4** (RHEL 8.10). See §0 — this constraint shapes everything. C++ is out of scope for the prototype; revisit only if the Phase-6 benchmark says the target box can't keep up.

**Verified ground truth:** the official UDP sample decodes contiguously with the §3 tables (222,189 messages, seq 12562→234751, session `1697659284`, zero gaps; observed sizes A=30 E=25 D=13 U=29 T=5 S=10 H=14 Y=14 — all matching §3.2). The capture starts mid-session (seq 12562), so it contains no R/L/F messages — directory handling must tolerate that, and golden tests use these exact numbers. TCP samples confirm SoupBinTCP framing (ITCH on port 15001, GLIMPSE on 15002).

---

## 0. Python 3.6.4 ground rules (embed in EVERY task prompt)

Every task must run on Python 3.6.4. Modern idioms silently target 3.7+; these are **forbidden**:

- ❌ `dataclasses` → use `typing.NamedTuple` class syntax (defaults OK on 3.6.1+) or `__slots__` classes.
- ❌ `asyncio` entirely (3.6 asyncio lacks `asyncio.run`/`get_running_loop`; too many 3.7+ habits to police) → all I/O is **non-blocking sockets driven by a `selectors.DefaultSelector` reactor** (§1). Protocol logic is sans-I/O and synchronous.
- ❌ `subprocess.run(capture_output=…, text=…)` → `stdout=PIPE, stderr=PIPE, universal_newlines=True`.
- ❌ walrus `:=`, f-string `=`, positional-only `/`, `dict | dict`, `time.time_ns()`, `breakpoint()`, `from __future__ import annotations`.
- ❌ `typing.Literal/Protocol/Final`; keep hints to 3.6-safe forms.
- ❌ relying on plain-`dict` iteration order (implementation detail in 3.6) → `collections.OrderedDict` where order matters.
- ✅ allowed: f-strings, `struct.Struct`, `memoryview`, `int.from_bytes`, `enum`, `selectors`, `socket`, `pathlib`.
- Packaging: `setup.py` + `setup.cfg` (no pyproject-only build; pip on 3.6 is old). Package name **`jnxfeed`**, console entry `python -m jnxfeed`.
- Dev/test: `pytest==7.0.1` (last 3.6-compatible). Tests must run **on 3.6** — provide `Dockerfile.dev` based on `registry.access.redhat.com/ubi8/python-36` and a `make test` that runs pytest inside it (falls back to local `python3.6` if present). A task is not done until its tests pass under 3.6.

## 1. Architecture

```
                     ┌───────────────────────────────────────────┐
                     │              FeedHandler (T5.2)           │
                     │  bootstrap: GLIMPSE snapshot → ITCH from  │
                     │  seq N   |   or ITCH replay from seq 1    │
                     └────────────────────┬──────────────────────┘
                                          │ TCP
                     ┌────────────────────▼──────────────────────┐
                     │        SoupBinTCP client session          │◄──► ITCH host   (e.g. :15001)
                     │  login / seq tracking / heartbeats /      │◄──► GLIMPSE host (e.g. :15002)
                     │  reconnect-resume        (T4.1 + T4.0)    │
                     └────────────────────┬──────────────────────┘
                                          │ in-order ITCH payloads
                     ┌────────────────────▼──────────────────────┐
                     │   ITCH decoder (schema + codec, T2.x)     │
                     │   also fed by pcap / .itch readers (T3.1) │
                     └───┬──────────────┬──────────────┬─────────┘
                         ▼              ▼              ▼
                  ┌──────────┐   ┌───────────┐   ┌───────────┐
                  │ RefData  │   │ OrderBook │   │ TradeTape │
                  │ (T5.1a)  │   │ (T5.1b)   │   │ (T5.1c)   │
                  └──────────┘   └───────────┘   └───────────┘

 I/O core: single-threaded selectors reactor (T4.0) — sockets non-blocking,
 protocol objects are sans-I/O state machines fed bytes, returning bytes/events.
 Test side: Exchange simulator (T6.1) = SoupBinTCP ITCH server + GLIMPSE server,
 replaying .itch fixture files.
```

**Design rules (apply to every task):**
- Layers communicate through plain data: the transport yields `bytes` payloads; the codec turns them into NamedTuples; consumers implement `apply(msg)`. No layer imports a layer above it.
- All protocol/framing/book logic is **sans-I/O pure functions or state machines** (feed bytes in, get messages/actions out) — unit-testable without sockets. Only the thin reactor layer (T4.0/T4.1) touches sockets.
- The decoder is zero-policy: no filtering, no book logic. Business semantics live in `book/`.
- One replay abstraction: live session, pcap reader, and `.itch` file reader all produce the same `(seq, bytes)` stream, so every CLI view works identically on live and recorded data.

## 2. Repository layout

```
jnx-feed/
  setup.py  setup.cfg           # package jnxfeed; runtime deps: none; dev: pytest==7.0.1
  Makefile                      # test / test-docker / lint targets
  Dockerfile.dev                # ubi8/python-36 test environment
  README.md  CONNECTIVITY.md    # CONNECTIVITY.md from T3.3
  jnxfeed/
    __init__.py  __main__.py
    types.py                    # Price helpers, Group/Side/State enums, sentinels   (T1.1)
    itch/
      schema.py                 # declarative field tables (mirror of plan §3.2)     (T2.1)
      messages.py               # NamedTuple per message type                        (T2.1)
      codec.py                  # decode/encode driven by schema                     (T2.2/T2.3)
    soup/
      packets.py                # SoupBinTCP framing codec (all 10 packet types)     (T2.4)
      session.py                # sans-I/O client session state machine              (T4.1)
    net/
      reactor.py                # selectors event loop, timers                       (T4.0)
      tcp.py                    # non-blocking TCP connector glue                    (T4.1)
    pcapio.py                   # stdlib pcap reader (linktype 1+VLAN, 113/SLL;
                                #  UDP extraction incl. Mold unwrap + in-order
                                #  TCP reassembly) — OFFLINE tooling only            (T3.1)
    itchfile.py                 # ITCH Binary Data File read/write (2-byte framing)  (T3.1)
    book/
      refdata.py                # directory, tick tables, states, ref prices         (T5.1a)
      orderbook.py              # order store + per-book price levels               (T5.1b)
      tape.py                   # trade tape from E messages                        (T5.1c)
      market.py                 # facade routing msgs to the above                  (T5.1d)
    glimpse.py                  # GLIMPSE snapshot client logic                     (T5.2)
    handler.py                  # FeedHandler bootstrap orchestrator                (T5.2)
    sim/exchange.py             # simulator servers                                 (T6.1)
    cli/                        # probe, capture, replay, tail, book, stats, static (T3.2/T7.1)
  tests/
    fixtures/                   # .itch files + golden manifests from official pcaps (T3.2)
    unit/  integration/
```

## 3. Canonical protocol reference (embed-in-task cheat sheet)

**Every task codes against THIS section, not a fresh reading of the PDFs.** Transcribed once from the specs and byte-verified against the official samples. Spec section refs given for audit.

### 3.1 Data types (ITCH spec §3)
- **Integer**: unsigned **big-endian**, sizes 1/2/4/8.
- **Alpha**: ASCII, left-justified, right-padded with spaces. Strip trailing spaces on decode; pad on encode.
- **Price**: unsigned big-endian 4-byte int, **1 implied decimal** (value is tenths of a yen: display `value/10`). Max valid `0x7FFFFFFE` = 214,748,364.6. **`0x7FFFFFFF` = "no reference price"** sentinel (only in reference-price `A` messages). Define `NO_PRICE = 0x7FFFFFFF` in `types.py`. (Note: differs from ASX — unsigned, 1 decimal, different sentinel.)
- **Quantity**: 4-byte unsigned int (not 8 — differs from ASX).
- **Orderbook Id**: 4-byte **Alpha** — the SICC code, e.g. `"8306"`. It is a string, NOT an integer (changed in spec v1.7).
- **Group** (order book group id, 4-byte Alpha): `DAY `=J-Market day, `NGHT`=J-Market night, `DAYX`=X-Market, `DAYU`=U-Market. Blank in `S` = system-wide.
- Timestamps: standalone `T` message carries seconds past midnight of session start day; every other message carries `ns:4` = nanoseconds since the last `T`.

### 3.2 ITCH message field tables (ITCH spec §4; GLIMPSE spec §5)

Notation `name:size:type`, offsets sequential from byte 0 = 1-char Message Type. `[len]` = total bytes — **verified against the official UDP sample for A/E/D/U/T/S/H/Y**.

| Type | Meaning | Fields after type byte | [len] |
|------|---------|------------------------|-------|
| `T` | Timestamp – Seconds | seconds:4:num | 5 |
| `S` | System Event | ns:4:num, group:4:alpha (blank=system-wide), event:1:alpha — `O` start of messages, `S` start of system hours, `Q` start of market hours, `M` end of market hours, `E` end of system hours, `C` end of messages. (GLIMPSE snapshots contain only `O`/`C`.) | 10 |
| `L` | Price Tick Size | ns:4:num, tick_table_id:4:num, tick_size:4:num, price_start:4:num (price-scaled, 1 decimal) | 17 |
| `R` | Orderbook Directory | ns:4:num, orderbook_id:4:alpha (SICC), isin:12:alpha, group:4:alpha, round_lot:4:num, tick_table_id:4:num, price_decimals:4:num (always 1), upper_limit:4:price, lower_limit:4:price | 45 |
| `H` | Trading State | ns:4:num, orderbook_id:4:alpha, group:4:alpha, state:1:alpha (`T`=Trading, `V`=Suspended) | 14 |
| `Y` | Short Selling Price Restriction | ns:4:num, orderbook_id:4:alpha, group:4:alpha, state:1:alpha (`0`=none, `1`=in effect) | 14 |
| `A` | Order Added | ns:4:num, order_number:8:num, side:1:alpha (B/S), qty:4:num, orderbook_id:4:alpha, group:4:alpha, price:4:price | 30 |
| `F` | Order Added w/ Attributes | all `A` fields, then attribution:4:alpha (reserved, blank), order_type:1:alpha (`Q`=DLP order) | 35 |
| `E` | Order Executed | ns:4:num, order_number:8:num, executed_qty:4:num, match_number:8:num | 25 |
| `D` | Order Deleted | ns:4:num, order_number:8:num | 13 |
| `U` | Order Replaced | ns:4:num, orig_order_number:8:num, new_order_number:8:num, qty:4:num, price:4:price | 29 |
| `G` | End of Snapshot (**GLIMPSE only**) | sequence_number:8:num (binary, NOT ASCII — differs from ASX) | 9 |

### 3.3 Semantics gotchas (order book correctness)
1. **Reference-price messages**: an `A` with `order_number == 0` is NOT an order — it carries the reference price (price may be `NO_PRICE`); side/qty are to be ignored. Route to refdata, never to the book. A manual ref-price update mid-session arrives as another such `A`.
2. **`E`/`D`/`U` carry no orderbook id, side, or price** — the handler must keep an order store keyed by order number holding `(orderbook_id, group, side, price, remaining_qty)`. `E` reduces remaining qty (remove order at zero; executions are cumulative per order); the **trade price is the passive order's stored price** (spec §4.8: "execution price may be derived from the passive order price"). `U` removes the original order and inserts the new one **inheriting book/side from the original**; new price/qty from the message.
3. **Order numbers are "unique per day per order book group"** (spec §4.7) but `E`/`D`/`U` don't carry the group → cross-group collision is theoretically possible on a combined feed. Prototype decision: key by order number alone, keep group in the record, **count and WARN on any key collision**. Open question Q1 for Japannext.
4. **Trading-state / short-sell spins** (spec §4.5/§4.6): before start of system hours a spin sends `H` for all books eligible to trade (absent ⇒ suspended) and `Y` for all books with restriction in effect (absent ⇒ none). Refdata defaults must encode those absence semantics.
5. **Directory may be missing**: a capture/session joined mid-stream (like the official UDP sample, which starts at seq 12562) contains no `R`/`L`. Books must auto-create on first reference to an unknown orderbook id, flagged `directory_missing`.
6. `F` (DLP orders) are normal book orders with an attribute — same book handling as `A`.

### 3.4 SoupBinTCP (Soup spec §4–§5) — the transport
- **Logical packet** = `length:2:num` (big-endian, excludes the length field itself) + `type:1:char` + payload. TCP stream ⇒ framing must handle partial reads (feed-bytes state machine).
- Server→client: `A` Login Accepted — session:10:alnum (LEFT-padded w/ spaces), seq:20:ASCII digits (LEFT-padded w/ spaces) = seq of next sequenced message; `J` Login Rejected — code:1 (`A` not authorized / bad user↔port pairing, `S` session unavailable), then socket closed; `S` Sequenced Data — **one ITCH message per packet**, client counts seq itself starting from the Login-Accepted value (first message of a session is seq 1); `H` server heartbeat; `Z` End of Session then close; `+` debug (ignore).
- Client→server: `L` Login Request — username:6:alnum (right-padded), password:10:alnum (right-padded), requested_session:10 (LEFT-padded, blank = current), requested_seq:20:ASCII (LEFT-padded, `0` = "most recent", `1` = full session replay, else next seq desired); `U` unsequenced data (unused for market data); `R` client heartbeat; `O` logout.
- Heartbeats: each side sends after >1 s idle; assume connection dead after **15 s** silence → reconnect and re-login with maintained session + next expected seq to resume **without loss** (this replaces all gap-recovery machinery a UDP feed would need).
- Username↔TCP-port pairs are fixed per assignment — no mix-and-match (reject code `A`).
- Sample ground truth: ITCH-TCP on port 15001, GLIMPSE on 15002.

### 3.5 GLIMPSE (GLIMPSE spec §4–§5)
- Same SoupBinTCP transport, same ITCH message formats, but a **snapshot**: connect, log in (**Requested Session MUST be blank** or login is rejected; use requested seq 1 — confirm, Q3), receive `T`/`S`(O,C only)/`L`/`R`/`H`/`Y`/`A`/`F` describing current state (open orders arrive as `A`/`F`), then **`G` End of Snapshot** whose 8-byte binary seq = **next seq of the real-time ITCH feed** — resume the live ITCH session from there. Trading-state/short-sell absence semantics as in §3.3(4).

### 3.6 Bootstrap / recovery algorithm (implemented by T5.2)
Two modes, selectable:
1. **Full replay** (default for prototype/UAT): log in to ITCH-TCP with requested seq 1, replay the whole session through `Market`, then stay live. Simple, exercises everything; cost = replaying the day so far.
2. **Snapshot sync**: connect GLIMPSE, apply the whole snapshot to a fresh `Market`, read next-live seq `N` from `G`, then log in to ITCH-TCP with requested seq `N` and go live. Fast mid-session join.
- On TCP loss in either mode: reconnect, re-login with same session + next expected seq (§3.4), continue — no messages lost.
- On Soup `Z` / ITCH `S` event `C`: session ended, stop cleanly.

### 3.7 Offline sample-data formats (fixture tooling only — NOT live transport)
- **pcap**: classic format; official samples use linktype 1 Ethernet (+possible VLAN tags) for the UDP capture and 113 Linux-cooked SLL for the TCP/GLIMPSE captures. TCP captures need minimal in-order reassembly (clean captures — per-direction seq splice suffices).
- **MoldUDP64 unwrap** (for the UDP sample only): each UDP payload = 20-byte header — `session:10:alpha`, `sequence:8:num`, `count:2:num` (0 = heartbeat, skip; 0xFFFF = end of session) — followed by `count` blocks of `length:2:num + ITCH message`. Sample: multicast 232.66.1.2:11002, session `1697659284`, 8,842 heartbeats.
- **ITCH Binary Data File** (`.itch`, Data File Formats spec §10): repeated `length:2:num + ITCH message`. This is both Japannext's SFTP full-day format and our native fixture/replay/capture format.

## 4. Task breakdown

Rules for dispatching tasks to Sonnet/Opus agents:
- Each task prompt embeds **§0 (Python rules) + §3 (protocol cheat sheet) + the task text**. Never hand agents the PDFs.
- Each task ships its own unit tests; "done" = tests green under Python 3.6 (`make test`).
- Sizes: S ≈ ≤150 LoC, M ≈ 150–400, L = the big one. Dependencies noted; tasks without a mutual dependency can run in parallel.

### Phase 0 — Foundation
**T1.1 — Scaffolding + shared types** *(S)*
- Repo layout of §2, `setup.py`/`setup.cfg` (`python_requires>=3.6`), `Dockerfile.dev` (ubi8/python-36, pip install pytest==7.0.1), `Makefile` (`test` local, `test-docker`), `types.py`: `NO_PRICE`, `price_to_str` (1 decimal), `Side`, `Group` constants, absence-semantics defaults.
- **Accept:** `make test-docker` runs trivial types tests green on 3.6; README documents both test paths.

### Phase 1 — Codecs (all sans-I/O, parallelizable after T1.1)
**T2.1 — ITCH schemas + message NamedTuples** *(M)* — table of §3.2 → declarative `schema.py` + one NamedTuple per type in `messages.py`.
- **Accept:** test asserts every schema's computed length equals §3.2 `[len]`; NamedTuple fields match schema order.

**T2.2 — ITCH binary decoder** *(M)* — after T2.1. Precompiled `struct.Struct` per type, `decode(buf) -> msg`, strict length check, alpha stripping, unknown-type → explicit error.
- **Accept:** hand-crafted byte vectors for all 12 types (incl. `G`), NO_PRICE sentinel, ref-price `A` (order_number 0), truncated-input errors.

**T2.3 — ITCH binary encoder** *(S)* — after T2.1, parallel w/ T2.2. Needed by simulator + fixture tooling.
- **Accept:** `decode(encode(m)) == m` round-trip for every type (share vectors with T2.2).

**T2.4 — SoupBinTCP framing codec** *(S)* — after T1.1. All 10 packet types of §3.4 + incremental `FrameBuffer.feed(bytes) -> packets` for partial reads.
- **Accept:** frame tests incl. byte-by-byte feeding, split length prefix; login request/accepted field padding exactness (left vs right padding!).

### Phase 2 — Sample-data validation + connectivity kit (EARLY on purpose: proves the codecs against real data and is the tool bag for the day real access arrives)
**T3.1 — pcap + .itch file I/O (offline tooling)** *(M)* — after T1.1. Stdlib pcap reader per §3.7: linktype 1 (+VLAN) and 113/SLL, UDP payload extraction **including Mold-header unwrap** (~40 lines, offline only), minimal in-order TCP reassembly. `.itch` file reader/writer.
- **Accept:** reads all three official pcaps; UDP extraction yields 224,754 packets / 8,842 heartbeats; `.itch` round-trip test.

**T3.2 — Golden decode of official samples + fixture extraction** *(M)* — after T2.2, T2.4, T3.1. Decode all three samples end-to-end; emit `tests/fixtures/sample_udp.itch` (+ small sliced fixtures) and a golden manifest.
- **Accept (exact numbers, pre-verified):** UDP sample → session `1697659284`, seq contiguous 12562→234751, 222,189 messages, counts `{A:128366, E:67902, D:10772, U:9287, T:5843, Y:16, S:2, H:1}`; TCP sample decodes as a Soup session (login accepted, sequenced ITCH on port 15001); GLIMPSE sample decodes fully and ends in `G` (assert its seq in the manifest). Zero decode errors.

**T3.3 — Connectivity kit + CONNECTIVITY.md** *(M)* — after T2.4 (parallel with T3.2). CLI probes runnable against UAT/prod the day access exists, each producing a timestamped diagnostic report:
  - `jnxfeed probe --host --port --user --pass [--seq N]` — TCP connect, Soup login, report accept/reject(+code), session id, server seq, first N decoded messages, heartbeat round-trip health, clean logout. Works for both ITCH and GLIMPSE endpoints.
  - `jnxfeed capture --host --port --user --pass --out day.itch [--seq 1]` — log in from seq 1 (or given), stream to a `.itch` file with a session/seq sidecar JSON — the **gather more sample data** tool; resumes after disconnects.
  - `CONNECTIVITY.md`: checklist of what to request from Japannext (ITCH + GLIMPSE hosts/ports per market group, credentials and the username↔port pairing rule, whether full-session replay from seq 1 is permitted/rate-limited, UAT slots — see their published Client Connectivity Testing Schedule) + the open questions in §6.
- **Accept:** probes run against a scripted localhost Soup stub included in the task (later re-pointed at the T6.1 simulator); capture output re-decodes cleanly with T3.1+T2.2.

### Phase 3 — Live transport
**T4.0 — selectors reactor** *(S)* — after T1.1. Minimal single-thread event loop: register sockets w/ callbacks, monotonic timers, clean shutdown. No protocol knowledge.
- **Accept:** loopback echo + timer-ordering tests.

**T4.1 — SoupBinTCP client session** *(M)* — after T4.0, T2.4. Sans-I/O session state machine (login, seq counting from Login-Accepted value, 1 s client heartbeats, 15 s death detection) + reactor TCP glue with reconnect-and-resume (re-login same session at next expected seq, per §3.4).
- **Accept:** tests against an in-process stub server: accept path, reject path (both codes), heartbeat exchange both ways, mid-stream disconnect → reconnect resumes at correct seq with no loss/duplication, `Z` ends session.

### Phase 4 — Book building (all sans-I/O; parallel with Phase 3 after T2.2)
**T5.1a — Reference data store** *(M)* — after T2.2. Consumes `R`/`L`/`H`/`Y`/ref-price `A`/`S`; holds the **static data table**: SICC, ISIN, group, round lot, tick table (assembled from `L` rows), price decimals, upper/lower limits, trading state, short-sell state, reference price, session events. Encodes §3.3(4) absence semantics and §3.3(5) auto-create.
- **Accept:** unit tests for spin semantics, ref-price update incl. NO_PRICE, tick-table assembly, unknown-book auto-create flag.

**T5.1b — Order store + price-level book builder** *(L — the core task)* — after T2.2. Implements §3.3(2): order-number-keyed store; `A`/`F` insert; `E` cumulative execution w/ passive-price trade derivation, remove at zero; `D` remove; `U` = delete original + insert new inheriting book/side; ref-price `A` excluded; collision counter (§3.3(3)); per-book aggregated price levels (sorted bid/ask) with top-N query.
- **Accept:** unit tests per rule above + property test on random message streams (book totals == sum of live orders); replay `tests/fixtures/sample_udp.itch` end-to-end with zero errors; orphan `E`/`D`/`U` from pre-capture orders counted and asserted (capture starts mid-session — a fixed, known number).

**T5.1c — Trade tape** *(S)* — after T5.1b (needs passive-price lookup). Rolling tape of `(time, book, price, qty, match_number)`, per-book cumulative volume/VWAP/last.
- **Accept:** tests incl. multi-fill order (several `E` same order number, one match each), tape totals vs. fixture golden counts.

**T5.1d — Market facade** *(S)* — after T5.1a–c. Single `Market.apply(msg)` routing everything; exposes refdata/books/tape; the only API the CLI and handler use.
- **Accept:** full fixture replay through `Market.apply` only, re-asserting T5.1a–c outcomes.

### Phase 5 — Snapshot & orchestration
**T5.2 — GLIMPSE client + FeedHandler orchestrator** *(M)* — after T4.1, T5.1d. GLIMPSE = a Soup session with blank requested-session applying snapshot messages to a `Market` until `G`, returning next-live seq. FeedHandler implements §3.6 both modes with states INIT→(SNAPSHOT)→LIVE→ENDED and logging.
- **Accept:** tests against stub Soup servers: (a) full-replay mode from seq 1; (b) snapshot mode — GLIMPSE stream then ITCH from returned seq `N`, message `N-1` never applied twice, `N` onward applied exactly once; (c) reconnect mid-live resumes seamlessly; (d) ENDED on `Z`.

### Phase 6 — Simulator & end-to-end
**T6.1 — Exchange simulator** *(M)* — after T2.3, T2.4. Replays a `.itch` file as: Soup ITCH server (login validation incl. reject paths, sequenced replay from any requested seq, heartbeats, `Z` at EOF, optional scripted disconnects) + GLIMPSE server (snapshot generated by running the fixture through `Market` up to a cut point, then serving state as `T`/`R`/`L`/`H`/`Y`/`A`/`F` + `G`). Speed control (as-fast-as-possible / paced / realtime-from-T-messages).
- **Accept:** self-test: client from seq 1 receives exact fixture message count; requested-seq login receives correct suffix; GLIMPSE snapshot at cut point equals direct replay state (differential test); scripted disconnect forces client resume.

**T6.2 — End-to-end integration tests** *(M)* — after T6.1, T5.2. Full stack vs. simulator on localhost: full-replay mode; GLIMPSE-sync mid-file; forced disconnect+resume; **final `Market` state identical across all paths** and equal to a direct in-process replay (the key invariant).
- **Accept:** `pytest tests/integration` green under 3.6 (docker).

### Phase 7 — CLI, display, performance
**T7.1 — CLI views** *(M)* — after T5.1d (usable with file replay before live parts exist; wire to T5.2 when ready). Subcommands, all working identically on `--live` / `--itch-file` / `--pcap` sources:
  - `static` — the **static data table**: fixed-width columns (SICC, ISIN, group, lot, tick table, limits, state, short-sell, ref price), `--csv` option; optional `--master JNX_ST_MASTER.csv` enrichment (Data File Formats §3).
  - `tail` — one decoded line per message (filter by type/book), the "see it moving" view.
  - `book SICC [--depth N]` — in-place ANSI-refresh top-N bid/ask + last trades (no curses).
  - `stats` — per-second message-type rates, session/seq, reconnect counters, book/order/orphan counts.
- **Accept:** golden-output tests on a small fixture for `static`/`tail`; `book`/`stats` demoed against the simulator (documented in README).

**T7.2 — Benchmark + hot-path tuning** *(S, stretch)* — after T6.2. Measure decode+book throughput replaying the 222k-message fixture on 3.6; document msgs/s; only if far below real feed rates, tune (Struct caching, fewer allocations). No C++ in prototype scope.
- **Accept:** `make bench` reproducible; results table in README.

### Dependency snapshot
`T1.1 → {T2.1, T2.4, T3.1, T4.0}`; `T2.1 → T2.2/T2.3`; `{T2.2,T2.4,T3.1} → T3.2`; `T2.4 → T3.3`; `{T4.0,T2.4} → T4.1`; `T2.2 → T5.1a, T5.1b → T5.1c → T5.1d`; `{T4.1,T5.1d} → T5.2`; `{T2.3,T2.4} → T6.1`; `{T6.1,T5.2} → T6.2`; `T5.1d → T7.1`. Wide parallelism in Phases 1–2 and between Phases 3 and 4.

## 5. Risks / notes
- **Python 3.6 drift is the #1 practical risk** with agent-written code. Mitigations: §0 embedded in every prompt, tests forced through the ubi8/python-36 container, no asyncio at all.
- **Order-number collisions across groups** (§3.3(3)): mitigated by counting/warning; resolve definitively with Japannext (Q1).
- **Price scaling**: 1 implied decimal and `0x7FFFFFFF` sentinel are JNX-specific (ASX was 2 decimals, signed, different sentinel) — do not copy ASX habits; unit vectors pin this.
- **The official UDP sample starts mid-session** (seq 12562, no R/L): auto-create semantics (§3.3(5)) and the asserted orphan-order counts in T5.1b turn this from a hazard into a test.
- **Full-replay bootstrap cost**: logging in at seq 1 late in a busy day replays the whole day over TCP — fine for a prototype, but confirm Japannext permits it and prefer GLIMPSE sync for late joins (Q4).
- **Service configuration is not in the public PDFs** (hosts, ports, credentials, per-group endpoints): everything network-facing takes these as parameters; CONNECTIVITY.md (T3.3) is the request list. Sample-derived ports (15001/15002) are simulator defaults only.
- Memory: full-day order store is a few million small tuples — fine for a prototype on RHEL 8; `stats` view reports live counts so we see it early.

## 6. Open questions for Japannext (ito@japannext.co.jp — tracked in CONNECTIVITY.md)
1. Are DAY/NGHT/DAYX/DAYU served as separate SoupBinTCP sessions/endpoints or one combined feed? Can order numbers collide across groups within one feed (E/D/U carry no group)?
2. Full service configuration: ITCH-TCP and GLIMPSE hosts/ports, credential/port assignments per user.
3. GLIMPSE Login Requested Sequence Number — confirm `1` (spec only mandates blank session).
4. Is full-session replay (ITCH-TCP login with requested seq 1) permitted at any time of day? Any rate limits?
5. SFTP access for daily files, especially the **ITCH Binary Data File** (full-day real data = ideal replay/regression input).

