"""End-to-end-ish test for jnxweb (Phase F7): runs ``python3 -m jnxweb``
as a real subprocess with ``--test-feed``, injects canned UPDATE
records built with jnxweb.records encoders over an AF_UNIX datagram
socket (the documented multicast-free record source -- see
jnxweb/mcast.py), then drives it purely as an external client would:
urllib.request against /tickers, /snap/<ticker>, /stats, and
tools/ws_probe.py as a second subprocess for the WebSocket path.

Ports/paths are collision-safe: HTTP binds :0 and the actual port is
parsed from the subprocess's stdout readiness line; the UDS path lives
in pytest's tmp_path.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from jnxweb import records

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

READY_TIMEOUT = 10.0
POLL_TIMEOUT = 5.0


def _blank_update(ticker="8306", epoch=1, pub_seq=1, exch_seq=1,
                  trigger="A", **overrides):
    rec = {
        "kind": "U",
        "epoch": epoch, "pub_seq": pub_seq,
        "session": "SESS000001", "exch_seq": exch_seq, "exch_ns": exch_seq * 1000,
        "trigger": trigger, "ticker": ticker, "group": "DAY",
        "isin": "JP3902400005", "round_lot": 100, "tick_table_id": 1,
        "price_decimals": 1, "upper_limit": 40000, "lower_limit": 20000,
        "flags": 0,
        "trading_state": "T", "short_sell_restriction": "0",
        "reference_price": 30000, "last_system_event": "O",
        "short_sell_price": 0,
        "level_count_bid": 1, "level_count_ask": 1,
        "bids": [(29990, 100, 1)], "asks": [(30010, 200, 2)],
        "total_bid_qty": 100, "total_ask_qty": 200,
        "total_bid_orders": 1, "total_ask_orders": 2,
        "last_price": 30000, "last_qty": 100, "last_match_number": 1,
        "last_trade_ns": exch_seq * 1000, "cum_qty": 100, "cum_turnover": 3000000,
        "trade_count": 1,
        "delta_op": "A", "delta_order_number": 1, "delta_orig_order_number": 0,
        "delta_side": "B", "delta_price": 29990, "delta_qty": 100,
        "delta_order_type": "L",
    }
    rec.update(overrides)
    return rec


@pytest.fixture
def jnxweb_process(tmp_path):
    uds_path = str(tmp_path / "jnxweb_test_feed.sock")
    proc = subprocess.Popen(
        [sys.executable, "-m", "jnxweb",
         "--http-port", "0", "--http-host", "127.0.0.1",
         "--test-feed", uds_path],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    port = None
    deadline = time.time() + READY_TIMEOUT
    lines = []
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            lines.append(line)
            if line.startswith("jnxweb listening on "):
                port = int(line.strip().rsplit(":", 1)[1])
                break
        if port is None:
            proc.terminate()
            proc.wait(timeout=5)
            raise RuntimeError(
                "jnxweb never became ready; output so far:\n" + "".join(lines))

        # Wait for the AF_UNIX test-feed socket to actually exist before
        # any test tries to sendto() it.
        sock_deadline = time.time() + READY_TIMEOUT
        while not os.path.exists(uds_path):
            if time.time() > sock_deadline:
                raise RuntimeError("test-feed socket never appeared")
            time.sleep(0.05)

        yield proc, port, uds_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _inject(uds_path, rec):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(records.encode_update(rec), uds_path)
    finally:
        sock.close()


def _get_json(port, path, expect_status=200):
    url = "http://127.0.0.1:{}{}".format(port, path)
    try:
        with urllib.request.urlopen(url, timeout=POLL_TIMEOUT) as resp:
            assert resp.getcode() == expect_status
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        assert exc.code == expect_status
        return json.loads(exc.read())


def _wait_for_ticker(port, ticker, timeout=POLL_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tickers = _get_json(port, "/tickers")
        if ticker in tickers:
            return
        time.sleep(0.1)
    raise AssertionError("ticker {!r} never appeared in /tickers".format(ticker))


def test_snap_unknown_ticker_is_404(jnxweb_process):
    _proc, port, _uds = jnxweb_process
    body = _get_json(port, "/snap/NOSUCH", expect_status=404)
    assert body == {"error": "unknown"}


def test_inject_and_query_tickers_snap_stats(jnxweb_process):
    _proc, port, uds_path = jnxweb_process
    _inject(uds_path, _blank_update(ticker="8306", pub_seq=1))
    _inject(uds_path, _blank_update(ticker="7203", pub_seq=2))
    _wait_for_ticker(port, "8306")
    _wait_for_ticker(port, "7203")

    tickers = _get_json(port, "/tickers")
    assert tickers == sorted(tickers)
    assert set(["8306", "7203"]).issubset(set(tickers))

    snap = _get_json(port, "/snap/8306")
    assert snap["ticker"] == "8306"
    assert snap["bids"][0][:2] == [29990, 100]
    assert snap["trades"] == []

    stats = _get_json(port, "/stats")
    assert stats["updates"] >= 2
    assert stats["bad"] == 0

    # A bad datagram (garbage bytes) must be counted, not crash the process.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.sendto(b"not a record", uds_path)
    sock.close()
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        stats = _get_json(port, "/stats")
        if stats["bad"] >= 1:
            break
        time.sleep(0.1)
    assert stats["bad"] >= 1


def test_epoch_change_clears_state(jnxweb_process):
    _proc, port, uds_path = jnxweb_process
    _inject(uds_path, _blank_update(ticker="9984", epoch=1, pub_seq=1))
    _wait_for_ticker(port, "9984")

    _inject(uds_path, _blank_update(ticker="9984", epoch=2, pub_seq=1))
    deadline = time.time() + POLL_TIMEOUT
    restarted = False
    while time.time() < deadline:
        stats = _get_json(port, "/stats")
        if stats["restarts"] >= 1 and stats["last_epoch"] == 2:
            restarted = True
            break
        time.sleep(0.1)
    assert restarted


def test_ws_probe_receives_live_frames(jnxweb_process):
    _proc, port, uds_path = jnxweb_process
    _inject(uds_path, _blank_update(ticker="6501", pub_seq=1))
    _wait_for_ticker(port, "6501")

    probe = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "tools", "ws_probe.py"),
         "127.0.0.1:{}".format(port), "6501", "--frames", "3",
         "--timeout", "10"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    # Give the probe a moment to connect + subscribe (which yields frame
    # 1, the immediate snapshot-on-subscribe), then feed two more,
    # spaced past the 10-pushes/s coalescing window so each becomes its
    # own frame.
    time.sleep(0.3)
    _inject(uds_path, _blank_update(ticker="6501", pub_seq=2, exch_seq=2,
                                    last_price=30020))
    time.sleep(0.2)
    _inject(uds_path, _blank_update(ticker="6501", pub_seq=3, exch_seq=3,
                                    last_price=30030))

    stdout, stderr = probe.communicate(timeout=15)
    assert probe.returncode == 0, "stdout={!r} stderr={!r}".format(stdout, stderr)
    frame_lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(frame_lines) == 3
    for line in frame_lines:
        payload = json.loads(line)
        assert payload.get("ticker") == "6501" or payload.get("error")
