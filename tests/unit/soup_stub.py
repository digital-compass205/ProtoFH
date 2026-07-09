"""Shared scripted SoupBinTCP stub server for T5.2+ unit tests.

Thread-per-connection blocking sockets -- deliberately simple; the code
under test is the non-blocking reactor stack, the stub just has to be an
obviously-correct peer. One script dict per accepted connection:

- ``reject``: reject code to answer the login with (then close)
- ``messages``: list of raw ITCH payloads served as SequencedData;
  message i has seq i+1 and the server replays from the client's
  requested seq (Soup replay semantics)
- ``drop_after``: abruptly close after this many SequencedData
- ``heartbeat_after``: send one ServerHeartbeat after the messages
- ``end_of_session``: send Z after the messages
- ``linger``: keep the connection open this many seconds after the
  script (reading and discarding client bytes) instead of closing
"""
import socket
import threading

from jnxfeed.soup import packets as sp


class StubSoupServer(object):
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
        if script.get("end_of_session") or script.get("linger"):
            # Give the client time to read (and, for linger, to act)
            # before the socket dies.
            conn.settimeout(script.get("linger", 2.0))
            try:
                while conn.recv(4096):
                    pass
            except (OSError, socket.timeout):
                pass
