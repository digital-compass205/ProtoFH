# Phase 3 Proposal — Production Deployment & Multi-Exchange Generalization

*Status: PROPOSAL for discussion — not yet a task-level plan. 2026-07-12.*
*Prerequisites: Phase 2 complete (see `PROJECT_WRAPUP.md`); `TARGET_VALIDATION.md` run on a real RHEL 8.10 box; Japannext UAT passed per `CONNECTIVITY.md`.*

## 1. Goals

1. **Production readiness**: run the Japannext stack unattended in a real trading environment — supervised processes, redundancy, day-rollover, operational tooling good enough that support staff (not the authors) can run it.
2. **Observability & support**: first-class monitoring (metrics, health, latency), alerting, and in-production capture/replay for incident analysis.
3. **Generalization**: refactor the architecture so the next exchange (or the next protocol: MoldUDP64, FIX, proprietary binary multicast) is a bounded plug-in effort — reusing the DB, wire format, publication, recovery, web client, and ops tooling unchanged.

Non-goals for Phase 3: order entry / trading connectivity; historical tick database; UI beyond operator needs; performance work beyond what production data rates demand (Phase 2 headroom is ~15×).

## 2. Workstream A — Production hardening

**A1. Process supervision & packaging.** systemd units for jnxdb/jnxfh/jnxweb (Restart=always with backoff, dedicated non-root user, resource limits); RPM or versioned tarball + install script fitting the locked-down RHEL host; config under `/etc/jnx/`, logs to journald + rotated files; `--version` build stamping (git sha) in every binary and every log header.

**A2. Redundancy.**
- *Feed-handler pairs*: primary/standby `jnxfh` (standby logged in to the exchange but not publishing, or cold with fast DB-recovery start — decide with measured failover budgets). Arbitration via a small lease in `jnxdb` (it is already the shared state holder) rather than a new coordination service.
- *A/B exchange lines* (when Japannext provides them): consume both, first-wins arbitration keyed on sequence number — this drops out naturally from the transport generalization in Workstream C.
- *DB/client fan-out*: multiple `jnxdb`/`jnxweb` instances are already supported by multicast; document and test the multi-host topology (TTL, IGMP, network requirements) with the network team.

**A3. Session lifecycle & time.** Day rollover automation (scheduled end-of-session shutdown on ITCH `C`/Soup `Z`, pre-open fresh start, stale-state refusal if the DB holds yesterday's session); NTP/PTP sanity checks at startup; explicit handling of Japannext's day/night market groups (two concurrent sessions — currently untested territory; needs a decision: one process per group vs multi-session in one process).

**A4. Fault-injection hardening.** Extend the F6 scenario suite: malformed exchange data (fuzz the decoder with a corpus built from mutated golden fixtures), slow/black-holed multicast, DB slow-consumer (write timeout path), disk-full logging, clock jumps, SIGSTOP pauses (heartbeat expiry on both sides). Every scenario gets a runbook entry.

## 3. Workstream B — Monitoring & support

**B1. Metrics endpoint.** Both C++ processes expose their existing stats as a plain-text HTTP `/metrics` in Prometheus exposition format (hand-rolled — it is a trivial text format, no library needed; reuse the query-listener plumbing). Core series: msgs/s by type, exchange seq lag (recv time vs exchange `T`-clock), pub_seq, db_connected, reconnect counts, orphan/collision counters, mcast send errors, RSS, per-stage latency histograms (decode→apply→db-write→mcast, coarse buckets, measured with the existing `mono_ns`). `jnxweb` gets the same for received-gap statistics.
- Alert catalogue (doc + sample rules): feed silent > N s during market hours, seq lag beyond threshold, db_connected=false, restart storms, gap rate on any client.

**B2. Latency truth.** Timestamp every UPDATE with FH receive time (field exists as `exch_ns`; add `recv_ns` in wire spec **v2** — see C3) so any consumer can measure end-to-end freshness; a `tools/latency_probe.py` that reports percentiles from the live multicast.

**B3. Production capture & replay.** An always-on capture (the Phase-1 `.itch` format, rotated hourly) written by `jnxfh` — this is both the incident-analysis record and future regression fixtures from *real* data. `run_e2e.py` learns to replay a captured hour against a candidate build and diff final state vs the production DB dump (the F2/F6 machinery reused for release validation).

**B4. Support tooling.** `jnxctl` — one operator CLI wrapping today's scattered tools (dbquery, mcast_spy, health, failover trigger, capture management); admin commands on the query port (log-level at runtime, connection status, forced resync); an on-call one-pager per alert linking into `RECOVERY.md`.

## 4. Workstream C — Generalization to multiple exchanges & protocols

The Phase 2 design already has the right seams; Phase 3 makes them explicit interfaces.

**C1. Layering target.**

```
[Transport]      SoupBinTCP | MoldUDP64+rewinder | FIX session | raw mcast | file/pcap replay
      ▼ ordered (seq, payload) stream + gap/recovery semantics per transport
[Decoder]        JNX ITCH | <exchange X> ITCH-dialect | FIX app messages | ...
      ▼ normalized book events: AddOrder/Exec/Delete/Replace/State/RefData/Trade
[Market core]    ONE shared engine (today's cpp/market) + per-exchange policy hooks
      ▼ ApplyResult
[Publication]    ONE record builder / wire format / jnxdb / multicast / jnxweb / tools
```

- *Transport interface*: `start / stop / on_payload(seq, bytes) / request_from(seq)`; each implementation owns its gap semantics (Soup = resume-on-reconnect; MoldUDP64 = A/B arbitration + re-request/rewinder; FIX = ResendRequest). Phase 1's Mold unwrapping code and pcaps give MoldUDP64 a ready-made test bed.
- *Normalized event model*: the key design decision. Today `Market::apply` takes JNX ITCH structs; define an exchange-neutral event set (superset fields, price scale + sentinel conventions carried in instrument metadata) and make the JNX decoder emit it. The F2 parity gate guarantees the refactor is behavior-neutral: the golden diff must stay empty.
- *Policy hooks* for the per-exchange quirks Phase 2 hard-coded (ref-price-in-`A`, passive-price trades, order-number scoping, absence semantics) — a small strategy interface, JNX as the first implementation.

**C2. FIX support (scoped).** For market data, FIX means a session layer (Logon/Heartbeat/TestRequest/ResendRequest/SequenceReset over TCP, tag=value parsing — hand-rolled, stdlib-only is feasible and keeps the zero-dependency rule) plus MarketDataSnapshot/IncrementalRefresh (35=W/X) decoding to the normalized events. Deliverable: transport + decoder + a FIX flavor of the simulator; book semantics reuse the shared core. FAST encoding, if a target exchange needs it, is a separate later work item.

**C3. Multi-exchange data model.** Wire spec **v2**: add `exchange` id + `recv_ns` to the envelope, version-bump with dual-decode support in clients during migration (the version byte and the frozen-vector test rig make this safe). `jnxdb` tables keyed by (exchange, ticker, group); `jnxweb` grows an exchange selector; instrument metadata (price scale, lot, tick tables) becomes per-exchange configuration rather than JNX constants.

**C4. Proof-of-generality milestone.** The workstream is DONE when a second feed runs through the shared stack with *only* transport+decoder+policy code being new. Candidates, in order of increasing effort: (1) JNX **MoldUDP64** service — same messages, new transport, official pcap already in hand, exercises A/B arbitration = the redundancy feature A2 needs anyway; (2) another Nasdaq-style ITCH exchange; (3) a FIX-based feed. Recommendation: **do (1) as the Phase-3 milestone**, keep (2)/(3) as fast-follow.

## 5. Suggested sequencing & sizing

| Stage | Content | Depends on |
|---|---|---|
| P3.0 | Target-box validation + UAT (carry-over) | exchange access |
| P3.1 | A1 packaging/systemd + B1 metrics + B4 jnxctl skeleton | — |
| P3.2 | B2 latency + B3 capture/replay + A4 fault-injection round | P3.1 |
| P3.3 | C1 layering refactor behind the parity gate (no behavior change) | P3.1 |
| P3.4 | MoldUDP64 transport + A/B arbitration (= C4 milestone + A2 line redundancy) | P3.3 |
| P3.5 | A2 FH failover pair + A3 rollover automation | P3.2, P3.4 |
| P3.6 | C2 FIX transport/decoder + C3 wire v2 multi-exchange model | P3.3 |

Rough effort in Phase-2 currency: P3.1–P3.2 ≈ one Phase-2 phase each; P3.3–P3.4 ≈ the F2+F5 pair; P3.6 is the largest single item (FIX session testing discipline). The same agent-worker method applies — each stage gets plan-grade prompts, mechanical gates (parity diff, scenario invariants, golden vectors extended per protocol), and real-data capture from B3 as new regression fixtures.

## 6. Open questions for the user / exchange

1. Failover budget: acceptable data-unavailability window on FH failure? (Drives hot-standby vs cold-restart in A2.)
2. Day/night groups: do we need both concurrently in production, and one process or two?
3. Monitoring stack on site: is Prometheus/Grafana available in the locked environment, or do we alert from logs alone?
4. Which second exchange/protocol is actually next commercially? (Reorders C4's candidates.)
5. Does the "no third-party libraries" rule hold for Phase 3 production too (it shaped B1's hand-rolled exporter and C2's hand-rolled FIX)?
6. Multicast in production: routed (needs IGMP/PIM coordination) or single-segment?
