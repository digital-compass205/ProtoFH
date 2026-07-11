# jnxfeed — Japannext PTS ITCH feed handler (prototype)

A prototype market data feed handler for the Japannext PTS equities ITCH
feed over SoupBinTCP, built to evaluate AI-agent-assisted development.
The full design, protocol cheat sheet, and task breakdown live in
`/workspace/JNX_PLAN.md`; official specs and sample captures are in
`/workspace/jnx-specs/`.

## Target platform

- **Python 3.6.4** (RHEL 8.10), **stdlib-only at runtime**.
- Development may happen on newer interpreters, but code must stay within
  the 3.6 subset defined in JNX_PLAN.md §0. A static guard
  (`tests/unit/test_py36_compat.py`) rejects the most common 3.7+ idioms;
  the authoritative check is running the suite on real 3.6 (below).

## Running the tests

Two paths:

```sh
# Fast local run — uses python3.6 if installed, else your python3:
make test

# Authoritative run — Python 3.6 inside the RHEL 8 (UBI8) container,
# matching the deployment target (requires docker):
make test-docker
```

`pytest==7.0.1` is pinned in `Dockerfile.dev` — it is the last pytest
release that supports Python 3.6.

## Layout

See JNX_PLAN.md §2. Summary: `jnxfeed/itch` (message codec), `jnxfeed/soup`
(SoupBinTCP), `jnxfeed/net` (selectors reactor), `jnxfeed/book` (refdata,
order books, trade tape), `jnxfeed/sim` (exchange simulator),
`jnxfeed/cli` (probe/capture/replay/views), `tests/` (unit, integration,
fixtures extracted from the official sample captures).

## Usage

`python -m jnxfeed <subcommand> --help` for full options.

**Connectivity kit** (T3.3 — point at UAT/prod the day access exists):

```sh
python -m jnxfeed probe   --host H --port P --user U --pass PW [--glimpse]
python -m jnxfeed capture --host H --port P --user U --pass PW --out day.itch --seq 1
```

**Views** (T7.1) — each works identically on three sources:
`--itch-file F` (replay a `.itch` file), `--pcap F` (replay the official
UDP sample pcap), or live `--host/--port/--user/--pass`
(+ `--glimpse-host/--glimpse-port` for snapshot bootstrap, `--seq N`):

```sh
# Static data table (add --csv for CSV output):
python -m jnxfeed static --itch-file tests/fixtures/sample_udp_head.itch

# One decoded line per message, with filters:
python -m jnxfeed tail --itch-file tests/fixtures/sample_udp_head.itch \
    --types A,E --book 1570 --limit 20

# Final book state from a file; per-second stats from the official pcap:
python -m jnxfeed book 1570 --itch-file tests/fixtures/sample_udp_head.itch
python -m jnxfeed stats --pcap /workspace/jnx-specs/Japannext_PTS_ITCH_Equities_v1.7.UDP.pcap
```

**Live demo against the simulator** — terminal 1 replays the committed
real-data slice at recorded-timestamp speed, terminal 2 watches book
`1570` (the busiest SICC in the slice) with in-place ANSI refresh:

```sh
# terminal 1
python -m jnxfeed.sim --itch-file tests/fixtures/sample_udp_head.itch --speed realtime

# terminal 2 (any view; book/stats refresh every --interval seconds)
python -m jnxfeed book 1570 --host 127.0.0.1 --port 15001 --user TEST --pass SECRET
python -m jnxfeed stats     --host 127.0.0.1 --port 15001 --user TEST --pass SECRET
```

The simulator also serves GLIMPSE on `:15002`; add
`--glimpse-host 127.0.0.1 --glimpse-port 15002` to any live view to
bootstrap from a snapshot instead of full replay.

## Benchmark

`make bench` replays the full official UDP sample (222,189 messages,
pre-loaded in memory; 3 repetitions, best shown). Numbers on the
development box — **CPython 3.14, NOT the 3.6.4 deployment target**; the
authoritative figures need the same script run under the UBI8 Python 3.6
container (as with `make test-docker`):

| stage                 | msgs/s (dev box, py3.14) |
|-----------------------|--------------------------|
| decode only           | ~1,030,000               |
| decode + Market.apply |   ~379,000               |

Both are far above real feed rates (and the plan's 50k msgs/s tuning
threshold), so no hot-path tuning was done (T7.2).

### C++ system (jnxfh/jnxdb) — F8 benchmark

`make -C cpp bench` (`tools/bench.py`) replays the same full official
sample (222,189 messages) through the C++ binaries, 3 repetitions, best
shown. **Dev-container numbers** (Ubuntu, gcc 15, `-O2`) — not yet run on
the RHEL 8.10 target (§8 of JNX_PLAN2.md):

| stage                                          | msgs/s (dev box) |
|-------------------------------------------------|------------------|
| decode only (`itch_replay`)                      | ~995,000         |
| decode + `Market.apply` (`book_dump`)            | ~655,000         |
| full pipeline: sim(max) → `jnxfh` → `jnxdb`+mcast| ~36,000–40,000   |

The full-pipeline number is measured honestly end-to-end (jnxfh's own
"starting live loop" → "end of session" log timestamps, published-update
count / elapsed seconds) and is **below the plan's 500k msg/s floor** —
but the bottleneck is the Python exchange **simulator**, not jnxfh/jnxdb:
`jnxfeed.sim` is a single-threaded Python process pushing bytes over a
loopback TCP socket, and jnxfh spends most of its time blocked in
`recv()` waiting on it, not doing decode/apply/publish work. jnxfh has no
offline/file-input mode (it only speaks SoupBinTCP, by design — see
JNX_PLAN2.md §1), so there is no way to drive it without *some* peer
pushing bytes at *some* rate; there is thus no true "sim-less pipeline"
number to measure directly. The honest proxy for jnxfh/jnxdb's own
ceiling is the **decode+apply** row above (~655k msg/s) — the same
market-apply codepath jnxfh runs per message, minus only the socket
recv, the UDS write to jnxdb, and the multicast send jnxfh also does per
message; those three additions are cheap kernel calls on a ~430-byte
record and not expected to be why (c) is so much lower than (b). Both
real feed rates and the plan's own tuning threshold (50k msgs/s) are far
below (b), so no further hot-path tuning was done here either — see
`tools/bench.py` for the full methodology and `make -C cpp bench`
output for a live run.
