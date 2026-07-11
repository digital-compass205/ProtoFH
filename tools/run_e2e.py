#!/usr/bin/env python3
"""run_e2e.py — F6 restart/recovery process orchestrator (dev-only tool).

Starts the Python exchange simulator (``jnxfeed.sim``), ``jnxdb`` and
``jnxfh`` together, drives one of the restart-matrix scenarios below
against a fixture, and collects final DB state (+ mcast spy stats + all
process logs) into a results directory.

Scenarios (see JNX_PLAN2.md F6 / the §1 restart matrix):

  baseline        uninterrupted run — the reference every other scenario
                  is compared against.
  kill_fh         SIGKILL jnxfh at ~40% progress, restart it: must
                  GET_STATE-recover from jnxdb and resume at last_seq+1.
  kill_db         SIGKILL jnxdb at ~40% progress, restart it 2 s later:
                  jnxfh keeps running/multicasting through the outage and
                  resyncs (RESET + full SYNC) once the DB returns.
  drop_exchange   the simulator scripts an abrupt ITCH disconnect at
                  ~40% progress (``--drop-after``); jnxfh's Soup client
                  reconnects and resumes at the next expected seq.
  glimpse_cold    cold start with ``--bootstrap=glimpse`` from a
                  mid-fixture GLIMPSE cut; orders/books/static must match
                  the baseline, trades legitimately differ (documented).
  kill_both       SIGKILL jnxfh AND jnxdb at ~40%, restart DB then FH:
                  DB comes up empty so FH cold-bootstraps (replay from
                  seq 1 — this simulator tolerates a fresh connection
                  re-requesting the whole session, so replay reproduces
                  the baseline exactly, trades included).

Triggers are driven by OBSERVED PROGRESS (polling jnxdb's STATS
last_exch_seq via the query port) — never bare sleeps, except the
scenario-mandated "restart N seconds later" delays, which are part of
the scenario definition, not a synchronization primitive.

Usage:
    python3 tools/run_e2e.py --scenario kill_fh \\
        --fixture tests/fixtures/sample_udp_head.itch [--paced] [--speed N]
        [--out DIR]

Prints PASS/FAIL and the results directory path; exit code 0 on PASS.
"""
import argparse
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JNXDB = os.path.join(REPO_ROOT, "cpp", "build", "jnxdb")
JNXFH = os.path.join(REPO_ROOT, "cpp", "build", "jnxfh")
BOOK_DUMP = os.path.join(REPO_ROOT, "cpp", "build", "book_dump")

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, REPO_ROOT)

from dbquery import query as db_query  # noqa: E402
from jnxfeed import itchfile  # noqa: E402

SCENARIOS = ("baseline", "kill_fh", "kill_db", "drop_exchange",
             "glimpse_cold", "kill_both")

#: STATS/TABLE fields that legitimately differ between an uninterrupted
#: baseline run and a restart scenario, and so are excluded from the
#: "final state identical" comparison. See normalize_stats()/DIFF NOTES
#: below and the final report in RECOVERY.md / the task writeup.
EXCLUDED_STATS_FIELDS = frozenset([
    "epoch",             # fresh per process start / per bootstrap
    "syncs_completed",   # depends on how many resyncs this scenario forced
    "syncs_discarded",   # partial-sync wipes; scenario-dependent
    "orders_applied",    # recovery-path counters: 0 on an uninterrupted run
    "ticks_applied",     # ditto
    "dups_dropped",      # asserted ==0 separately in tests; excluded here
                          # only so a coincidental nonzero value in one run
                          # doesn't get compared against a structurally
                          # unrelated zero in another.
    "updates_applied",   # counts BOTH live UPDATE applies and full-SYNC
                          # dump rows (one per ticker on a resync); a
                          # scenario that forces a resync (kill_db,
                          # kill_both) has a structurally different total
                          # from an uninterrupted baseline even though the
                          # final table CONTENT is identical — the actual
                          # content is what static/state/trades/orders.csv
                          # compare below.
])


# --------------------------------------------------------------------------
# small process/port helpers
# --------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def unique_mcast_group():
    # 239.192.0.0/16 is the locally-scoped admin block already used by the
    # rest of the project; pick a random low-collision address per run.
    return "239.192.{}.{}".format(random.randint(1, 254),
                                  random.randint(1, 254))


def wait_port(port, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.02)
    raise RuntimeError("port {} never came up".format(port))


def stats_dict(port):
    lines = db_query("127.0.0.1", port, "STATS")
    return dict(line.split("=", 1) for line in lines if "=" in line)


def poll_progress(query_port, threshold, timeout=30.0, field="last_exch_seq"):
    """Block until jnxdb STATS[field] >= threshold. No bare sleeps for the
    actual synchronization — this polls observed progress; the poll
    interval itself is just a CPU-friendly backoff, not the trigger."""
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        try:
            st = stats_dict(query_port)
            last = int(st.get(field, -1))
            if last >= threshold:
                return last
        except (OSError, socket.timeout, ValueError):
            pass
        time.sleep(0.01)
    raise RuntimeError("timed out waiting for {} >= {} (last seen {})".format(
        field, threshold, last))


def fixture_msg_count(fixture):
    return sum(1 for _ in itchfile.read_file(fixture))


class Proc(object):
    """A child process with its stdout+stderr captured to a log file."""

    def __init__(self, name, args, log_path, cwd=None, env=None):
        self.name = name
        self.log_path = log_path
        self._log = open(log_path, "wb")
        self.popen = subprocess.Popen(
            args, stdout=self._log, stderr=subprocess.STDOUT,
            cwd=cwd or REPO_ROOT, env=env)

    def poll(self):
        return self.popen.poll()

    def wait(self, timeout=None):
        return self.popen.wait(timeout=timeout)

    def sigkill(self):
        if self.popen.poll() is None:
            self.popen.kill()
            try:
                self.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def terminate(self):
        if self.popen.poll() is None:
            self.popen.terminate()

    def close(self):
        try:
            self._log.close()
        except OSError:
            pass

    def log_text(self):
        with open(self.log_path, "r", errors="replace") as f:
            return f.read()


# --------------------------------------------------------------------------
# the orchestrator
# --------------------------------------------------------------------------

class Runner(object):
    """One scenario run: owns ports/sockets/dirs and every child process,
    guarantees cleanup via teardown()."""

    def __init__(self, fixture, out_dir, speed, mcast_group=None):
        self.fixture = fixture
        self.out_dir = out_dir
        self.speed = speed
        os.makedirs(out_dir, exist_ok=True)
        self.sock_dir = tempfile.mkdtemp(prefix="jnx-e2e-")
        self.mcast_group = mcast_group or unique_mcast_group()
        self.mcast_port = free_port()
        self.procs = []
        self.evidence = []
        self._sock_counter = 0

    # -- process launchers --------------------------------------------------

    def _log_path(self, name):
        return os.path.join(self.out_dir, name + ".log")

    def new_sock_path(self, tag="db"):
        self._sock_counter += 1
        return os.path.join(self.sock_dir, "{}{}.sock".format(
            tag, self._sock_counter))

    def start_db(self, sock_path, query_port, tag="jnxdb"):
        p = Proc(tag, [JNXDB, "--sock=" + sock_path,
                      "--query_port={}".format(query_port)],
                self._log_path(tag))
        self.procs.append(p)
        wait_port(query_port)
        return p

    def start_sim(self, itch_port, glimpse_port, drop_after=None,
                 glimpse_cut=None, tag="sim"):
        args = [sys.executable, "-m", "jnxfeed.sim",
               "--itch-file", self.fixture,
               "--itch-port", str(itch_port),
               "--glimpse-port", str(glimpse_port),
               "--speed", str(self.speed)]
        if drop_after is not None:
            args += ["--drop-after", str(drop_after)]
        if glimpse_cut is not None:
            args += ["--glimpse-cut", str(glimpse_cut)]
        p = Proc(tag, args, self._log_path(tag))
        self.procs.append(p)
        wait_port(itch_port)
        return p

    def start_fh(self, itch_port, glimpse_port, sock_path, query_port,
                bootstrap, tag):
        args = [JNXFH, "--itch_host=127.0.0.1",
               "--itch_port={}".format(itch_port),
               "--glimpse_host=127.0.0.1",
               "--glimpse_port={}".format(glimpse_port),
               "--db_sock=" + sock_path,
               "--bootstrap=" + bootstrap,
               "--mcast_group=" + self.mcast_group,
               "--mcast_port={}".format(self.mcast_port),
               "--mcast_if=127.0.0.1"]
        p = Proc(tag, args, self._log_path(tag))
        self.procs.append(p)
        return p

    def start_spy(self, until_idle=3.0, max_wait=90.0, tag="spy"):
        args = [sys.executable,
               os.path.join(REPO_ROOT, "tools", "mcast_spy.py"),
               "--group", self.mcast_group, "--port", str(self.mcast_port),
               "--iface", "127.0.0.1", "--stats",
               "--until-idle", str(until_idle),
               "--max-wait", str(max_wait)]
        p = Proc(tag, args, self._log_path(tag))
        self.procs.append(p)
        time.sleep(1.0)  # let it join the group before publishing starts
        return p

    # -- dump collection ------------------------------------------------------

    def collect_dump(self, query_port, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        for table in ("static", "state", "trades"):
            lines = db_query("127.0.0.1", query_port, "TABLE " + table)
            with open(os.path.join(dest_dir, table + ".csv"), "w") as f:
                f.write("\n".join(lines) + "\n")
        static_lines = db_query("127.0.0.1", query_port, "TABLE static")
        header = static_lines[0].split(",") if static_lines else []
        tickers = sorted(set(
            ln.split(",")[0] for ln in static_lines[1:] if ln.strip()))
        with open(os.path.join(dest_dir, "orders.csv"), "w") as f:
            f.write("ticker,order_number,side,price,remaining_qty\n")
            for ticker in tickers:
                for ln in db_query("127.0.0.1", query_port,
                                   "ORDERS " + ticker)[1:]:
                    parts = ln.split()
                    if len(parts) == 5:
                        f.write("{},{},{},{},{}\n".format(
                            ticker, parts[0], parts[1], parts[2], parts[3]))
        stats = stats_dict(query_port)
        with open(os.path.join(dest_dir, "stats.txt"), "w") as f:
            for k in sorted(stats):
                f.write("{}={}\n".format(k, stats[k]))
        return stats

    # -- teardown -------------------------------------------------------------

    def teardown(self):
        for p in self.procs:
            p.sigkill()
        for p in self.procs:
            p.close()
        shutil.rmtree(self.sock_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# comparison helper (also imported by tests/integration/test_recovery.py)
# --------------------------------------------------------------------------

def _read_lines(path):
    with open(path) as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


#: Per-section columns dropped before comparison: recency/bookkeeping
#: stamps that a SYNC-dump resync legitimately rewrites for every ticker
#: (not just the ones that actually changed) even though nothing about
#: the ticker's substantive state changed. tools/compare_db_dump.py (the
#: F5-established canonical DB-vs-reference comparator) already excludes
#: exactly these columns from its own state/trades sections — this
#: mirrors that precedent rather than inventing a new exception. See the
#: F6 writeup for the full root-cause explanation (Tables::apply_update's
#: "T2 state — wholesale (last_exch_seq/last_update_ns from envelope)"
#: always stamps a resync row's ENVELOPE seq/ns, which is the sync
#: operation's point in time, not the ticker's own last individual
#: touch).
SECTION_DROP_COLUMNS = {
    "state": frozenset(["last_system_event", "last_exch_seq",
                        "last_update_ns"]),
    "trades": frozenset(["last_trade_ns"]),
}

#: Sections whose rows with an EMPTY ticker (column 0) are pseudo-rows for
#: system-wide 'S'/'L' triggers, not per-instrument state — tools/
#: compare_db_dump.py already treats these as out of scope for any
#: canonical-state comparison (its db_table() helper does
#: `if not d.get("ticker"): continue`). We mirror that precedent instead
#: of inventing a new exception. ("orders" rows always carry a real
#: ticker by construction, so it is not in this set.)
SECTIONS_SKIP_EMPTY_TICKER = frozenset(["static", "state", "trades"])


def normalize_csv(path, drop_cols=None, skip_empty_ticker=False):
    """Header + sorted body lines, with `drop_cols` column names removed
    (by header name) from every row first. Row order in these files is
    not part of the contract, only content; drop_cols removes columns
    that are legitimately allowed to differ (see SECTION_DROP_COLUMNS).
    skip_empty_ticker additionally drops system-wide pseudo-rows (empty
    ticker column) — see SECTIONS_SKIP_EMPTY_TICKER."""
    lines = _read_lines(path)
    if not lines:
        return []
    header = lines[0].split(",")
    if drop_cols:
        keep_idx = [i for i, h in enumerate(header) if h not in drop_cols]
    else:
        keep_idx = list(range(len(header)))

    def project(line):
        parts = line.split(",")
        return ",".join(parts[i] for i in keep_idx if i < len(parts))

    body_lines = lines[1:]
    if skip_empty_ticker:
        body_lines = [ln for ln in body_lines
                     if ln.split(",", 1)[0] != ""]
    body = sorted(project(ln) for ln in body_lines)
    return [project(lines[0])] + body


def normalize_stats(path):
    lines = _read_lines(path)
    out = {}
    for ln in lines:
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        if k in EXCLUDED_STATS_FIELDS:
            continue
        out[k] = v
    return out


def compare_dump_dirs(got_dir, want_dir, sections=("static", "state",
                                                    "trades", "orders"),
                      compare_stats=True):
    """Returns (ok, list-of-mismatch-strings). Compares the normalized
    dump produced by Runner.collect_dump: static/state/trades/orders CSVs
    (sorted, so an unordered-map iteration difference is not a
    mismatch) and, if compare_stats, STATS (excluding the fields in
    EXCLUDED_STATS_FIELDS — see that constant's docstring for why each
    one is excluded). Callers proving a KNOWN, documented partial
    divergence (e.g. glimpse_cold's trade history, or its unrecoverable
    pre-cut system-wide events) pass a narrower `sections` tuple and
    compare_stats=False, since aggregate STATS counters (e.g. `books`)
    fold in the very sections being intentionally skipped."""
    problems = []
    for sec in sections:
        got_path = os.path.join(got_dir, sec + ".csv")
        want_path = os.path.join(want_dir, sec + ".csv")
        drop_cols = SECTION_DROP_COLUMNS.get(sec)
        skip_empty = sec in SECTIONS_SKIP_EMPTY_TICKER
        got = normalize_csv(got_path, drop_cols, skip_empty)
        want = normalize_csv(want_path, drop_cols, skip_empty)
        if got != want:
            g, w = set(got), set(want)
            only_got = sorted(g - w)[:5]
            only_want = sorted(w - g)[:5]
            problems.append(
                "{}: differs (only-in-got={} only-in-want={})".format(
                    sec, only_got, only_want))
    if compare_stats:
        got_stats = normalize_stats(os.path.join(got_dir, "stats.txt"))
        want_stats = normalize_stats(os.path.join(want_dir, "stats.txt"))
        if got_stats != want_stats:
            diffs = []
            for k in sorted(set(got_stats) | set(want_stats)):
                gv, wv = got_stats.get(k), want_stats.get(k)
                if gv != wv:
                    diffs.append("{}: got={} want={}".format(k, gv, wv))
            problems.append("stats: " + "; ".join(diffs))
    return (not problems, problems)


# --------------------------------------------------------------------------
# scenario implementations
# --------------------------------------------------------------------------

def run_baseline(runner, total_msgs, bootstrap="replay", glimpse_cut=None,
                 out_name="expected"):
    sock_path = runner.new_sock_path()
    query_port = free_port()
    itch_port = free_port()
    glimpse_port = free_port()

    runner.start_db(sock_path, query_port)
    runner.start_sim(itch_port, glimpse_port, glimpse_cut=glimpse_cut)
    spy = runner.start_spy()
    fh = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                         bootstrap, "jnxfh")
    rc = fh.wait(timeout=120)
    if rc != 0:
        runner.evidence.append("baseline jnxfh exited {}".format(rc))
        return False, os.path.join(runner.out_dir, out_name)

    spy.wait(timeout=120)
    dest = os.path.join(runner.out_dir, out_name)
    runner.collect_dump(query_port, dest)
    runner.evidence.append("baseline: jnxfh exit 0, dump at " + dest)
    return True, dest


def run_kill_fh(runner, total_msgs, pct=0.4):
    sock_path = runner.new_sock_path()
    query_port = free_port()
    itch_port = free_port()
    glimpse_port = free_port()
    threshold = int(total_msgs * pct)

    runner.start_db(sock_path, query_port)
    runner.start_sim(itch_port, glimpse_port)
    spy = runner.start_spy()
    fh1 = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                          "replay", "jnxfh_1")
    seq_at_kill = poll_progress(query_port, threshold)
    fh1.sigkill()
    runner.evidence.append(
        "kill_fh: SIGKILLed jnxfh at last_exch_seq={} (threshold {})".format(
            seq_at_kill, threshold))
    db_seq_after_kill = int(stats_dict(query_port)["last_exch_seq"])

    fh2 = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                          "replay", "jnxfh_2")
    rc = fh2.wait(timeout=120)
    spy.wait(timeout=120)

    log2 = fh2.log_text()
    resume_line = None
    for ln in log2.splitlines():
        if "resume mode:" in ln:
            resume_line = ln.strip()
            break
    ok = rc == 0 and resume_line is not None
    if resume_line is not None:
        # "resume mode: session='X' seq=N" -- N-1 must equal the DB's
        # last_exch_seq observed right after the kill.
        try:
            seq_resumed = int(resume_line.split("seq=")[1].split()[0])
        except (IndexError, ValueError):
            seq_resumed = None
        ok = ok and (seq_resumed == db_seq_after_kill + 1)
        runner.evidence.append("kill_fh: " + resume_line)
        runner.evidence.append(
            "kill_fh: resumed seq={} == db last_exch_seq+1={}".format(
                seq_resumed, db_seq_after_kill + 1))
    else:
        runner.evidence.append(
            "kill_fh: FAIL — no 'resume mode:' line in restarted jnxfh log")

    stats = stats_dict(query_port)
    dups = int(stats.get("dups_dropped", -1))
    ok = ok and dups == 0
    runner.evidence.append("kill_fh: dups_dropped={} (want 0)".format(dups))

    dest = os.path.join(runner.out_dir, "kill_fh")
    runner.collect_dump(query_port, dest)
    return ok, dest


def run_kill_db(runner, total_msgs, pct=0.4, restart_delay=2.0):
    sock_path = runner.new_sock_path()
    query_port = free_port()
    itch_port = free_port()
    glimpse_port = free_port()
    threshold = int(total_msgs * pct)

    runner.start_db(sock_path, query_port, tag="jnxdb_1")
    runner.start_sim(itch_port, glimpse_port)
    spy = runner.start_spy(until_idle=3.0, max_wait=120.0)
    fh = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                         "replay", "jnxfh")

    seq_at_kill = poll_progress(query_port, threshold)
    db1 = runner.procs[0]  # jnxdb_1, started first
    db1.sigkill()
    runner.evidence.append(
        "kill_db: SIGKILLed jnxdb at last_exch_seq={} (threshold {})".format(
            seq_at_kill, threshold))

    # Scenario-mandated fixed delay before restart (not a sync primitive —
    # the plan specifies "restarted 2 s later" as part of the scenario).
    time.sleep(restart_delay)

    runner.start_db(sock_path, query_port, tag="jnxdb_2")
    rc = fh.wait(timeout=120)
    spy.wait(timeout=120)

    stats = stats_dict(query_port)
    syncs = int(stats.get("syncs_completed", 0))
    ok = rc == 0 and syncs >= 1
    runner.evidence.append(
        "kill_db: after restart, jnxdb_2 syncs_completed={} (want >=1)"
        .format(syncs))

    spy_summary = spy.log_text().strip().splitlines()[-1] if \
        spy.log_text().strip() else ""
    spy_fields = dict(kv.split("=") for kv in spy_summary.split()) \
        if spy_summary else {}
    gaps = int(spy_fields.get("gaps", -1))
    updates = int(spy_fields.get("updates", -1))
    published = int(stats.get("updates_applied", -1))
    ok = ok and gaps == 0
    runner.evidence.append(
        "kill_db: mcast gaps={} during DB outage (FH kept multicasting; "
        "mcast updates={} vs db updates_applied={} — DB write and mcast "
        "are decoupled, per-message mcast never stops)".format(
            gaps, updates, published))

    dest = os.path.join(runner.out_dir, "kill_db")
    runner.collect_dump(query_port, dest)
    return ok, dest


def run_drop_exchange(runner, total_msgs, pct=0.4):
    sock_path = runner.new_sock_path()
    query_port = free_port()
    itch_port = free_port()
    glimpse_port = free_port()
    drop_after = int(total_msgs * pct)

    runner.start_db(sock_path, query_port)
    runner.start_sim(itch_port, glimpse_port, drop_after=drop_after)
    spy = runner.start_spy()
    fh = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                         "replay", "jnxfh")
    rc = fh.wait(timeout=120)
    spy.wait(timeout=120)

    log = fh.log_text()
    saw_disconnect = any("connection lost" in ln for ln in log.splitlines())
    saw_reconnect = any(
        "logging in" in ln and "seq=" in ln
        for ln in log.splitlines()[1:])  # after the first (initial) login
    ok = rc == 0 and saw_disconnect and saw_reconnect
    runner.evidence.append(
        "drop_exchange: scripted disconnect after {} packets; "
        "saw 'connection lost'={} saw reconnect-login={}".format(
            drop_after, saw_disconnect, saw_reconnect))

    stats = stats_dict(query_port)
    dups = int(stats.get("dups_dropped", -1))
    ok = ok and dups == 0
    runner.evidence.append(
        "drop_exchange: dups_dropped={} (want 0)".format(dups))

    spy_summary = spy.log_text().strip().splitlines()[-1] if \
        spy.log_text().strip() else ""
    spy_fields = dict(kv.split("=") for kv in spy_summary.split()) \
        if spy_summary else {}
    gaps = int(spy_fields.get("gaps", -1))
    ok = ok and gaps == 0
    runner.evidence.append(
        "drop_exchange: mcast pub_seq gaps={} (want 0, i.e. no duplicate "
        "or missing publishes across the resume)".format(gaps))

    dest = os.path.join(runner.out_dir, "drop_exchange")
    runner.collect_dump(query_port, dest)
    return ok, dest


def compute_post_cut_trades(fixture, cut):
    """The EXACT expected trades.csv delta for a GLIMPSE bootstrap cut at
    message `cut` (1-based count, i.e. cut = number of messages folded
    into the snapshot; live replay covers messages[cut:]).

    A naive "replay only messages[cut:] through a fresh Market" reference
    is WRONG: post-cut trades can execute against resting orders that
    were themselves opened before the cut (the snapshot restores those
    resting orders via synthetic 'A' rows) -- so post-cut trade volume
    is NOT simply what an isolated tail-replay would produce. The only
    exact reference is a single full-fixture replay through the real
    (order-book-continuous) prototype Market, snapshotting each ticker's
    cumulative trade stats at the cut boundary and diffing against the
    final totals -- i.e. exactly what "cum stats only reflect
    post-snapshot trades" means.

    Returns {ticker: (trade_count, cum_qty, cum_turnover, last_price,
    last_qty, last_match_number)} for every ticker with >0 post-cut
    trades (tickers with none are simply absent -- DB should also show
    trade_count 0 / no trades.csv row content for them).
    """
    from jnxfeed.book.market import Market
    from jnxfeed.itch import codec

    market = Market()
    snapshot = {}       # ticker -> (trade_count, volume, notional) as of cut
    last_match = {}      # ticker -> match_number of its last execution

    with open(fixture, "rb") as f:
        for i, raw in enumerate(itchfile.iter_messages(f)):
            if i == cut:
                snapshot = dict(
                    (oid, (s.trade_count, s.volume, s.notional))
                    for oid, s in market.tape.stats.items())
            execution = market.apply(codec.decode(raw))
            if execution is not None:
                last_match[execution.orderbook_id] = execution.match_number
    if cut >= i + 1:
        snapshot = dict(
            (oid, (s.trade_count, s.volume, s.notional))
            for oid, s in market.tape.stats.items())

    out = {}
    for oid, s in market.tape.stats.items():
        base_count, base_vol, base_notional = snapshot.get(oid, (0, 0, 0))
        post_count = s.trade_count - base_count
        if post_count <= 0:
            continue
        out[oid] = (post_count, s.volume - base_vol,
                    s.notional - base_notional, s.last_price, s.last_qty,
                    last_match.get(oid))
    return out


def _compare_db_dump_sections(query_port, dump_dir):
    """Runs tools/compare_db_dump.py against a live jnxdb query port and
    returns {section_name: 'OK'|'MISMATCH'} plus the raw stdout."""
    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tools",
                                      "compare_db_dump.py"),
         "--port", str(query_port), dump_dir],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True)
    sections = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("OK", "MISMATCH"):
            sections[parts[0]] = parts[1]
    return sections, result.stdout


def run_glimpse_cold(runner, total_msgs, cut_pct=0.4):
    sock_path = runner.new_sock_path()
    query_port = free_port()
    itch_port = free_port()
    glimpse_port = free_port()
    cut = int(total_msgs * cut_pct)

    runner.start_db(sock_path, query_port)
    runner.start_sim(itch_port, glimpse_port, glimpse_cut=cut)
    spy = runner.start_spy()
    fh = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                         "glimpse", "jnxfh")
    rc = fh.wait(timeout=120)
    spy.wait(timeout=120)
    ok = rc == 0
    runner.evidence.append(
        "glimpse_cold: cold start, GLIMPSE cut at message {} of {}"
        .format(cut, total_msgs))

    # Reference #1: a direct book_dump of the WHOLE fixture -- this is
    # what an uninterrupted from-seq-1 run converges to (proven equal by
    # F5's test_jnxfh.py::test_replay_bootstrap_full_pipeline). orders,
    # books and static must match it exactly.
    full_ref = os.path.join(runner.out_dir, "glimpse_cold_full_ref")
    subprocess.run([BOOK_DUMP, runner.fixture, full_ref], check=True)

    # Compare orders/books/static against the full-fixture reference (the
    # trades section is EXPECTED to mismatch here -- the snapshot carries
    # no trade history -- so it is not gated on below).
    full_sections, full_out = _compare_db_dump_sections(query_port, full_ref)
    orders_ok = full_sections.get("orders") == "OK"
    books_ok = full_sections.get("books") == "OK"
    static_ok = full_sections.get("static") == "OK"
    trades_vs_full = full_sections.get("trades")   # expected MISMATCH
    ok = ok and orders_ok and books_ok and static_ok
    runner.evidence.append(
        "glimpse_cold: vs full-fixture reference: orders={} books={} "
        "static={} trades={} (trades MISMATCH here is EXPECTED -- the "
        "snapshot carries no trade history)".format(
            full_sections.get("orders"), full_sections.get("books"),
            full_sections.get("static"), trades_vs_full))
    if not (orders_ok and books_ok and static_ok):
        runner.evidence.append("glimpse_cold: full-ref compare output:\n" +
                               full_out)

    # The EXACT expected trades: computed via a single full-fixture
    # replay through the prototype Market, snapshotting each ticker's
    # cumulative stats at the cut boundary (see compute_post_cut_trades's
    # docstring for why a naive isolated tail-replay is NOT the right
    # reference -- post-cut trades can consume resting pre-cut orders
    # that the snapshot restores).
    expected_trades = compute_post_cut_trades(runner.fixture, cut)
    db_trades_lines = db_query("127.0.0.1", query_port, "TABLE trades")
    db_header = db_trades_lines[0].split(",")
    db_trades = {}
    for ln in db_trades_lines[1:]:
        d = dict(zip(db_header, ln.split(",")))
        if d.get("ticker") and int(d["trade_count"]) > 0:
            db_trades[d["ticker"]] = d

    trade_problems = []
    for ticker, (tc, qty, turnover, price, lqty, match) in \
            sorted(expected_trades.items()):
        d = db_trades.get(ticker)
        if d is None:
            trade_problems.append("{}: expected {} trades, db has none"
                                  .format(ticker, tc))
            continue
        got = (int(d["trade_count"]), int(d["cum_qty"]),
              int(d["cum_turnover"]), int(d["last_price"]),
              int(d["last_qty"]), int(d["last_match_number"]))
        want = (tc, qty, turnover, price or 0, lqty or 0, match or 0)
        if got != want:
            trade_problems.append("{}: db={} want={}".format(
                ticker, got, want))
    extra = sorted(set(db_trades) - set(expected_trades))
    if extra:
        trade_problems.append(
            "db has trades for tickers with none expected post-cut: {}"
            .format(extra))

    trades_ok = not trade_problems
    ok = ok and trades_ok
    runner.evidence.append(
        "glimpse_cold: post-cut trades vs exact expected delta ({} "
        "tickers traded after the cut): {}".format(
            len(expected_trades), "OK" if trades_ok else "MISMATCH"))
    if trade_problems:
        runner.evidence.append("glimpse_cold: trade problems: " +
                               "; ".join(trade_problems[:10]))

    dest = os.path.join(runner.out_dir, "glimpse_cold")
    runner.collect_dump(query_port, dest)

    return ok, dest, expected_trades


def run_kill_both(runner, total_msgs, pct=0.4, restart_delay=0.5):
    sock_path = runner.new_sock_path()
    query_port = free_port()
    itch_port = free_port()
    glimpse_port = free_port()
    threshold = int(total_msgs * pct)

    runner.start_db(sock_path, query_port, tag="jnxdb_1")
    runner.start_sim(itch_port, glimpse_port)
    spy = runner.start_spy(until_idle=3.0, max_wait=120.0)
    fh1 = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                          "replay", "jnxfh_1")

    seq_at_kill = poll_progress(query_port, threshold)
    db1 = runner.procs[0]
    db1.sigkill()
    fh1.sigkill()
    runner.evidence.append(
        "kill_both: SIGKILLed jnxdb AND jnxfh at last_exch_seq={} "
        "(threshold {})".format(seq_at_kill, threshold))

    time.sleep(restart_delay)
    runner.start_db(sock_path, query_port, tag="jnxdb_2")
    # DB is empty (fresh epoch/last_seq=0): jnxfh cold-bootstraps. The
    # simulator tolerates a brand-new connection re-requesting seq 1 (it
    # replays self.messages[0:] regardless of prior connections), so
    # bootstrap=replay reproduces the exact baseline — including trade
    # history, unlike the GLIMPSE path.
    fh2 = runner.start_fh(itch_port, glimpse_port, sock_path, query_port,
                          "replay", "jnxfh_2")
    rc = fh2.wait(timeout=120)
    spy.wait(timeout=120)

    stats = stats_dict(query_port)
    ok = rc == 0
    runner.evidence.append(
        "kill_both: restarted db then fh (bootstrap=replay); "
        "jnxfh_2 exit={} last_exch_seq={}".format(rc,
                                                   stats.get("last_exch_seq")))

    dest = os.path.join(runner.out_dir, "kill_both")
    runner.collect_dump(query_port, dest)
    return ok, dest


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=
                                     argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--out", default=None,
                        help="results directory (default: a fresh tmp dir)")
    parser.add_argument("--paced", action="store_true",
                        help="force paced replay (default speed 300 "
                             "msgs/sec); scenarios that need to land a "
                             "kill mid-stream force this on regardless")
    parser.add_argument("--speed", type=float, default=300.0,
                        help="paced messages/sec (default %(default)s)")
    args = parser.parse_args(argv)

    fixture = os.path.abspath(args.fixture)
    if not os.path.exists(JNXDB) or not os.path.exists(JNXFH):
        print("run_e2e: build cpp first (make -C cpp all)", file=sys.stderr)
        return 2

    needs_pacing = args.scenario in ("kill_fh", "kill_db", "kill_both")
    speed = args.speed if (args.paced or needs_pacing) else "max"

    out_dir = args.out or tempfile.mkdtemp(
        prefix="jnx-e2e-{}-".format(args.scenario))
    os.makedirs(out_dir, exist_ok=True)

    total_msgs = fixture_msg_count(fixture)
    runner = Runner(fixture, out_dir, speed)
    ok = False
    dest = None
    try:
        if args.scenario == "baseline":
            ok, dest = run_baseline(runner, total_msgs)
        elif args.scenario == "kill_fh":
            ok, dest = run_kill_fh(runner, total_msgs)
        elif args.scenario == "kill_db":
            ok, dest = run_kill_db(runner, total_msgs)
        elif args.scenario == "drop_exchange":
            ok, dest = run_drop_exchange(runner, total_msgs)
        elif args.scenario == "glimpse_cold":
            ok, dest, _tail = run_glimpse_cold(runner, total_msgs)
        elif args.scenario == "kill_both":
            ok, dest = run_kill_both(runner, total_msgs)
    finally:
        runner.teardown()

    print("scenario:", args.scenario)
    print("fixture:", fixture)
    for line in runner.evidence:
        print(" -", line)
    print("results dir:", out_dir)
    if dest:
        print("dump dir:", dest)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
