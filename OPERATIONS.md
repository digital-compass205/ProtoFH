# OPERATIONS.md — running jnx-fh2 (jnxdb / jnxfh / jnxweb)

This is the day-to-day operator guide: how to start the three
processes, what the config files look like, how to read the logs, and
where to go when something breaks. For the restart/recovery decision
tree itself (what happens automatically when a component dies) see
**RECOVERY.md** — this document assumes that behavior and doesn't
repeat it.

## 1. Components and start order

Three independent processes (JNX_PLAN2.md §1):

| process  | role                                              | language |
|----------|---------------------------------------------------|----------|
| `jnxdb`  | in-memory database (5 tables), no persistence      | C++11    |
| `jnxfh`  | feed handler: exchange -> decode -> apply -> publish | C++11  |
| `jnxweb` | web GUI client, subscribes to multicast            | Python 3.6 |

**Start order: any.** This is a deliberate design property, not a
convenience — every component tolerates the others being absent at
startup and catches up automatically:

- `jnxfh` before `jnxdb`: `jnxfh` starts anyway (unless
  `require_db=1`), stays live on the exchange, and keeps retrying the
  DB connection every 1 s (RECOVERY.md §2). Multicast publication is
  unaffected.
- `jnxdb` before `jnxfh`: `jnxdb` just sits idle waiting for a UDS
  connection on its ingest socket.
- `jnxweb` any time, before or after the others: it self-heals from
  missed/late multicast datagrams because every UPDATE record carries
  full ticker state (JNX_PLAN2.md §1, "jnxweb is loss-tolerant by
  design").

A practical start order for a fresh environment (not required, just
tidy — matches how the dry run below is written):

```sh
cpp/build/jnxdb  --config=etc/jnxdb.cfg &
cpp/build/jnxfh  --config=etc/jnxfh.cfg &
python3 -m jnxweb --mcast 239.192.1.1:26400 --http-port 8080 --mcast-if <local-if-ip> &
```

## 2. Config files

`jnxdb` and `jnxfh` (both C++) take an optional `--config=PATH` file of
`key=value` lines (`#` comments, blank lines ignored — `cpp/common/cfg.h`),
with any key overridable on the command line as `--key=value` (file
loaded first, then argv overrides — so `--config=etc/jnxfh.cfg
--bootstrap=glimpse` works as expected). Samples:

- **`etc/jnxdb.cfg`** — `sock=` (UDS ingest path), `query_port=` (TCP
  operator/recovery port, binds 127.0.0.1 only).
- **`etc/jnxfh.cfg`** — exchange host/port/credentials, GLIMPSE
  host/port, `db_sock=` (must match jnxdb's `sock=`), `require_db=`,
  `bootstrap=` (`replay`|`glimpse`), multicast group/port/ttl/interface.
  Full key list is documented inline in the file and in the header
  comment of `cpp/fh/jnxfh_main.cpp`.

`jnxweb` (Python) has no config file — it's flag-only (stdlib
`argparse`, no file format to keep 3.6-safe and dependency-free):

```sh
python3 -m jnxweb \
    --mcast 239.192.1.1:26400 \
    --mcast-if 127.0.0.1 \
    --http-port 8080 \
    --http-host 0.0.0.0
```

Key flags: `--mcast GROUP:PORT` (default `239.192.1.1:26400`),
`--mcast-if ADDR` (**must** be set explicitly for same-host testing —
see the dry-run section below; wrong/absent interface is the #1 cause
of "jnxweb shows nothing"), `--http-port`/`--http-host` (GUI listen
address), `--test-feed UDS_PATH` (dev/test only: injects records over a
UNIX datagram socket instead of real multicast — see
`jnxweb/mcast.py`).

## 3. Monitoring cheatsheet

### 3.1 The 5 s stats log lines (stderr, both C++ processes)

`jnxdb`:
```
stats: session=SIM0000001 epoch=1783778102262296597 last_exch_seq=2000
       updates=2000 dups=0 books=35 orders=189 ticks=0
       fh_connected=1 query_clients=0 rss_kb=4512
```
| field | meaning |
|---|---|
| `session` | current exchange session id (from `jnxfh`'s meta) |
| `epoch` | `jnxfh`'s process epoch (changes on cold bootstrap, stable across DB-only restarts) |
| `last_exch_seq` | last applied exchange sequence number — **the single best "is data still flowing" signal**; should keep climbing during trading |
| `updates` | UPDATE records applied since start (live applies + sync-dump rows) |
| `dups` | duplicate/stale UPDATEs rejected (`exch_seq <= last_exch_seq` at same epoch) — **should stay 0**; nonzero means something replayed data jnxdb already had (safety net firing, not silent data loss) |
| `books` / `orders` / `ticks` | live row counts in T4/T3/tick tables |
| `fh_connected` | 1 if jnxfh's UDS ingest connection is currently up |
| `query_clients` | number of open TCP query connections (dbquery, jnxweb-adjacent tooling, etc.) |
| `rss_kb` | jnxdb's own resident set size — the soak-test signal (see §5 below and `tools/run_e2e.py --scenario soak`) |

`jnxfh`:
```
stats: msgs/s=433 exch_seq=2000 pub_seq=1934 books=25 orders=315
       db_connected=1 mcast_errors=0 rss_kb=4556
```
| field | meaning |
|---|---|
| `msgs/s` | exchange messages applied in the last 5 s window / 5 — the live throughput number |
| `exch_seq` | last applied exchange sequence (matches jnxdb's `last_exch_seq` once the write lands) |
| `pub_seq` | local publish counter — **resets to 1 on every jnxfh restart**, even a same-epoch recovery restart; don't treat it as monotonic across restarts (RECOVERY.md §1 explains why) |
| `books` / `orders` | jnxfh's own in-memory market state sizes |
| `db_connected` | 0 while in the DB-reconnect retry loop (RECOVERY.md §2) — jnxfh keeps running and multicasting regardless |
| `mcast_errors` | cumulative multicast `sendto()` failures (should stay 0 in a healthy network) |
| `rss_kb` | jnxfh's own RSS — soak-test signal |

### 3.2 `dbquery.py STATS` fields

`python3 tools/dbquery.py STATS` returns the same `Meta` fields as the
log line plus a couple more, one `key=value` per line:

```
session=... epoch=... last_exch_seq=... updates_applied=... dups_dropped=...
orders_applied=... ticks_applied=... syncs_completed=... syncs_discarded=...
orders_live=... books=... ticks=... rss_kb=...
```

`syncs_completed` / `syncs_discarded` matter specifically for the
`jnxdb`-restart recovery path (RECOVERY.md §2/§5): a completed sync
bracket means jnxfh's post-restart RESET+SYNC push landed cleanly;
`syncs_discarded > 0` means jnxdb itself died again *mid*-sync and
wiped a partial dump — investigate immediately, it means two failures
overlapped.

Other useful `dbquery.py` commands: `PING` (liveness), `GET <ticker>`
(everything about one ticker), `BOOK <ticker>`, `ORDERS <ticker>`,
`TRADES <ticker>`, `TABLE static|state|trades` (full-table CSV dump —
what `run_e2e.py`'s scenario comparisons use).

### 3.3 `mcast_spy.py` — watching the wire

```sh
# Live tail of decoded UPDATE records:
python3 tools/mcast_spy.py --group 239.192.1.1 --port 26400 --iface 127.0.0.1

# Summary only (good for scripted health checks):
python3 tools/mcast_spy.py --group 239.192.1.1 --port 26400 --iface 127.0.0.1 \
    --stats --until-idle 5
# -> updates=N gaps=G bad=B epochs=E first_pub_seq=... last_pub_seq=...
```
`gaps` counts **forward** jumps in `pub_seq` within one epoch (missed
datagrams); it's the client-side loss counter the design intentionally
relies on instead of a recovery protocol (JNX_PLAN2.md §1). `--iface`
must match the interface `jnxfh` is sending from (`mcast_if` in
`jnxfh.cfg`) or nothing arrives — the same gotcha as jnxweb's
`--mcast-if`.

## 4. Failure playbook

Every restart-matrix scenario (jnxfh crash, jnxdb crash, exchange TCP
drop, GLIMPSE-cold start, both crashing together) — what happens
automatically, what to check, and what NOT to do — is documented with
real log evidence in **RECOVERY.md**. Read that first when anything
dies; this file does not repeat it. Quick index of RECOVERY.md's
sections:

1. `jnxfh` crashes, `jnxdb` stays up — automatic resume, zero replay.
2. `jnxdb` crashes, `jnxfh` stays up — automatic RESET+SYNC on DB return.
3. Exchange TCP drops — automatic SoupBinTCP reconnect/resume.
4. GLIMPSE-cold bootstrap specifics.
5. Both crash together — cold bootstrap from the exchange.

## 5. Soak / leak check

`tools/run_e2e.py --scenario soak --minutes M` runs jnxdb+jnxfh
continuously against a looped copy of a fixture (concatenated ahead of
time so it's one uninterrupted SoupBinTCP session — no restarts needed
mid-run), polling both processes' RSS via `/proc/<pid>/status` every
15 s (`--poll-interval`). It PASSes if, after discarding the first 20%
of samples as warmup, RSS growth from the first post-warmup sample to
the last is under 5% for **both** processes. Because the same order
numbers recur every loop of the fixture, this also exercises the
order-number-collision path (JNX_PLAN.md §3.3(3): replace + WARN +
counter) under sustained load — collisions there are expected, not a
bug.

```sh
python3 tools/run_e2e.py --scenario soak --minutes 30      # plan floor
python3 tools/run_e2e.py --scenario soak --minutes 10      # quick check
```

Prints a per-sample RSS curve and a PASS/FAIL line; `--out DIR` keeps
the results (including `soak/rss_curve.csv`) instead of a throwaway
tmpdir.

## 6. Dry run (simulator-based)

Everything below runs entirely offline against the Phase-1 Python
simulator (`jnxfeed.sim`) and the committed fixtures — no exchange
access needed. Four terminals:

```sh
# Terminal 1 — exchange simulator (serves both ITCH :15001 and GLIMPSE :15002)
python3 -m jnxfeed.sim --itch-file tests/fixtures/sample_udp_head.itch \
    --itch-port 15001 --glimpse-port 15002 --speed realtime

# Terminal 2 — jnxdb
mkdir -p /tmp/jnx-dryrun
cpp/build/jnxdb --sock=/tmp/jnx-dryrun/db.sock --query_port=26401

# Terminal 3 — jnxfh (bootstrap=replay logs in at seq 1, full session replay)
cpp/build/jnxfh --itch_host=127.0.0.1 --itch_port=15001 \
    --glimpse_host=127.0.0.1 --glimpse_port=15002 \
    --db_sock=/tmp/jnx-dryrun/db.sock \
    --bootstrap=replay \
    --mcast_group=239.192.1.1 --mcast_port=26400 --mcast_if=127.0.0.1

# Terminal 4 — jnxweb GUI (browse to http://127.0.0.1:8080)
python3 -m jnxweb --mcast 239.192.1.1:26400 --mcast-if 127.0.0.1 \
    --http-port 8080
```

**`--mcast_if=127.0.0.1` / `--mcast-if 127.0.0.1` on both jnxfh and
jnxweb is required for same-host loopback testing** — this is also why
`IP_MULTICAST_LOOP=1` is set on the sender (JNX_PLAN2.md §1); without a
matching interface the datagrams never reach a same-host receiver.

Expected numbers after the simulator reaches end-of-session (the head
fixture is 2,000 messages, 66 of them `T` timestamp-only messages that
update the clock but publish nothing — see
`tests/integration/test_jnxfh.py` for the authoritative count,
`FIXTURE_PUBLISHED = 2000 - 66 = 1934`):

```sh
python3 tools/dbquery.py --port 26401 STATS
# last_exch_seq=2000  updates_applied=1934  dups_dropped=0
```
jnxfh's own exit line matches: `end of session (Z): published=1934
last_seq=2000 pub_seq=1934` (verbatim example in RECOVERY.md §1).
Cross-check against a pure C++ decode+apply pass over the same file —
must match exactly (this is also `tests/integration`'s bit-identical
gate):
```sh
cpp/build/book_dump tests/fixtures/sample_udp_head.itch /tmp/jnx-dryrun/dump
python3 tools/compare_db_dump.py --port 26401 /tmp/jnx-dryrun/dump
# -> "orders OK", "books OK", "trades OK", "state OK" (one per section);
#    exit code 0 means every section matched
```
Or drive the whole thing through the orchestrator in one shot (this is
what `run_e2e.py --scenario baseline` does under the hood, plus dump
collection):
```sh
python3 tools/run_e2e.py --scenario baseline \
    --fixture tests/fixtures/sample_udp_head.itch
# -> PASS, dump dir printed
```
