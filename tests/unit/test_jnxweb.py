"""Unit tests for jnxweb (Phase F7): wsock framing/handshake, state
epoch/trades logic, and httpd routing driven in-process against a real
loopback socket. No live multicast needed here -- see
tests/unit/test_jnxweb_e2e.py for the subprocess/--test-feed path.
"""
import base64
import json
import socket
import struct
import threading
import time

import pytest

from jnxfeed.net.reactor import Reactor
from jnxweb import (dbquery_client, httpd, records, snapshot as snapshot_mod,
                    state as state_mod, wsock)
from jnxweb.static_page import PAGE_HTML

NO_PRICE = 0x7FFFFFFF


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


# --- wsock: handshake ------------------------------------------------------

def test_accept_key_rfc6455_example():
    # RFC 6455 §1.3 worked example.
    assert (wsock.compute_accept_key("dGhlIHNhbXBsZSBub25jZQ==")
            == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


# --- wsock: frame codec ------------------------------------------------------

def _mask_client_frame(opcode, payload, fin=True):
    out = bytearray()
    out.append((0x80 if fin else 0) | opcode)
    mask_key = b"\x01\x02\x03\x04"
    length = len(payload)
    if length < 126:
        out.append(0x80 | length)
    elif length < 65536:
        out.append(0x80 | 126)
        out += struct.pack(">H", length)
    else:
        out.append(0x80 | 127)
        out += struct.pack(">Q", length)
    out += mask_key
    out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(out)


def test_decode_frame_short_payload():
    frame = _mask_client_frame(wsock.OP_TEXT, b"hi")
    result = wsock.decode_frame(bytearray(frame))
    assert result == (True, wsock.OP_TEXT, b"hi", len(frame))


def test_decode_frame_16bit_length():
    payload = b"x" * 300
    frame = _mask_client_frame(wsock.OP_BINARY, payload)
    fin, opcode, decoded, consumed = wsock.decode_frame(bytearray(frame))
    assert (fin, opcode, decoded, consumed) == (True, wsock.OP_BINARY, payload, len(frame))


def test_decode_frame_64bit_length():
    payload = b"y" * 70000
    frame = _mask_client_frame(wsock.OP_BINARY, payload)
    fin, opcode, decoded, consumed = wsock.decode_frame(bytearray(frame))
    assert decoded == payload
    assert consumed == len(frame)


def test_decode_frame_incomplete_returns_none():
    frame = _mask_client_frame(wsock.OP_TEXT, b"hello world")
    assert wsock.decode_frame(bytearray(frame[:3])) is None


def test_decode_frame_rejects_unmasked():
    # Server-side encode_frame never masks -- feeding one to decode_frame
    # (which expects a CLIENT frame) must be rejected as a protocol error.
    frame = wsock.encode_frame(wsock.OP_TEXT, b"hi")
    with pytest.raises(wsock.FrameError):
        wsock.decode_frame(bytearray(frame))


def test_encode_text_frame_roundtrip_via_client_decoder():
    # jnxweb/tools/ws_probe.py's decode_server_frame logic, inlined here
    # to keep this test self-contained: unmasked frame -> payload.
    frame = wsock.encode_text_frame('{"a": 1}')
    assert frame[0] == 0x81  # FIN=1, opcode=TEXT
    assert (frame[1] & 0x80) == 0  # server frames are not masked
    length = frame[1] & 0x7F
    assert frame[2:2 + length].decode("utf-8") == '{"a": 1}'


def test_encode_close_and_ping_pong_frames():
    close = wsock.encode_close_frame(1000, "bye")
    assert close[0] == 0x80 | wsock.OP_CLOSE
    payload = close[2:]
    code = struct.unpack(">H", payload[:2])[0]
    assert code == 1000
    assert payload[2:] == b"bye"


# --- wsock: WebSocketClient over a real socketpair, reactor stepped manually --

def _step(reactor, times=1):
    for _ in range(times):
        reactor._run_due_timers()
        events = reactor._selector.select(0.05)
        for key, mask in events:
            st = key.data
            if mask & 0x1 and st.read_cb:
                st.read_cb()
            if mask & 0x2 and st.write_cb:
                st.write_cb()


class _FakeHub(object):
    def __init__(self, snap=None):
        self.snap = snap
        self.removed = []

    def lookup(self, ticker):
        return self.snap.get(ticker) if self.snap else None

    def remove(self, client):
        self.removed.append(client)


def test_ws_client_ping_gets_pong():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub()
    client = wsock.WebSocketClient(b, reactor, hub)
    try:
        a.sendall(_mask_client_frame(wsock.OP_PING, b"pingdata"))
        _step(reactor, 2)
        data = a.recv(4096)
        fin, opcode, payload, consumed = _decode_unmasked(data)
        assert opcode == wsock.OP_PONG
        assert payload == b"pingdata"
    finally:
        client.close()
        a.close()
        reactor.close()


def _decode_unmasked(data):
    b0, b1 = data[0], data[1]
    opcode = b0 & 0x0F
    fin = bool(b0 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack_from(">H", data, offset)[0]
        offset += 2
    elif length == 127:
        length = struct.unpack_from(">Q", data, offset)[0]
        offset += 8
    payload = data[offset:offset + length]
    return fin, opcode, payload, offset + length


def test_ws_client_subscribe_unknown_ticker_gets_error():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub(snap={})
    client = wsock.WebSocketClient(b, reactor, hub)
    try:
        a.sendall(_mask_client_frame(wsock.OP_TEXT, b'{"sub": "NOPE"}'))
        _step(reactor, 2)
        data = a.recv(4096)
        _, opcode, payload, _ = _decode_unmasked(data)
        assert opcode == wsock.OP_TEXT
        assert json.loads(payload.decode("utf-8")) == {"error": "unknown"}
    finally:
        client.close()
        a.close()
        reactor.close()


def test_ws_client_subscribe_known_ticker_gets_snapshot():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub(snap={"8306": {"kind": "U", "ticker": "8306"}})
    client = wsock.WebSocketClient(b, reactor, hub)
    try:
        a.sendall(_mask_client_frame(wsock.OP_TEXT, b'{"sub": "8306"}'))
        _step(reactor, 2)
        data = a.recv(4096)
        _, opcode, payload, _ = _decode_unmasked(data)
        assert json.loads(payload.decode("utf-8")) == {"kind": "U", "ticker": "8306"}
    finally:
        client.close()
        a.close()
        reactor.close()


def test_ws_client_close_frame_gets_close_reply_and_teardown():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub()
    client = wsock.WebSocketClient(b, reactor, hub)
    a.sendall(_mask_client_frame(wsock.OP_CLOSE, b""))
    _step(reactor, 3)
    data = a.recv(4096)
    _, opcode, _, _ = _decode_unmasked(data)
    assert opcode == wsock.OP_CLOSE
    assert hub.removed == [client]
    a.close()
    reactor.close()


def test_ws_client_fragmented_frame_rejected():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub()
    client = wsock.WebSocketClient(b, reactor, hub)
    a.sendall(_mask_client_frame(wsock.OP_TEXT, b"partial", fin=False))
    _step(reactor, 3)
    assert hub.removed == [client]
    a.close()
    reactor.close()


def test_ws_client_send_buffer_cap_drops_client():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub()
    client = wsock.WebSocketClient(b, reactor, hub)
    huge = {"blob": "x" * (wsock.MAX_SEND_BUF + 1)}
    client._send_json(huge)
    assert hub.removed == [client]
    a.close()
    reactor.close()


def test_ws_client_coalesces_rapid_updates():
    a, b = socket.socketpair()
    reactor = Reactor()
    hub = _FakeHub()
    client = wsock.WebSocketClient(b, reactor, hub)
    try:
        client.queue_update({"n": 1})
        client.queue_update({"n": 2})
        client.queue_update({"n": 3})
        # A short, single, tightly-bounded pump: just enough to flush the
        # immediate first push, well under the 100ms coalescing window so
        # the pending {"n": 3} timer can't have fired yet.
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            events = reactor._selector.select(0.01)
            for key, mask in events:
                st = key.data
                if mask & 0x2 and st.write_cb:
                    st.write_cb()
            if not client.send_buf:
                break
        data = a.recv(65536)
        # only the latest payload should have gone out immediately;
        # 2 and 3 got coalesced into the pending slot with 3 winning
        # (and its flush timer hasn't fired within this short window).
        _, _, payload, consumed = _decode_unmasked(data)
        first = json.loads(payload.decode("utf-8"))
        assert first == {"n": 1}
        assert len(data) == consumed  # nothing else arrived yet (coalescing)
        assert client._pending_payload == {"n": 3}
    finally:
        client.close()
        a.close()
        reactor.close()


# --- state ---------------------------------------------------------------

def test_state_apply_and_snapshot():
    updates = []
    st = state_mod.State(on_ticker_update=lambda t: updates.append(t))
    assert st.snapshot("8306") is None
    st.apply_update(_blank_update(ticker="8306", pub_seq=1))
    assert updates == ["8306"]
    snap = st.snapshot("8306")
    assert snap["ticker"] == "8306"
    assert snap["trades"] == []
    assert st.ticker_list() == ["8306"]


def test_state_trade_ring_only_on_trigger_e():
    st = state_mod.State()
    st.apply_update(_blank_update(ticker="8306", pub_seq=1, trigger="A"))
    st.apply_update(_blank_update(ticker="8306", pub_seq=2, trigger="E",
                                  last_price=30010, last_qty=50, exch_seq=2))
    snap = st.snapshot("8306")
    assert len(snap["trades"]) == 1
    assert snap["trades"][0]["price"] == 30010
    assert snap["trades"][0]["qty"] == 50


def test_state_trade_ring_bounded_and_newest_first():
    st = state_mod.State()
    for i in range(60):
        st.apply_update(_blank_update(ticker="8306", pub_seq=i + 1,
                                      trigger="E", last_price=i,
                                      exch_seq=i + 1))
    snap = st.snapshot("8306")
    assert len(snap["trades"]) == 50
    assert snap["trades"][0]["price"] == 59  # newest first
    assert snap["trades"][-1]["price"] == 10


def test_state_epoch_change_clears_and_fires_restart():
    restarts = []
    st = state_mod.State(on_restart=lambda e: restarts.append(e))
    st.apply_update(_blank_update(ticker="8306", epoch=1, pub_seq=1))
    st.apply_update(_blank_update(ticker="8306", epoch=1, pub_seq=2,
                                  trigger="E"))
    assert st.ticker_list() == ["8306"]
    st.apply_update(_blank_update(ticker="8306", epoch=2, pub_seq=1))
    assert restarts == [2]
    assert st.restarts == 1
    snap = st.snapshot("8306")
    assert snap["trades"] == []  # trade ring cleared by the epoch change


def test_state_first_record_does_not_count_as_restart():
    restarts = []
    st = state_mod.State(on_restart=lambda e: restarts.append(e))
    st.apply_update(_blank_update(epoch=5, pub_seq=1))
    assert restarts == []
    assert st.restarts == 0


def test_state_gap_counting():
    st = state_mod.State()
    st.apply_update(_blank_update(pub_seq=1))
    st.apply_update(_blank_update(pub_seq=2))
    st.apply_update(_blank_update(pub_seq=5))  # expected 3, saw 5 -> gap of 2
    assert st.gaps == 2


def test_state_bad_datagram_counter():
    st = state_mod.State()
    st.record_bad()
    st.record_bad()
    assert st.stats()["bad"] == 2


# --- state: merge_snapshot (mid-day bootstrap) ------------------------------

def _snap_row(ticker, epoch, exch_seq, **overrides):
    # A snapshot row is a decoded UPDATE dict (trigger '#', pub_seq 0) --
    # exactly what jnxdb's SNAP emits via make_dump_update.
    return _blank_update(ticker=ticker, epoch=epoch, pub_seq=0,
                         exch_seq=exch_seq, trigger="#", **overrides)


def test_merge_snapshot_seeds_empty_state():
    updates = []
    st = state_mod.State(on_ticker_update=lambda t: updates.append(t))
    merged = st.merge_snapshot(
        [_snap_row("8306", 7, 100), _snap_row("9984", 7, 205)], 7)
    assert merged == 2
    assert st.ticker_list() == ["8306", "9984"]
    assert st.last_epoch == 7
    assert sorted(updates) == ["8306", "9984"]
    assert st.stats()["snapshots"] == 1
    assert st.stats()["snapshot_rows"] == 2


def test_merge_snapshot_does_not_regress_fresher_live():
    st = state_mod.State()
    # Live feed already delivered a newer image for 8306 (exch_seq 300).
    st.apply_update(_blank_update(ticker="8306", epoch=7, pub_seq=1,
                                  exch_seq=300, reference_price=31000))
    merged = st.merge_snapshot([_snap_row("8306", 7, 100,
                                           reference_price=15000)], 7)
    assert merged == 0
    assert st.snapshot("8306")["reference_price"] == 31000  # live kept


def test_merge_snapshot_overwrites_staler_live():
    st = state_mod.State()
    st.apply_update(_blank_update(ticker="8306", epoch=7, pub_seq=1,
                                  exch_seq=50, reference_price=15000))
    merged = st.merge_snapshot([_snap_row("8306", 7, 120,
                                           reference_price=16000)], 7)
    assert merged == 1
    assert st.snapshot("8306")["reference_price"] == 16000  # snapshot wins


def test_merge_snapshot_older_epoch_discarded():
    st = state_mod.State()
    st.apply_update(_blank_update(ticker="8306", epoch=9, pub_seq=1,
                                  exch_seq=10, reference_price=31000))
    merged = st.merge_snapshot([_snap_row("8306", 8, 999,
                                           reference_price=15000)], 8)
    assert merged == 0
    assert st.last_epoch == 9
    assert st.snapshot("8306")["reference_price"] == 31000


def test_merge_snapshot_newer_epoch_clears_and_adopts():
    restarts = []
    st = state_mod.State(on_restart=lambda e: restarts.append(e))
    st.apply_update(_blank_update(ticker="8306", epoch=7, pub_seq=1,
                                  exch_seq=800, reference_price=15000))
    merged = st.merge_snapshot(
        [_snap_row("8306", 8, 5, reference_price=16000),
         _snap_row("9984", 8, 6)], 8)
    # Old-epoch 8306 image was wiped; both fresh rows seeded.
    assert restarts == [8]
    assert merged == 2
    assert st.last_epoch == 8
    assert st.ticker_list() == ["8306", "9984"]
    assert st.snapshot("8306")["reference_price"] == 16000


def test_merge_snapshot_order_independent_with_live():
    # snapshot-then-live and live-then-snapshot converge to the same image.
    def build(order):
        st = state_mod.State()
        live = _blank_update(ticker="8306", epoch=7, pub_seq=1, exch_seq=300,
                             reference_price=31000)
        snap = _snap_row("8306", 7, 100, reference_price=15000)
        for step in order:
            if step == "live":
                st.apply_update(live)
            else:
                st.merge_snapshot([snap], 7)
        return st.snapshot("8306")["reference_price"]

    assert build(["snap", "live"]) == build(["live", "snap"]) == 31000


# --- snapshot: SNAP reply parsing -------------------------------------------

def _snap_reply_text(rows, epoch, last_exch_seq=0, session="SESS000001"):
    lines = ["SNAP epoch={} last_exch_seq={} session={} count={}".format(
        epoch, last_exch_seq, session, len(rows))]
    for rec in rows:
        lines.append(base64.b64encode(records.encode_update(rec)).decode())
    lines.append(".")
    return "\n".join(lines) + "\n"


def test_parse_snap_reply_roundtrips():
    rows = [_snap_row("8306", 7, 100), _snap_row("9984", 7, 205)]
    text = _snap_reply_text(rows, epoch=7, last_exch_seq=205)
    parsed, snap_epoch, err = snapshot_mod._parse_snap_reply(text)
    assert err is None
    assert snap_epoch == 7
    assert [r["ticker"] for r in parsed] == ["8306", "9984"]
    assert parsed[0]["exch_seq"] == 100 and parsed[0]["trigger"] == "#"


def test_parse_snap_reply_empty_db():
    text = _snap_reply_text([], epoch=0)
    parsed, snap_epoch, err = snapshot_mod._parse_snap_reply(text)
    assert err is None and parsed == [] and snap_epoch == 0


def test_parse_snap_reply_count_mismatch_is_error():
    rows = [_snap_row("8306", 7, 100)]
    text = _snap_reply_text(rows, epoch=7)
    text = text.replace("count=1", "count=2")  # header lies about the count
    parsed, snap_epoch, err = snapshot_mod._parse_snap_reply(text)
    assert parsed is None and "truncated" in err


def test_parse_snap_reply_err_line():
    parsed, snap_epoch, err = snapshot_mod._parse_snap_reply("ERR badcmd\n.\n")
    assert parsed is None and "rejected" in err


# --- snapshot: SnapshotBootstrap end-to-end over a stub jnxdb ---------------

def _stub_snap_server(reply_bytes):
    """One-shot loopback TCP server that answers the first 'SNAP' command
    with `reply_bytes`. Returns the bound port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(1024)
            if not chunk:
                break
            buf += chunk
        conn.sendall(reply_bytes)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def _run_reactor_until(reactor, predicate, timeout=3.0):
    def _tick():
        reactor.call_later(0.02, _tick)
    reactor.call_later(0.02, _tick)
    thread = threading.Thread(target=reactor.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and not predicate():
            time.sleep(0.02)
    finally:
        reactor.stop()
        thread.join(timeout=2.0)


def test_snapshot_bootstrap_end_to_end():
    rows = [_snap_row("8306", 7, 100), _snap_row("9984", 7, 205)]
    reply = _snap_reply_text(rows, epoch=7, last_exch_seq=205).encode("ascii")
    port = _stub_snap_server(reply)

    reactor = Reactor()
    st = state_mod.State()
    boot = snapshot_mod.SnapshotBootstrap(reactor, st, ("127.0.0.1", port))
    # Kick start() on the reactor thread (registration must not race it).
    reactor.call_later(0.0, boot.start)
    _run_reactor_until(reactor, lambda: st.stats()["snapshots"] > 0)

    assert st.ticker_list() == ["8306", "9984"]
    assert st.last_epoch == 7
    assert st.snapshot("8306")["exch_seq"] == 100


def test_snapshot_bootstrap_disabled_is_noop():
    reactor = Reactor()
    st = state_mod.State()
    boot = snapshot_mod.SnapshotBootstrap(reactor, st, None)
    boot.start()          # no db addr -> must not raise or fetch
    boot.on_restart(9)
    boot.close()
    assert st.stats()["snapshots"] == 0
    assert st.ticker_list() == []


# --- httpd: real loopback server, reactor driven in a background thread ----

@pytest.fixture
def running_server():
    reactor = Reactor()
    st = state_mod.State()
    hub = wsock.WsHub(reactor, st)
    st.on_ticker_update = hub.on_ticker_update
    st.on_restart = hub.on_restart
    server = httpd.HttpServer(reactor, st, hub, PAGE_HTML,
                              host="127.0.0.1", port=0)

    # Bound how long the reactor's select() can block so reactor.stop()
    # (called from the test thread below) takes effect promptly instead
    # of waiting on a select() with no timeout.
    def _tick():
        reactor.call_later(0.02, _tick)
    reactor.call_later(0.02, _tick)

    thread = threading.Thread(target=reactor.run, daemon=True)
    thread.start()
    try:
        yield server, st, hub
    finally:
        reactor.stop()
        thread.join(timeout=2.0)


def _get(server, path):
    import urllib.request
    url = "http://127.0.0.1:{}{}".format(server.port, path)
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.getcode(), resp.read()
    except Exception as exc:  # urllib.error.HTTPError has .code/.read()
        if hasattr(exc, "code") and hasattr(exc, "read"):
            return exc.code, exc.read()
        raise


def test_httpd_root_serves_page(running_server):
    server, st, hub = running_server
    status, body = _get(server, "/")
    assert status == 200
    assert b"jnxweb" in body


def test_httpd_tickers_empty_then_populated(running_server):
    server, st, hub = running_server
    status, body = _get(server, "/tickers")
    assert status == 200
    assert json.loads(body) == []

    st.apply_update(_blank_update(ticker="7203"))
    status, body = _get(server, "/tickers")
    assert json.loads(body) == ["7203"]


def test_httpd_snap_unknown_ticker_404(running_server):
    server, st, hub = running_server
    status, body = _get(server, "/snap/NOPE")
    assert status == 404
    assert json.loads(body) == {"error": "unknown"}


def test_httpd_snap_known_ticker(running_server):
    server, st, hub = running_server
    st.apply_update(_blank_update(ticker="8306"))
    status, body = _get(server, "/snap/8306")
    assert status == 200
    payload = json.loads(body)
    assert payload["ticker"] == "8306"
    assert payload["trades"] == []


def test_httpd_stats(running_server):
    server, st, hub = running_server
    st.apply_update(_blank_update(ticker="8306"))
    status, body = _get(server, "/stats")
    assert status == 200
    payload = json.loads(body)
    assert payload["updates"] == 1
    assert payload["tickers"] == 1


def test_httpd_404_unknown_route(running_server):
    server, st, hub = running_server
    status, body = _get(server, "/nope")
    assert status == 404


def test_httpd_websocket_end_to_end(running_server):
    server, st, hub = running_server
    st.apply_update(_blank_update(ticker="8306"))

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(("127.0.0.1", server.port))
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    request = (
        "GET /ws HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).format(key)
    sock.sendall(request.encode("ascii"))
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    header, _, rest = bytes(buf).partition(b"\r\n\r\n")
    assert header.startswith(b"HTTP/1.1 101")
    assert wsock.compute_accept_key(key).encode("ascii") in header

    sock.sendall(_mask_client_frame(wsock.OP_TEXT, b'{"sub": "8306"}'))
    time.sleep(0.1)
    data = rest + sock.recv(65536)
    _, opcode, payload, _ = _decode_unmasked(data)
    assert opcode == wsock.OP_TEXT
    assert json.loads(payload.decode("utf-8"))["ticker"] == "8306"
    sock.close()


# --- dbquery_client: reply parsing -----------------------------------------

def test_parse_orders_reply_strips_terminator_and_parses_rows():
    text = (
        "order_number side price qty_remaining type\n"
        "202310190000001279 S 1081.2 100 -\n"
        "202310190000001491 B 1080.5 100 DLP\n"
        ".\n"
    )
    rows, err = dbquery_client._parse_orders_reply(text)
    assert err is None
    assert rows == [
        {"order_number": "202310190000001279", "side": "S", "price": "1081.2",
         "qty_remaining": 100, "order_type": "-"},
        {"order_number": "202310190000001491", "side": "B", "price": "1080.5",
         "qty_remaining": 100, "order_type": "DLP"},
    ]


def test_parse_orders_reply_empty_book_is_header_only():
    rows, err = dbquery_client._parse_orders_reply(
        "order_number side price qty_remaining type\n.\n")
    assert err is None
    assert rows == []


def test_parse_orders_reply_err_line_reports_unknown():
    rows, err = dbquery_client._parse_orders_reply("ERR unknown\n.\n")
    assert rows is None
    assert err == "unknown"


# --- dbquery_client: OrdersQuery against a fake jnxdb query server ----------

class _FakeQueryServer(object):
    """Blocking one-shot TCP server that speaks jnxdb's line protocol
    (a text reply terminated by a lone '.' line, connection left open)
    just enough to drive OrdersQuery end-to-end without a real jnxdb."""

    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve_one, daemon=True)
        self.thread.start()

    def _serve_one(self):
        conn, _ = self.sock.accept()
        conn.settimeout(5)
        buf = b""
        while b"\n" not in buf:
            buf += conn.recv(4096)
        conn.sendall(self.reply_text.encode("ascii"))
        time.sleep(0.2)  # stay open a beat, like jnxdb does
        conn.close()

    def close(self):
        self.sock.close()
        self.thread.join(timeout=2.0)


def _run_orders_query(host, port, ticker, timeout=3.0):
    reactor = Reactor()
    result = {}

    def on_done(rows, error):
        result["rows"] = rows
        result["error"] = error
        reactor.stop()

    dbquery_client.OrdersQuery(reactor, host, port, ticker, on_done,
                              timeout=timeout)

    def _watchdog():
        reactor.stop()
    reactor.call_later(timeout + 1.0, _watchdog)
    reactor.run()
    reactor.close()
    return result


def test_orders_query_success_end_to_end():
    server = _FakeQueryServer(
        "order_number side price qty_remaining type\n"
        "1 B 1080.5 100 -\n.\n")
    try:
        result = _run_orders_query("127.0.0.1", server.port, "9511")
    finally:
        server.close()
    assert result["error"] is None
    assert result["rows"] == [
        {"order_number": "1", "side": "B", "price": "1080.5",
         "qty_remaining": 100, "order_type": "-"},
    ]


def test_orders_query_unknown_ticker():
    server = _FakeQueryServer("ERR unknown\n.\n")
    try:
        result = _run_orders_query("127.0.0.1", server.port, "ZZZZ")
    finally:
        server.close()
    assert result["rows"] is None
    assert result["error"] == "unknown"


def test_orders_query_connection_refused_reports_error():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()  # nothing listens on this port now
    result = _run_orders_query("127.0.0.1", closed_port, "9511")
    assert result["rows"] is None
    assert result["error"] is not None


# --- httpd: /orders/<ticker> route ------------------------------------------

def test_httpd_orders_503_when_db_query_not_configured():
    reactor = Reactor()
    st = state_mod.State()
    hub = wsock.WsHub(reactor, st)
    server = httpd.HttpServer(reactor, st, hub, PAGE_HTML,
                              host="127.0.0.1", port=0)

    def _tick():
        reactor.call_later(0.02, _tick)
    reactor.call_later(0.02, _tick)
    thread = threading.Thread(target=reactor.run, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/orders/9511")
        assert status == 503
        assert json.loads(body) == {"error": "db query not configured"}
    finally:
        reactor.stop()
        thread.join(timeout=2.0)


def test_httpd_orders_proxies_fake_jnxdb():
    fake_db = _FakeQueryServer(
        "order_number side price qty_remaining type\n"
        "1 B 1080.5 100 -\n.\n")
    reactor = Reactor()
    st = state_mod.State()
    hub = wsock.WsHub(reactor, st)
    server = httpd.HttpServer(reactor, st, hub, PAGE_HTML,
                              host="127.0.0.1", port=0,
                              db_query_addr=("127.0.0.1", fake_db.port))

    def _tick():
        reactor.call_later(0.02, _tick)
    reactor.call_later(0.02, _tick)
    thread = threading.Thread(target=reactor.run, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/orders/9511")
        assert status == 200
        payload = json.loads(body)
        assert payload["ticker"] == "9511"
        assert payload["orders"] == [
            {"order_number": "1", "side": "B", "price": "1080.5",
             "qty_remaining": 100, "order_type": "-"},
        ]
    finally:
        reactor.stop()
        thread.join(timeout=2.0)
        fake_db.close()
