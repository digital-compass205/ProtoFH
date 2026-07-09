"""Tests for jnxfeed.net.tcp.TcpSoupConnector (JNX_PLAN.md T4.1).

Runs the reactor's run() loop against an in-process, thread-per-connection
blocking-socket stub server (the same pattern as
tests/unit/test_connectivity_kit.py's StubSoupServer -- that one is
blocking/threaded, which is fine for a stub even though the connector
under test is non-blocking). A watchdog timer guarantees the reactor
always exits even if a test's expectations are never met, so the suite
stays fast and never hangs.
"""
import socket
import threading
import time

from jnxfeed.itch import codec, messages
from jnxfeed.net import reactor as reactor_mod
from jnxfeed.net import tcp as tcp_mod
from jnxfeed.soup import packets as sp
from jnxfeed.soup import session as ss

WATCHDOG_TIMEOUT = 5.0


def itch_t(seconds):
    return codec.encode(messages.TimestampSeconds(seconds=seconds))


class StubSoupServer(object):
    """Scripted SoupBinTCP server, one script per accepted connection.

    Script keys: reject, messages (list of raw ITCH payloads), drop_after
    (close abruptly after N SequencedData), end_of_session (send Z at the
    end), heartbeat_after.
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.login_requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self):
        for script in self.scripts:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._handle(conn, script)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _read_login(self, conn):
        fb = sp.FrameBuffer()
        conn.settimeout(5.0)
        while True:
            data = conn.recv(4096)
            if not data:
                return None
            for pkt in fb.feed(data):
                if isinstance(pkt, sp.LoginRequest):
                    return pkt

    def _handle(self, conn, script):
        login = self._read_login(conn)
        if login is None:
            return
        self.login_requests.append(login)

        if script.get("reject"):
            conn.sendall(sp.encode(sp.LoginRejected(reject_code=script["reject"])))
            return

        all_messages = script.get("messages", [])
        start = max(login.requested_sequence, 1)
        conn.sendall(sp.encode(sp.LoginAccepted(session="TESTSESS", sequence=start)))

        sent = 0
        for message in all_messages[start - 1:]:
            if script.get("drop_after") is not None and sent >= script["drop_after"]:
                return
            conn.sendall(sp.encode(sp.SequencedData(message=message)))
            sent += 1
        if script.get("heartbeat_after"):
            conn.sendall(sp.encode(sp.ServerHeartbeat()))
        if script.get("end_of_session"):
            conn.sendall(sp.encode(sp.EndOfSession()))
            conn.settimeout(2.0)
            try:
                conn.recv(4096)
            except (OSError, socket.timeout):
                pass


def run_with_watchdog(reactor, timeout=WATCHDOG_TIMEOUT):
    """Run the reactor, guaranteeing it stops within ``timeout`` seconds."""
    watchdog = reactor.call_later(timeout, reactor.stop)
    reactor.run()
    watchdog.cancel()


def test_login_and_stream_to_end_of_session():
    msgs = [itch_t(34200 + i) for i in range(5)]
    received = []
    ended = []

    with StubSoupServer([{"messages": msgs, "end_of_session": True}]) as srv:
        r = reactor_mod.Reactor()
        session = ss.SoupSession(
            "TEST", "SECRET",
            on_message=lambda seq, payload: received.append((seq, payload)),
            on_end_of_session=lambda: (ended.append(True), r.stop()),
        )
        connector = tcp_mod.TcpSoupConnector(r, "127.0.0.1", srv.port, session,
                                              tick_interval=0.05)
        connector.start()
        run_with_watchdog(r)
        r.close()

    assert received == [(i + 1, msgs[i]) for i in range(5)]
    assert ended == [True]
    assert connector.state == tcp_mod.STATE_STOPPED


def test_login_rejected_surfaces_failure_and_stops():
    rejected = []

    with StubSoupServer([{"reject": sp.REJECT_NOT_AUTHORIZED}]) as srv:
        r = reactor_mod.Reactor()
        session = ss.SoupSession(
            "TEST", "SECRET",
            on_login_rejected=lambda code: (rejected.append(code), r.stop()),
        )
        connector = tcp_mod.TcpSoupConnector(r, "127.0.0.1", srv.port, session,
                                              tick_interval=0.05, max_retries=3,
                                              backoff_initial=0.05)
        connector.start()
        run_with_watchdog(r)
        r.close()

    assert rejected == [sp.REJECT_NOT_AUTHORIZED]
    assert session.state == ss.STATE_FAILED
    # A protocol-level reject is not a transient network issue: no retry.
    assert connector.attempts == 1
    assert connector.state == tcp_mod.STATE_STOPPED


def test_mid_stream_disconnect_reconnect_resumes_no_loss_no_dup():
    msgs = [itch_t(34200 + i) for i in range(6)]
    received = []

    scripts = [
        {"messages": msgs, "drop_after": 3},
        {"messages": msgs, "end_of_session": True},
    ]
    with StubSoupServer(scripts) as srv:
        r = reactor_mod.Reactor()
        session = ss.SoupSession(
            "TEST", "SECRET",
            on_message=lambda seq, payload: received.append(seq),
            on_end_of_session=lambda: r.stop(),
        )
        connector = tcp_mod.TcpSoupConnector(r, "127.0.0.1", srv.port, session,
                                              tick_interval=0.05,
                                              backoff_initial=0.05, backoff_max=0.2)
        connector.start()
        run_with_watchdog(r)
        r.close()

    # No loss, no duplication: seqs 1..6 received exactly once, in order.
    assert received == [1, 2, 3, 4, 5, 6]
    # Resumed the second connection with a requested seq matching the next
    # expected message (4th message == seq 4).
    assert [lr.requested_sequence for lr in srv.login_requests] == [1, 4]


def test_stop_closes_socket_and_cancels_timers():
    with StubSoupServer([{"messages": [], "heartbeat_after": True}]) as srv:
        r = reactor_mod.Reactor()
        session = ss.SoupSession("TEST", "SECRET")
        connector = tcp_mod.TcpSoupConnector(r, "127.0.0.1", srv.port, session,
                                              tick_interval=0.05)
        connector.start()

        def stop_and_halt():
            connector.stop()
            r.stop()

        r.call_later(0.3, stop_and_halt)
        run_with_watchdog(r)
        r.close()

    assert connector.state == tcp_mod.STATE_STOPPED
    assert connector._sock is None


def test_give_up_after_max_retries_when_server_unreachable():
    # No listener on this port.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    gave_up = []
    r = reactor_mod.Reactor()
    session = ss.SoupSession("TEST", "SECRET")
    connector = tcp_mod.TcpSoupConnector(
        r, "127.0.0.1", port, session,
        tick_interval=0.05, max_retries=2,
        backoff_initial=0.02, backoff_max=0.05,
        on_give_up=lambda: (gave_up.append(True), r.stop()),
    )
    connector.start()
    run_with_watchdog(r, timeout=3.0)
    r.close()

    assert gave_up == [True]
    assert connector.attempts == 2
    assert connector.state == tcp_mod.STATE_STOPPED
