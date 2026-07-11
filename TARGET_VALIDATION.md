# TARGET_VALIDATION.md — RHEL 8.10 go-live checklist

Copy-paste script for JNX_PLAN2.md §8, run on the real RHEL 8.10 target
box before go-live. Every command below either prints an expected
value to check by eye or exits non-zero on failure — treat any
deviation as a stop-ship until explained.

Two artifacts you need on the box first:

- The dist tarball built by `make dist` (or `make -C cpp dist`) on the
  dev box — see step 2.
- The Phase-1 Python simulator (`jnxfeed`), needed **only** for step 4's
  offline dry run, since it targets Python 3.6 natively and this repo's
  own dev container cannot run 3.6 to validate it. Copy the whole repo
  (or at least the `jnxfeed/` package) separately for this step — it is
  intentionally NOT part of the dist tarball (dist ships only what
  production needs to run: `cpp/`, `jnxweb/`, target-box `tools/*.py`,
  configs, docs — see `tools/make_dist.py`).

## 1. Toolchain versions

```sh
gcc --version      # expect 8.5.x (RHEL 8 default devtoolset / base gcc)
python3 --version  # expect 3.6.x (RHEL 8 platform python)
make --version      # any GNU make; no cmake needed anywhere in this repo
```
Stop here and escalate if gcc is not 8.5.x — the whole point of this
checklist is catching gcc 15-vs-8.5 divergence (JNX_PLAN2.md §7 risk)
before go-live, and an unexpected compiler version invalidates every
later step.

## 2. Unpack + build + test the tarball

```sh
scp dist/jnx-fh2-*.tar.gz target-box:/tmp/
ssh target-box
mkdir -p ~/jnx-fh2-validate && cd ~/jnx-fh2-validate
tar xzf /tmp/jnx-fh2-*.tar.gz --strip-components=1
make -C cpp all test
```
Expect: clean build (the Makefile uses `-Werror -pedantic` — any
gcc-8.5-specific warning becomes a build failure here, which is the
whole point), then every `cpp/test/test_*.cpp` binary printing
`--- N test(s), 0 failure(s) ---`.

**This must work with only `gcc-c++` and `make` installed** — no git,
no python, no network access needed for this step. If your target image
is genuinely minimal, this is also the moment to confirm that (e.g. by
checking `python3`/`git` aren't accidentally propping up the build via
some unexpected step in the Makefile — they shouldn't be; `cpp/Makefile`
has zero non-toolchain dependencies).

## 3. Python 3.6 compatibility gate

```sh
python3 tools/py36check.py jnxweb/*.py tools/dbquery.py tools/mcast_spy.py \
    tools/ws_probe.py
# exit 0, no output = clean

python3 -m jnxweb --help
# argparse usage text, exits 0 -- proves the module imports cleanly on
# REAL 3.6 (not just passes the AST-level py36check lint the dev
# container's 3.14 interpreter runs)
```
If `--help` fails with an `ImportError`, it's almost certainly a
missing `jnxfeed.net.reactor` — the dist tarball includes the minimal
`jnxfeed/__init__.py` + `jnxfeed/net/__init__.py` + `jnxfeed/net/reactor.py`
slice jnxweb actually imports; confirm those three files unpacked
correctly before assuming it's a real 3.6-compat bug.

## 4. Loopback dry run (needs the separately-copied `jnxfeed` prototype)

```sh
# from the full repo copy (not the dist tarball), python3.6:
python3 -m jnxfeed.sim --itch-file tests/fixtures/sample_udp_head.itch \
    --itch-port 15001 --glimpse-port 15002 --speed realtime &

# from the dist tarball build:
mkdir -p /tmp/jnx-target-dryrun
cpp/build/jnxdb --sock=/tmp/jnx-target-dryrun/db.sock --query_port=26401 &
cpp/build/jnxfh --itch_host=127.0.0.1 --itch_port=15001 \
    --glimpse_host=127.0.0.1 --glimpse_port=15002 \
    --db_sock=/tmp/jnx-target-dryrun/db.sock --bootstrap=replay \
    --mcast_group=239.192.1.1 --mcast_port=26400 --mcast_if=127.0.0.1 &

# after the sim reaches end-of-session:
python3 tools/dbquery.py --port 26401 STATS
```
Expected numbers (must match the dev-box run byte-for-byte — this is
the whole point of the F2/F5 bit-identical gates, now proven on the
real target compiler):
```
last_exch_seq=2000
updates_applied=1934
dups_dropped=0
```
See OPERATIONS.md §6 for the full 4-terminal dry run including
`jnxweb` and the `book_dump`/`compare_db_dump.py` cross-check, and
RECOVERY.md for what every restart scenario should look like if you
want to exercise those too before go-live (`tools/run_e2e.py`, needs
the full repo copy — dist doesn't ship it since it's a dev-only
orchestrator, not a production tool).

## 5. Multicast reachability (two hosts: the FH host and a client host)

On the FH host (or wherever you ran step 4's `jnxfh`), rerun it with
`--mcast_if=<FH-host-real-interface-IP>` (not loopback this time) so
the datagrams actually go out on the wire. On the **client** host:
```sh
python3 tools/mcast_spy.py --group 239.192.1.1 --port 26400 \
    --iface <client-host-real-interface-IP> --stats --until-idle 5
```
Expect `updates=1934 gaps=0 bad=0` (or a live-flow equivalent if this
is run against a real feed instead of the dry run above). `gaps=0` is
the important number — a nonzero gap count here means something in the
network path (firewall, wrong TTL, IGMP snooping misconfigured on a
switch) is dropping multicast between the two hosts, which is a
network-team issue, not a code issue; see OPERATIONS.md §3.3 for what
`gaps` counts and doesn't count.

## 6. UAT probes against the real Japannext line

Only after 1–5 are all clean. This step uses the Phase-1 `jnxfeed`
connectivity kit (needs the full repo copy, python3.6) against real
Japannext UAT endpoints — full checklist, exact commands, and the
expected-outcome table (reject codes, timeout meanings, etc.) are in
**CONNECTIVITY.md §1 and §3**. Do not duplicate that content here; this
step is just the pointer. Bring the JSON reports CONNECTIVITY.md's
probes produce to any support conversation with Japannext
(`ito@japannext.co.jp`).
