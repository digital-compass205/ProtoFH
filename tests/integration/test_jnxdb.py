"""F4 integration: drive a real jnxdb process end to end.

Starts cpp/build/jnxdb with a temp UDS path + random query port, connects
raw sockets as a fake FH (using jnxweb.records encoders) and as a query
client, and asserts the full protocol matrix: HELLO handshake, RESET+SYNC
dump, live updates, dup rejection, queries, partial-sync wipe, corrupt
frame handling, SIGTERM clean shutdown.

Code stays 3.6-clean even though it runs on the dev interpreter.
"""
import os
import random
import signal
import socket
import subprocess
import time

import pytest

from jnxweb import records

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
JNXDB = os.path.join(REPO_ROOT, "cpp", "build", "jnxdb")

pytestmark = pytest.mark.skipif(
    not os.path.exists(JNXDB), reason="jnxdb binary not built (make -C cpp)"
)


# --- helpers -------------------------------------------------------------

def query(port, line, timeout=5.0):
    """One command -> list of body lines (terminating '.' stripped)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(line.encode("ascii") + b"\n")
        buf = b""
        while not buf.endswith(b"\n.\n") and buf != b".\n":
            data = s.recv(65536)
            if not data:
                break
            buf += data
    lines = buf.decode("ascii").splitlines()
    if lines and lines[-1] == ".":
        lines = lines[:-1]
    return lines


def stats_dict(port):
    return dict(
        line.split("=", 1) for line in query(port, "STATS") if "=" in line
    )


def wait_for_port(port, proc, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # process died (e.g. port collision)
        try:
            if query(port, "PING") == ["PONG"]:
                return True
        except OSError:
            time.sleep(0.05)
    return False


def recv_exact(sock, n, timeout=5.0):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        data = sock.recv(n - len(buf))
        if not data:
            raise AssertionError("EOF after {} of {} bytes".format(len(buf), n))
        buf += data
    return buf


def recv_record(sock):
    """Read exactly one record (header + body) off a socket."""
    header = recv_exact(sock, records.RECORD_HEADER_SIZE)
    kind, body_len = records.decode_header(header)
    body = recv_exact(sock, body_len) if body_len else b""
    return records.decode_record(header + body)


def make_update(seq, ticker, epoch=1, trigger="A", op="A", order_number=0,
                price=0, qty=0, **extra):
    rec = {
        "kind": "U",
        "epoch": epoch,
        "pub_seq": seq,
        "session": "ITESTSESS",
        "exch_seq": seq,
        "exch_ns": seq * 1000,
        "trigger": trigger,
        "ticker": ticker,
        "group": "DAY",
        "delta_op": op,
        "delta_order_number": order_number,
        "delta_side": "" if op == "#" else "B",
        "delta_price": price,
        "delta_qty": qty,
        "delta_order_type": "" if op == "#" else " ",
    }
    rec.update(extra)
    return records.encode_update(rec)


class DbProc(object):
    """jnxdb under test: process + sock path + query port."""

    def __init__(self, tmpdir):
        self.sock_path = os.path.join(str(tmpdir), "db.sock")
        self.proc = None
        self.port = 0
        for _ in range(20):  # retry on port collision
            port = random.randint(30000, 60000)
            proc = subprocess.Popen(
                [JNXDB, "--sock=" + self.sock_path,
                 "--query_port={}".format(port)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if wait_for_port(port, proc):
                self.proc = proc
                self.port = port
                return
            proc.terminate()
            proc.wait()
        raise AssertionError("could not start jnxdb on any port")

    def connect_fh(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(self.sock_path)
        return s

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def stderr_text(self):
        # only valid after stop()
        return self.proc.stderr.read().decode("ascii", "replace")


@pytest.fixture()
def db(tmp_path):
    d = DbProc(tmp_path)
    yield d
    d.stop()


# --- tests ---------------------------------------------------------------

def test_ping_and_empty_stats(db):
    assert query(db.port, "PING") == ["PONG"]
    st = stats_dict(db.port)
    assert st["session"] == ""
    assert st["epoch"] == "0"
    assert st["last_exch_seq"] == "0"
    assert st["updates_applied"] == "0"
    assert st["books"] == "0"
    assert st["orders_live"] == "0"


def test_unknown_command_and_ticker(db):
    assert query(db.port, "BOGUS") == ["ERR badcmd"]
    assert query(db.port, "GET 0000") == ["ERR unknown"]
    assert query(db.port, "TABLE bogus") == ["ERR badcmd"]


def test_hello_handshake(db):
    fh = db.connect_fh()
    try:
        fh.sendall(records.encode_hello(
            {"kind": "H", "epoch": 5, "last_exch_seq": 99}))
        reply = recv_record(fh)
        # empty DB replies 0/0
        assert reply == {"kind": "H", "epoch": 0, "last_exch_seq": 0}
    finally:
        fh.close()


def test_sync_dump_live_updates_and_queries(db):
    fh = db.connect_fh()
    try:
        # RESET + sync bracket: 2 ticks, 1 order, 1 '#' update, SYNC_END
        payload = records.encode_control("R")
        payload += records.encode_control("B")
        payload += records.encode_tick(
            {"kind": "K", "table_id": 1, "price_start": 0, "tick_size": 1})
        payload += records.encode_tick(
            {"kind": "K", "table_id": 1, "price_start": 30000,
             "tick_size": 5})
        payload += records.encode_order(
            {"kind": "O", "order_number": 7001, "ticker": "8306",
             "group": "DAY", "side": "S", "price": 15010,
             "qty_remaining": 300, "order_type": "Q"})
        payload += make_update(
            100, "8306", trigger="#", op="#",
            isin="JP3902400005", round_lot=100, tick_table_id=1,
            price_decimals=1, upper_limit=200000, lower_limit=100000,
            flags=records.FLAG_DIRECTORY_SEEN,
            trading_state="T", short_sell_restriction="0",
            reference_price=15000, last_system_event="Q",
            level_count_ask=1, asks=[(15010, 300, 1)],
            total_ask_qty=300, total_ask_orders=1)
        payload += records.encode_sync_end(
            {"kind": "E", "session": "ITESTSESS", "last_exch_seq": 100,
             "epoch": 1})
        fh.sendall(payload)

        # live updates: an A, then an E trade. The FH always sends the FULL
        # merged row (wholesale upsert), so static/state sections repeat.
        full_row = dict(
            isin="JP3902400005", round_lot=100, tick_table_id=1,
            price_decimals=1, upper_limit=200000, lower_limit=100000,
            flags=records.FLAG_DIRECTORY_SEEN,
            trading_state="T", short_sell_restriction="0",
            reference_price=15000, last_system_event="Q",
            level_count_ask=1, asks=[(15010, 300, 1)],
            total_ask_qty=300, total_ask_orders=1,
        )
        fh.sendall(make_update(101, "8306", trigger="A", op="A",
                               order_number=7002, price=14990, qty=200,
                               level_count_bid=1, bids=[(14990, 200, 1)],
                               total_bid_qty=200, total_bid_orders=1,
                               **full_row))
        fh.sendall(make_update(102, "8306", trigger="E", op="E",
                               order_number=7002, price=14990, qty=0,
                               last_price=14990, last_qty=200,
                               last_match_number=555, last_trade_ns=102000,
                               cum_qty=200, cum_turnover=2998000,
                               trade_count=1, **full_row))

        # poll STATS until applied
        for _ in range(100):
            st = stats_dict(db.port)
            if st["updates_applied"] == "3" and st["syncs_completed"] == "1":
                break
            time.sleep(0.02)
        st = stats_dict(db.port)
        assert st["session"] == "ITESTSESS"
        assert st["epoch"] == "1"
        assert st["last_exch_seq"] == "102"
        assert st["updates_applied"] == "3"  # 1 sync row + 2 live
        assert st["dups_dropped"] == "0"
        assert st["orders_applied"] == "1"
        assert st["ticks_applied"] == "2"
        assert st["syncs_completed"] == "1"
        assert st["books"] == "1"
        assert st["ticks"] == "2"
        # order 7001 alive; 7002 was inserted then filled to zero
        assert st["orders_live"] == "1"

        # out-of-seq dup: same epoch, old seq -> dropped
        fh.sendall(make_update(50, "8306", trigger="A", op="A",
                               order_number=7099, price=1, qty=1))
        for _ in range(100):
            st = stats_dict(db.port)
            if st["dups_dropped"] == "1":
                break
            time.sleep(0.02)
        assert st["dups_dropped"] == "1"
        assert st["last_exch_seq"] == "102"  # unchanged

        # GET
        got = query(db.port, "GET 8306")
        kv = dict(line.split("=", 1) for line in got if "=" in line)
        assert kv["ticker"] == "8306"
        assert kv["group"] == "DAY"
        assert kv["isin"] == "JP3902400005"
        assert kv["trading_state"] == "T"
        assert kv["reference_price"] == "1500.0"
        assert kv["last_exch_seq"] == "102"
        assert kv["trade_count"] == "1"

        # BOOK
        book = query(db.port, "BOOK 8306")
        assert book[0] == "ticker=8306 group=DAY"
        # after the fill the bid side is empty; the ask level remains and
        # its price renders /10 with one decimal
        assert any("1501.0" in line for line in book)
        assert any("totals:" in line for line in book)

        # ORDERS: only 7001 lives
        orders = query(db.port, "ORDERS 8306")
        assert orders[0].startswith("order_number")
        assert len(orders) == 2
        assert orders[1].split() == ["7001", "S", "1501.0", "300", "DLP"]

        # TRADES: summary + one tape entry (trigger E)
        trades = query(db.port, "TRADES 8306")
        assert trades[0] == "ticker=8306 group=DAY"
        assert any("last_match_number=555" in line for line in trades)
        tape_lines = [l for l in trades if l.startswith("  ")]
        assert len(tape_lines) == 1
        assert tape_lines[0].split() == ["102000", "1499.0", "200", "555"]

        # TABLE CSVs
        tstatic = query(db.port, "TABLE static")
        assert tstatic[0].startswith("ticker,group,isin")
        assert tstatic[1].startswith("8306,DAY,JP3902400005,100,1,1,")
        tstate = query(db.port, "TABLE state")
        assert tstate[1] == "8306,DAY,T,0,15000,Q,102,102000"
        ttrades = query(db.port, "TABLE trades")
        assert ttrades[1] == "8306,DAY,14990,200,555,102000,200,2998000,1"
    finally:
        fh.close()


def test_get_state_returns_dump(db):
    fh = db.connect_fh()
    try:
        fh.sendall(records.encode_control("B"))
        fh.sendall(records.encode_tick(
            {"kind": "K", "table_id": 2, "price_start": 0, "tick_size": 10}))
        fh.sendall(make_update(200, "7203", trigger="#", op="#"))
        fh.sendall(records.encode_sync_end(
            {"kind": "E", "session": "S2", "last_exch_seq": 200,
             "epoch": 3}))
        fh.sendall(records.encode_hello(
            {"kind": "H", "epoch": 0, "last_exch_seq": 0}))
        hello = recv_record(fh)
        assert hello == {"kind": "H", "epoch": 3, "last_exch_seq": 200}

        fh.sendall(records.encode_control("G"))
        recs = [recv_record(fh)]
        while recs[-1]["kind"] != "E":
            recs.append(recv_record(fh))
        kinds = [r["kind"] for r in recs]
        assert kinds == ["B", "K", "U", "E"]
        assert recs[1] == {"kind": "K", "table_id": 2, "price_start": 0,
                           "tick_size": 10}
        assert recs[2]["ticker"] == "7203"
        assert recs[2]["trigger"] == "#"
        assert recs[2]["delta_op"] == "#"
        assert recs[3] == {"kind": "E", "session": "S2",
                           "last_exch_seq": 200, "epoch": 3}
    finally:
        fh.close()


def test_partial_sync_disconnect_wipes(db):
    fh = db.connect_fh()
    fh.sendall(records.encode_control("B"))
    fh.sendall(make_update(10, "8306", trigger="#", op="#"))
    # poll until the row landed
    for _ in range(100):
        if stats_dict(db.port)["books"] == "1":
            break
        time.sleep(0.02)
    fh.close()  # disconnect INSIDE the bracket
    for _ in range(100):
        st = stats_dict(db.port)
        if st["syncs_discarded"] == "1":
            break
        time.sleep(0.02)
    assert st["syncs_discarded"] == "1"
    assert st["books"] == "0"  # wiped
    assert st["updates_applied"] == "0"


def test_live_update_disconnect_does_not_wipe(db):
    fh = db.connect_fh()
    fh.sendall(make_update(10, "8306"))
    for _ in range(100):
        if stats_dict(db.port)["books"] == "1":
            break
        time.sleep(0.02)
    fh.close()  # normal flow, no bracket
    time.sleep(0.2)
    st = stats_dict(db.port)
    assert st["books"] == "1"  # survives
    assert st["syncs_discarded"] == "0"


def test_corrupt_frame_closes_ingest_keeps_tables_and_query(db):
    fh = db.connect_fh()
    fh.sendall(make_update(10, "8306"))
    for _ in range(100):
        if stats_dict(db.port)["books"] == "1":
            break
        time.sleep(0.02)
    fh.sendall(b"\xde\xad\xbe\xef\x00\x00\x00\x00")  # bad magic
    # server closes the connection
    fh.settimeout(5.0)
    assert fh.recv(1) == b""
    fh.close()
    # tables intact, query alive
    st = stats_dict(db.port)
    assert st["books"] == "1"
    # and a new FH connection works
    fh2 = db.connect_fh()
    fh2.sendall(records.encode_hello(
        {"kind": "H", "epoch": 0, "last_exch_seq": 0}))
    assert recv_record(fh2)["kind"] == "H"
    fh2.close()


def test_second_fh_connection_kicks_first(db):
    fh1 = db.connect_fh()
    fh1.sendall(make_update(10, "8306"))
    for _ in range(100):
        if stats_dict(db.port)["books"] == "1":
            break
        time.sleep(0.02)
    fh2 = db.connect_fh()
    fh2.sendall(records.encode_hello(
        {"kind": "H", "epoch": 0, "last_exch_seq": 0}))
    assert recv_record(fh2)["kind"] == "H"  # new conn serviced
    fh1.settimeout(5.0)
    assert fh1.recv(1) == b""  # old conn closed by server
    fh1.close()
    fh2.close()


def test_reset_record_wipes(db):
    fh = db.connect_fh()
    fh.sendall(make_update(10, "8306"))
    for _ in range(100):
        if stats_dict(db.port)["books"] == "1":
            break
        time.sleep(0.02)
    fh.sendall(records.encode_control("R"))
    for _ in range(100):
        st = stats_dict(db.port)
        if st["books"] == "0":
            break
        time.sleep(0.02)
    assert st["books"] == "0"
    assert st["last_exch_seq"] == "0"
    fh.close()


def test_sigterm_clean_shutdown_unlinks_socket(db):
    assert os.path.exists(db.sock_path)
    db.proc.send_signal(signal.SIGTERM)
    assert db.proc.wait(timeout=5) == 0
    assert not os.path.exists(db.sock_path)
    err = db.stderr_text()
    assert "clean shutdown complete" in err


def test_sigkill_then_restart_on_same_socket(db, tmp_path):
    # unclean death leaves a stale socket file; a fresh jnxdb must unlink
    # and rebind it.
    db.proc.kill()
    db.proc.wait(timeout=5)
    assert os.path.exists(db.sock_path)  # stale
    d2 = DbProc(tmp_path)  # same tmpdir -> same sock path
    try:
        assert query(d2.port, "PING") == ["PONG"]
        fh = d2.connect_fh()
        fh.sendall(records.encode_hello(
            {"kind": "H", "epoch": 0, "last_exch_seq": 0}))
        assert recv_record(fh)["kind"] == "H"
        fh.close()
    finally:
        d2.stop()
