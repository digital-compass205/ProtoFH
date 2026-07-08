"""T3.3 tests: probe + capture against an in-process stub Soup server.

The stub is a scripted, thread-per-connection blocking-socket server:
each test supplies one script per expected connection; a script says how
to answer the login and what to send afterwards. Timeouts are kept short
so the suite stays fast.
"""
import io
import json
import os
import socket
import struct
import threading

import pytest

from jnxfeed import itchfile
from jnxfeed.cli import capture as capture_cli
from jnxfeed.cli import probe as probe_cli
from jnxfeed.itch import codec, messages
from jnxfeed.soup import packets as sp


def itch_t(seconds):
    return codec.encode(messages.TimestampSeconds(seconds=seconds))


def itch_r(book):
    return codec.encode(messages.OrderbookDirectory(
        ns=1, orderbook_id=book, isin="JP0000000000", group="DAY",
        round_lot=100, tick_table_id=1, price_decimals=1,
        upper_limit=99999, lower_limit=1,
    ))


def itch_g(seq):
    return codec.encode(messages.EndOfSnapshot(sequence_number=seq))


class StubSoupServer(object):
    """Scripted SoupBinTCP server. ``scripts`` is a list of dicts, one per
    accepted connection, with keys:

    - reject: reject code to answer login with (then close)
    - messages: list of raw ITCH messages served as SequencedData,
      starting at the client's requested seq (replay semantics: the
      server serves its tail from requested_seq onward)
    - heartbeat_after: send one ServerHeartbeat after the messages
    - end_of_session: send Z after the messages
    - drop_after: abruptly close after this many SequencedData packets
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

    # -- internals -----------------------------------------------------------

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

        # Replay semantics: message i in the script list has seq i+1;
        # serve from the client's requested seq (0 = "most recent" is not
        # needed by these tests).
        all_messages = script.get("messages", [])
        start = max(login.requested_sequence, 1)
        conn.sendall(sp.encode(sp.LoginAccepted(session="TESTSESS", sequence=start)))

        sent = 0
        for message in all_messages[start - 1:]:
            if script.get("drop_after") is not None and sent >= script["drop_after"]:
                return  # abrupt close mid-stream
            conn.sendall(sp.encode(sp.SequencedData(message=message)))
            sent += 1
        if script.get("heartbeat_after"):
            conn.sendall(sp.encode(sp.ServerHeartbeat()))
        if script.get("end_of_session"):
            conn.sendall(sp.encode(sp.EndOfSession()))
            # Give the client a moment to read before the socket dies.
            conn.settimeout(2.0)
            try:
                conn.recv(4096)
            except (OSError, socket.timeout):
                pass


def run_probe(argv):
    out = io.StringIO()
    code = probe_cli.main(argv, out=out)
    return code, out.getvalue()


BASE = ["--user", "TEST", "--pass", "SECRET", "--timeout", "3"]


def test_probe_happy_path(tmp_path):
    report_path = str(tmp_path / "probe.json")
    msgs = [itch_t(34200), itch_r("8306"), itch_t(34201)]
    with StubSoupServer([{"messages": msgs, "heartbeat_after": True}]) as srv:
        code, text = run_probe(
            ["--host", "127.0.0.1", "--port", str(srv.port)] + BASE +
            ["--messages", "3", "--report", report_path]
        )
    assert code == probe_cli.EXIT_OK
    assert "login: ACCEPTED session='TESTSESS' next_seq=1" in text
    assert "3 sequenced message(s) received" in text
    assert "TimestampSeconds" in text and "OrderbookDirectory" in text
    assert "logout: clean" in text
    # login was sent with the right fields
    assert srv.login_requests[0].username == "TEST"
    assert srv.login_requests[0].requested_sequence == 1
    # JSON report exists and matches
    with open(report_path) as f:
        report = json.load(f)
    assert report["exit_code"] == 0
    assert report["session"] == "TESTSESS"
    assert len(report["messages"]) == 3


@pytest.mark.parametrize("code_char", [sp.REJECT_NOT_AUTHORIZED,
                                       sp.REJECT_SESSION_UNAVAILABLE])
def test_probe_login_rejected(code_char):
    with StubSoupServer([{"reject": code_char}]) as srv:
        code, text = run_probe(["--host", "127.0.0.1", "--port", str(srv.port)] + BASE)
    assert code == probe_cli.EXIT_REJECTED
    assert "login: REJECTED code {!r}".format(code_char) in text


def test_probe_connect_refused():
    # Grab a port with no listener.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    code, text = run_probe(["--host", "127.0.0.1", "--port", str(port)] + BASE)
    assert code == probe_cli.EXIT_CONNECT
    assert "connect: FAILED" in text


def test_probe_glimpse_mode():
    msgs = [itch_t(34200), itch_r("8306"), itch_r("8035"), itch_g(424242)]
    with StubSoupServer([{"messages": msgs}]) as srv:
        code, text = run_probe(
            ["--host", "127.0.0.1", "--port", str(srv.port)] + BASE + ["--glimpse"]
        )
    assert code == probe_cli.EXIT_OK
    assert "next live ITCH seq = 424242" in text
    assert "R=2" in text
    # GLIMPSE mode must force a blank requested session (plan section 3.5).
    assert srv.login_requests[0].requested_session == ""


def test_capture_max_messages_and_sidecar(tmp_path):
    out_path = str(tmp_path / "cap.itch")
    msgs = [itch_t(34200 + i) for i in range(5)]
    with StubSoupServer([{"messages": msgs, "end_of_session": True}]) as srv:
        out = io.StringIO()
        code = capture_cli.main(
            ["--host", "127.0.0.1", "--port", str(srv.port)] + BASE +
            ["--out", out_path, "--seq", "1", "--max-messages", "5"], out=out,
        )
    assert code == capture_cli.EXIT_OK
    captured = list(itchfile.read_file(out_path))
    assert captured == msgs
    with open(capture_cli.sidecar_path(out_path)) as f:
        meta = json.load(f)
    assert meta["session"] == "TESTSESS"
    assert meta["first_seq"] == 1
    assert meta["next_seq"] == 6
    assert meta["message_count"] == 5
    assert meta["message_type_counts"] == {"T": 5}
    assert meta["end_reason"] == "max_messages"


def test_capture_reconnect_resume_no_gaps_no_dups(tmp_path):
    out_path = str(tmp_path / "cap.itch")
    msgs = [itch_t(34200 + i) for i in range(5)]
    scripts = [
        # 1st connection: serve 3 messages then drop abruptly.
        {"messages": msgs, "drop_after": 3},
        # 2nd connection: client must resume at seq 4; serve the tail + Z.
        {"messages": msgs, "end_of_session": True},
    ]
    with StubSoupServer(scripts) as srv:
        out = io.StringIO()
        code = capture_cli.main(
            ["--host", "127.0.0.1", "--port", str(srv.port)] + BASE +
            ["--out", out_path, "--seq", "1",
             "--retries", "3", "--retry-delay", "0.1"], out=out,
        )
    assert code == capture_cli.EXIT_OK
    # Resume logged in with exactly the next needed seq.
    assert [lr.requested_sequence for lr in srv.login_requests] == [1, 4]
    # File is gapless and duplicate-free.
    assert list(itchfile.read_file(out_path)) == msgs
    with open(capture_cli.sidecar_path(out_path)) as f:
        meta = json.load(f)
    assert meta["message_count"] == 5
    assert meta["reconnects"] == 1
    assert meta["end_reason"] == "end_of_session"


def test_capture_sequence_gap_is_fatal(tmp_path):
    out_path = str(tmp_path / "cap.itch")
    # Sidecar says we need seq 4, but the server can only start at 6
    # (its replay list is exhausted below the request): simulate by a
    # server whose LoginAccepted seq is beyond the request.
    class GapServer(StubSoupServer):
        def _handle(self, conn, script):
            login = self._read_login(conn)
            self.login_requests.append(login)
            conn.sendall(sp.encode(sp.LoginAccepted(session="TESTSESS", sequence=99)))

    with GapServer([{}]) as srv:
        out = io.StringIO()
        code = capture_cli.main(
            ["--host", "127.0.0.1", "--port", str(srv.port)] + BASE +
            ["--out", out_path, "--seq", "4"], out=out,
        )
    assert code == capture_cli.EXIT_PROTOCOL
    assert "gap" in out.getvalue()
    assert list(itchfile.read_file(out_path)) == []


def test_main_dispatch():
    from jnxfeed import __main__ as main_mod
    # unknown subcommand
    assert main_mod.main(["nonsense"]) == 2
    # planned-but-unimplemented subcommand
    assert main_mod.main(["tail"]) == 2
    # no args = usage, exit 2
    assert main_mod.main([]) == 2
