"""F5 integration: the full C++ feed-handler pipeline against the Python
exchange simulator on the head fixture.

    simulator (jnxfeed.sim) --Soup/ITCH--> jnxfh --UDS--> jnxdb
                                              \\--mcast--> mcast_spy

Asserts (replay bootstrap): jnxfh exits 0 on Z; jnxdb STATS shows
last_exch_seq == fixture message count, updates_applied == messages - T
count, dups_dropped == 0; the multicast is gap-free with the same update
count; and tools/compare_db_dump.py finds the DB equal to a direct
cpp/build/book_dump of the fixture across every section.

A GLIMPSE-bootstrap variant checks snapshot+live lands on the same final
book/order state (trade history legitimately differs — the snapshot
carries open orders, not past trades).
"""
import os
import socket
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
JNXDB = os.path.join(REPO_ROOT, "cpp", "build", "jnxdb")
JNXFH = os.path.join(REPO_ROOT, "cpp", "build", "jnxfh")
BOOK_DUMP = os.path.join(REPO_ROOT, "cpp", "build", "book_dump")
FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures",
                       "sample_udp_head.itch")

# Head fixture ground truth (see F2): 2000 messages, 66 of them 'T'.
FIXTURE_MSGS = 2000
FIXTURE_T = 66
FIXTURE_PUBLISHED = FIXTURE_MSGS - FIXTURE_T  # 1934
FIXTURE_LIVE_ORDERS = 425

pytestmark = pytest.mark.skipif(
    not (os.path.exists(JNXDB) and os.path.exists(JNXFH)),
    reason="C++ binaries not built (make -C cpp all)",
)

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
from dbquery import query  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("port {} never came up".format(port))


def stats_dict(port):
    return dict(line.split("=", 1) for line in query("127.0.0.1", port,
                                                     "STATS"))


class Stack(object):
    """jnxdb + simulator (+ optionally a spy) with guaranteed teardown."""

    def __init__(self, tmp_path, mcast_group):
        self.procs = []
        self.sock_path = str(tmp_path / "db.sock")
        self.query_port = free_port()
        self.itch_port = free_port()
        self.glimpse_port = free_port()
        self.mcast_group = mcast_group
        self.mcast_port = free_port()

    def start_db(self):
        p = subprocess.Popen(
            [JNXDB, "--sock=" + self.sock_path,
             "--query_port={}".format(self.query_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        wait_port(self.query_port)
        return p

    def start_sim(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "jnxfeed.sim", "--itch-file", FIXTURE,
             "--itch-port", str(self.itch_port),
             "--glimpse-port", str(self.glimpse_port)],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.procs.append(p)
        wait_port(self.itch_port)
        return p

    def start_spy(self):
        p = subprocess.Popen(
            [sys.executable, os.path.join(REPO_ROOT, "tools",
                                          "mcast_spy.py"),
             "--group", self.mcast_group, "--port", str(self.mcast_port),
             "--iface", "127.0.0.1", "--stats", "--until-idle", "2",
             "--max-wait", "60"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, universal_newlines=True)
        self.procs.append(p)
        time.sleep(1.5)  # let it join the group before the FH blasts
        return p

    def run_fh(self, bootstrap):
        return subprocess.run(
            [JNXFH, "--itch_host=127.0.0.1",
             "--itch_port={}".format(self.itch_port),
             "--glimpse_host=127.0.0.1",
             "--glimpse_port={}".format(self.glimpse_port),
             "--db_sock=" + self.sock_path,
             "--bootstrap=" + bootstrap,
             "--mcast_group=" + self.mcast_group,
             "--mcast_port={}".format(self.mcast_port),
             "--mcast_if=127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120)

    def teardown(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture
def stack(tmp_path):
    st = Stack(tmp_path, "239.192.61.1")
    yield st
    st.teardown()


def test_replay_bootstrap_full_pipeline(stack, tmp_path):
    stack.start_db()
    stack.start_sim()
    spy = stack.start_spy()

    result = stack.run_fh("replay")
    assert result.returncode == 0  # clean exit on Z

    spy_out, _ = spy.communicate(timeout=90)
    summary = spy_out.strip().splitlines()[-1]
    fields = dict(kv.split("=") for kv in summary.split())
    assert int(fields["updates"]) == FIXTURE_PUBLISHED
    assert int(fields["gaps"]) == 0
    assert int(fields["bad"]) == 0
    assert int(fields["first_pub_seq"]) == 1
    assert int(fields["last_pub_seq"]) == FIXTURE_PUBLISHED

    stats = stats_dict(stack.query_port)
    assert int(stats["last_exch_seq"]) == FIXTURE_MSGS
    assert int(stats["updates_applied"]) == FIXTURE_PUBLISHED
    assert int(stats["dups_dropped"]) == 0
    assert int(stats["orders_live"]) == FIXTURE_LIVE_ORDERS
    assert stats["session"] == "SIM0000001"

    # DB content equals a direct replay through the C++ market core.
    ref_dir = str(tmp_path / "ref")
    subprocess.run([BOOK_DUMP, FIXTURE, ref_dir], check=True)
    cmp_result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tools",
                                      "compare_db_dump.py"),
         "--port", str(stack.query_port), ref_dir],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, universal_newlines=True)
    assert cmp_result.returncode == 0, cmp_result.stdout


def test_glimpse_bootstrap(stack, tmp_path):
    stack.start_db()
    stack.start_sim()

    result = stack.run_fh("glimpse")
    assert result.returncode == 0

    stats = stats_dict(stack.query_port)
    # Snapshot cut is 50% (sim default): live replay covers 1001..2000.
    assert int(stats["last_exch_seq"]) == FIXTURE_MSGS
    assert int(stats["dups_dropped"]) == 0
    # Final order book state must equal the full replay's.
    assert int(stats["orders_live"]) == FIXTURE_LIVE_ORDERS

    # Orders + books (state rebuilt via snapshot) must match the direct
    # dump; trade history legitimately differs (snapshot has no tape), so
    # only assert the order table here via compare's per-section output.
    ref_dir = str(tmp_path / "ref")
    subprocess.run([BOOK_DUMP, FIXTURE, ref_dir], check=True)
    cmp_result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tools",
                                      "compare_db_dump.py"),
         "--port", str(stack.query_port), ref_dir],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, universal_newlines=True)
    sections = dict()
    for line in cmp_result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("OK", "MISMATCH"):
            sections[parts[0]] = parts[1]
    assert sections.get("orders") == "OK", cmp_result.stdout
    assert sections.get("books") == "OK", cmp_result.stdout
