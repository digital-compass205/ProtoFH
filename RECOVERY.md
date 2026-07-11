# RECOVERY.md — jnxfh/jnxdb restart & recovery runbook

This is the operator runbook for the §1 restart matrix (JNX_PLAN2.md),
proven end-to-end by `tools/run_e2e.py` + `tests/integration/test_recovery.py`
(F6). Every log line quoted below is real output from those runs, not
paraphrased.

Two facts underpin everything in this document:

- **DB write happens FIRST, multicast SECOND**, per exchange message
  (`cpp/fh/jnxfh_main.cpp`'s `on_message` handler). `jnxdb` is the
  authoritative record of what has been applied; multicast is a
  best-effort broadcast of the same data. If `jnxfh` dies between the two,
  that one multicast datagram is simply never sent — acceptable, lossy-by-design
  (clients self-heal on the next update to that ticker; do not chase this
  as a bug).
- **`jnxdb` has no persistence.** It is a pure in-memory mirror. Killing
  it (however cleanly or uncleanly) always means it comes back **empty**.
  Recovery after a DB restart is always a fresh SYNC dump pushed by
  `jnxfh` from its own live memory, never a replay from exchange.

---

## 1. `jnxfh` crashes (SIGKILL or any unclean death), `jnxdb` stays up

**What happens automatically:** on restart, `jnxfh` connects to `jnxdb`,
sends `HELLO`, and if the DB reports non-zero state (`GET_STATE`) it
downloads the full order store + tick tables + all static/state/trade
rows, rebuilds its in-memory `Market`, and logs back into the exchange
**with the same Soup session** requesting `last_seq + 1`. This is a pure
SoupBinTCP resume — the exchange line does not replay anything jnxdb
already has recorded. **Zero exchange replay, nothing lost.**

**Real evidence** (from a `kill_fh` run of `tools/run_e2e.py`, SIGKILLed
at exch_seq 800):

```
# restarted jnxfh's log:
recover: recovered from db: 189 orders, 0 tick rows, 35 book rows; session='SIM0000001' last_seq=800 epoch=1783778102262296597
jnxfh: resume mode: session='SIM0000001' seq=801
soup: connected to 127.0.0.1:47129, logging in (session='SIM0000001' seq=801)
jnxfh: logged in: session='SIM0000001' next seq 801
jnxfh: end of session (Z): published=1161 last_seq=2000 pub_seq=1161
```

`jnxdb`'s `STATS` after the full run showed `dups_dropped=0` — the resume
point (`801`) landed exactly one past the DB's last recorded exchange
sequence (`800`), so no message was ever double-applied.

**Operator action:** none required — this is fully automatic. Do **not**
manually clear `jnxdb` state or pass `--bootstrap` overrides when
restarting `jnxfh` after its own crash; as long as `jnxdb` is alive and
holds state, the recovery decision tree ignores `--bootstrap` entirely
and takes the resume path.

**Verify health:**
```
python3 tools/dbquery.py STATS
# dups_dropped=0                     -- no double-applied messages
# last_exch_seq should keep climbing -- live flow resumed
python3 tools/mcast_spy.py --stats --until-idle 3
# gaps=0                             -- no FORWARD gaps in pub_seq. Note
#                                        pub_seq is a per-PROCESS local
#                                        counter that always restarts at
#                                        1 on every jnxfh start, whether
#                                        or not the epoch changed — a
#                                        GET_STATE-recovered restart (this
#                                        scenario) keeps the SAME epoch
#                                        (recovered from jnxdb's meta) but
#                                        still resets pub_seq, so you will
#                                        legitimately see pub_seq drop
#                                        back to 1 mid-epoch in the raw
#                                        multicast stream. mcast_spy's gap
#                                        counter only flags FORWARD jumps
#                                        (missed datagrams), so this is
#                                        not a false negative for loss —
#                                        just don't build tooling that
#                                        assumes pub_seq is monotonic
#                                        across a jnxfh restart; use
#                                        (epoch, exch_seq) for that.
```

---

## 2. `jnxdb` crashes (SIGKILL or any unclean death), `jnxfh` stays up

**What happens automatically:** `jnxfh`'s next UDS write fails; it marks
`db_connected=false` and **keeps running** — it stays logged into the
exchange, keeps applying every message to its own `Market`, and keeps
multicasting every UPDATE. A background 1-second reactor timer (never a
thread — everything in `jnxfh` is single-threaded) retries the DB
connection. When `jnxdb` comes back (empty, since it has no
persistence), the `HELLO` handshake reports epoch/last_seq `0/0`, which
never matches `jnxfh`'s live position, so `jnxfh` pushes a full **RESET +
SYNC dump** from its own memory, then resumes normal per-message writes.

**Real evidence** (from a `kill_db` run, SIGKILLed at exch_seq ~800,
restarted 2s later):

```
publish: db write failed (Broken pipe); marking db disconnected
jnxfh: stats: msgs/s=285 exch_seq=1425 pub_seq=1378 books=25 orders=315 db_connected=0 mcast_errors=0
jnxfh: db connected (db epoch=0 last_seq=0)
jnxfh: pushed RESET + full sync to db (23317 bytes, last_seq=1425)
jnxfh: db link restored
jnxfh: end of session (Z): published=1934 last_seq=2000 pub_seq=1934
```

Note `exch_seq` kept climbing (`1425` in the mid-outage stats line) and
`published=1934` at the end matches the uninterrupted baseline exactly
— **the outage was invisible to multicast subscribers**, aside from a
window where `jnxdb` had no data to answer queries with.

**Operator action:** just restart `jnxdb` (any config, any order — no
special flags). While it is down, `jnxdb` query clients (dbquery,
compare tools, the future `jnxweb`) will fail to connect; that is
expected and resolves itself the moment `jnxdb` is back up and `jnxfh`'s
1s retry timer notices.

**Do NOT** restart `jnxfh` in this scenario — it is not the thing that
died, and doing so throws away its live in-memory state (forcing a
`--bootstrap` cold start against the exchange instead of a cheap local
resync).

**Verify health:**
```
python3 tools/dbquery.py STATS
# syncs_completed >= 1     -- a RESET+SYNC bracket was accepted since restart
# syncs_discarded == 0     -- no partial (interrupted-mid-bracket) sync was
#                              wiped; if this is ever nonzero, jnxdb was
#                              itself killed again WHILE receiving a sync
#                              (see §5, partial-sync wipe)
python3 tools/mcast_spy.py --stats --until-idle 3
# gaps=0, updates == (fixture msgs - T msgs)  -- multicast never stopped
```

---

## 3. Exchange (ITCH) TCP connection drops mid-session

**What happens automatically:** `jnxfh`'s Soup client detects the
disconnect (`peer closed`, or a read/write error, or 15s of silence —
`peer_silent`), logs a `WARN`, and reconnects with **1s→10s capped
exponential backoff**, re-logging in with the **same session id** and
`requested_sequence = last_applied_seq + 1`. `jnxdb` sees ordinary
per-message UPDATEs continue as if nothing happened; nothing about this
path touches `jnxdb`'s recovery protocol at all.

**Real evidence** (from a `drop_exchange` run, scripted disconnect after
800 packets):

```
soup: connected to 127.0.0.1:44117, logging in (session='' seq=1)
jnxfh: logged in: session='SIM0000001' next seq 1
soup: connection lost (peer closed); retrying in 1000 ms
soup: connected to 127.0.0.1:44117, logging in (session='SIM0000001' seq=801)
jnxfh: logged in: session='SIM0000001' next seq 801
jnxfh: end of session (Z): published=1934 last_seq=2000 pub_seq=1934
```

`jnxdb` STATS after the run: `dups_dropped=0`, and the multicast spy's
`pub_seq` was contiguous end to end — the resume produced no duplicate
publishes.

**Operator action:** none required. If the backoff cap (10s) is being
hit repeatedly, that indicates a real network/exchange-side problem
worth escalating — `jnxfh`'s own retry loop will not give up on its own
(by design: no retry storm, but also no abandonment on a transient
network blip).

**Verify health:**
```
python3 tools/dbquery.py STATS
# dups_dropped == 0
python3 tools/mcast_spy.py --stats --until-idle 3
# gaps == 0
```

---

## 4. Cold start with a GLIMPSE bootstrap (`--bootstrap=glimpse`)

**What happens automatically:** with no DB state (`jnxdb` empty/never
run) and `--bootstrap=glimpse`, `jnxfh` logs into the GLIMPSE port with a
blank requested session, applies the snapshot (tick tables, R/H/Y/ref-price
rows, one `A` per live order) to a fresh `Market`, then logs into the
live ITCH port at `next_live_seq` (the snapshot's cut point + 1) and
streams normally from there. It also immediately pushes a RESET + full
SYNC dump of the snapshot to `jnxdb`.

**Known, proven-exact divergence:** the GLIMPSE snapshot carries **no
trade history** — only currently-resting orders and current state. So:

- `orders`, `books`, `static` tables converge to **exactly** the same
  final content as an uninterrupted from-message-1 run (proven against
  `cpp/build/book_dump` on the whole fixture).
- `trades` (`cum_qty`, `cum_turnover`, `trade_count`, `last_price`,
  `last_qty`, `last_match_number`) reflects **only post-snapshot trades**
  — i.e. trades that happened strictly after the cut point, INCLUDING
  trades against resting orders the snapshot restored. `tests/
  integration/test_recovery.py::test_glimpse_cold_bootstrap` proves this
  is exactly right (not just "plausible") by replaying the full fixture
  once through the Python prototype `Market`, snapshotting each ticker's
  cumulative trade stats at the cut boundary, and diffing against the
  final totals — that computed delta is required to match `jnxdb`'s
  `trades` table exactly.

**Operator action:** this is a deliberate, informed choice for cold
starts where the operator accepts "book/order state resumes instantly,
intraday trade history before the snapshot point is gone." If full trade
history matters, prefer `--bootstrap=replay` (§5) when the exchange
tolerates a from-seq-1 relogin, or accept that trade history before any
cold start is simply unavailable from `jnxdb` going forward (it was
never persisted anywhere — `jnxdb` has no disk).

**Verify health:**
```
python3 tools/compare_db_dump.py --port <query_port> <book_dump of the fixture>
# orders   OK
# books    OK
# static   OK
# trades   MISMATCH  <- EXPECTED here; inspect the printed per-ticker
#                        deltas and confirm they are all "fewer trades in
#                        db than in the full-day dump", never "more" or
#                        "different tickers than expected"
```

---

## 5. Both `jnxfh` AND `jnxdb` crash together

**What happens automatically:** restart `jnxdb` first (comes up empty),
then restart `jnxfh`. `jnxfh`'s `HELLO` sees epoch/last_seq `0/0` — no
recoverable state — so it falls through to its `--bootstrap` mode
against the **still-running exchange**. This simulator (and, per the
Soup spec, most real exchanges within their retention window) tolerates
a brand-new connection re-requesting sequence 1, so `--bootstrap=replay`
reproduces the **exact same final state as an uninterrupted run,
including full trade history** — strictly better than the GLIMPSE path
for this scenario, because nothing here depends on a snapshot's
inherent trade-history gap.

**Real evidence** (from a `kill_both` run, both SIGKILLed at exch_seq
~800, DB restarted, then `jnxfh` restarted with `--bootstrap=replay`):
final `jnxdb` state (`static`, `state`, `trades`, `orders`, `STATS`
excluding restart-bookkeeping counters) was byte-for-byte identical to
the uninterrupted baseline across 3 repeated trials.

**Operator action:** restart `jnxdb` before `jnxfh` (if `jnxfh` comes up
first with `--require_db=0`, it will just retry the DB link in the
background per §2 — order isn't strictly required, but DB-first avoids
an extra resync cycle). Pick `--bootstrap=replay` if the exchange/line
retention allows a from-seq-1 relogin for the current session; if not
(a real exchange may reject an out-of-window replay request), fall back
to `--bootstrap=glimpse` and accept the §4 trade-history caveat.

**Verify health:**
```
python3 tools/dbquery.py STATS
# last_exch_seq should reach the exchange's current position
python3 tools/compare_db_dump.py --port <query_port> <book_dump of the fixture>
# ALL sections OK (unlike §4, trades should match too under replay)
```

---

## 6. Boundary conditions worth knowing about

- **`jnxfh` killed between its DB write and its multicast send:** one
  multicast datagram is lost. This is fine by design — `jnxdb` is
  authoritative, and mcast clients self-heal on the ticker's next
  update. Do **not** treat `mcast updates == db updates_applied` as a
  correctness requirement during/around a kill; only treat sustained
  gaps (`mcast_spy --stats` `gaps > 0` after things settle) as a
  problem.
- **Stale DB from a previous exchange session:** `jnxfh`'s HELLO
  handshake does a strict epoch/last_seq match; any mismatch (including
  "DB belongs to an old session") triggers RESET + full SYNC from
  `jnxfh`'s live memory, never a silent stale-data acceptance.
- **`jnxdb` killed mid-SYNC (between `SYNC_BEGIN` and `SYNC_END`):** the
  bracket is discarded wholesale on reconnect/restart (`syncs_discarded`
  counter in `STATS`) — a partial sync never leaves half-applied rows
  behind. This is exercised live by the `kill_db` scenario's timing
  window and covered directly by `cpp/test/test_tables.cpp`.
- **A ticker whose only-ever messages are now-fully-closed orders, with
  no directory/state message ever seen for it, AND no live order left
  by the time a resync happens:** its group cannot be reconstructed from
  `jnxfh`'s in-memory `Market` (this is an inherent limitation shared
  with the Python prototype's `refdata` model, not new to F6 — see the
  bug/fix note below). `jnxfh`'s SYNC dump now correctly **skips** such a
  row rather than emitting a bogus one; if the ticker never trades again,
  its previously-published row is not present after that particular
  resync. This is narrow (idle order-only, directory-less tickers,
  observed only during `kill_db`/`kill_both`-with-resync scenarios) and
  documented here rather than "fixed" further, since fixing it fully
  would require persisting more per-ticker metadata than the Market core
  currently does (a bigger, riskier change than F6's scope).

---

## 7. Bug found and fixed during F6 testing

While proving scenario **b** (`kill_db`) reaches byte-identical final
state to an uninterrupted baseline, `tools/run_e2e.py`'s stricter
raw-row comparison (sorted full CSV rows, not the ticker-collapsing
comparison `tools/compare_db_dump.py` does) caught a real bug:

**Symptom:** after a DB restart + resync, some tickers ended up with
**two** rows in `jnxdb`'s tables — one correctly keyed `(ticker, "DAY")`
(created by a live UPDATE after the resync) and a bogus duplicate keyed
`(ticker, "")` (empty group), created by the resync's own SYNC dump.

**Root cause:** `build_sync_dump` (`cpp/fh/publish.cpp`) resolves each
ticker's `group` for its dump-time `'#'` UPDATE row from `refdata`, or —
if `refdata` doesn't know it (an order-only ticker never issued a
directory record) — by scanning currently-**live** orders for a match.
If every order for that ticker had already closed by resync time, the
scan found nothing and the code fell through, emitting the row with an
**empty** group anyway. A later live message for the same ticker (which
always carries its own correct group) then created a second, correctly
keyed row — the two coexisted, and `jnxdb`'s key is `(ticker, group)`,
so they never merged.

**Fix (`cpp/fh/publish.cpp`, `build_sync_dump`):** when a ticker's group
is genuinely unresolvable (no refdata group, no matching live order),
**skip** emitting that ticker's `'#'` sync row instead of emitting one
with an empty group. A subsequent live touch (if any) then creates the
single, correctly-grouped row from scratch; if the ticker never trades
again, it is simply absent from the post-resync DB (see §6's boundary
note) rather than present-twice-and-wrong.

As a secondary improvement, the same function now also re-emits the
`(ticker="", group=<g>)` system-wide pseudo-rows a live `'S'` message
produces, for every group `jnxfh`'s `PubContext` has ever logged an
event for — these were previously silently dropped by every resync
(unreachable via `refdata`/order-book iteration), which is now fixed for
the DB-restart-resync paths. (This particular row class is orthogonal to
the exchange's own event history and cannot be recovered by a GLIMPSE
cold bootstrap at all, since GLIMPSE snapshots never carry system events
— that path's rows are excluded from the F6 comparator via the same
precedent `tools/compare_db_dump.py` already sets: system-wide,
empty-ticker rows are out of scope for canonical per-instrument state.)

Both fixes are confined to `build_sync_dump`'s F5/F6-only resync
mechanism — nothing in the frozen wire format (F3), the market core
(F2, parity-critical with the Python prototype), or the DB ingest
protocol (F4) changed. `make -C cpp test` and the full pytest suite stay
green.
