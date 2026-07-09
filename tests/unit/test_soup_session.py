"""Tests for jnxfeed.soup.session (JNX_PLAN.md T4.1).

Pure sans-I/O tests: feed bytes built with jnxfeed.soup.packets.encode
directly into a SoupSession, no sockets involved.
"""
from jnxfeed.soup import packets as sp
from jnxfeed.soup import session as ss


def make_session(**kwargs):
    events = {
        "accepted": [],
        "rejected": [],
        "messages": [],
        "ended": [],
        "silent": [],
    }

    def on_login_accepted(session_id, next_seq):
        events["accepted"].append((session_id, next_seq))

    def on_login_rejected(code):
        events["rejected"].append(code)

    def on_message(seq, payload):
        events["messages"].append((seq, payload))

    def on_end_of_session():
        events["ended"].append(True)

    def on_peer_silent():
        events["silent"].append(True)

    session = ss.SoupSession(
        "user01", "pass01",
        on_login_accepted=on_login_accepted,
        on_login_rejected=on_login_rejected,
        on_message=on_message,
        on_end_of_session=on_end_of_session,
        on_peer_silent=on_peer_silent,
        **kwargs
    )
    return session, events


def test_initial_state_and_login_request_wire():
    session, events = make_session()
    assert session.state == ss.STATE_CONNECTED
    session.start(now=0.0)
    assert session.state == ss.STATE_LOGIN_SENT
    out = session.pending_output()
    decoded = sp.decode_frame(out[2:])
    assert isinstance(decoded, sp.LoginRequest)
    assert decoded.username == "user01"
    assert decoded.password == "pass01"
    assert decoded.requested_session == ""
    assert decoded.requested_sequence == 1
    # buffer drained
    assert session.pending_output() == b""


def test_login_accepted_transitions_to_live_and_sets_resume_point():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    wire = sp.encode(sp.LoginAccepted(session="SESSA", sequence=42))
    session.on_bytes(wire, now=0.1)
    assert session.state == ss.STATE_LIVE
    assert session.session_id == "SESSA"
    assert session.next_seq == 42
    assert events["accepted"] == [("SESSA", 42)]


def test_login_rejected_not_authorized():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    wire = sp.encode(sp.LoginRejected(reject_code=sp.REJECT_NOT_AUTHORIZED))
    session.on_bytes(wire, now=0.1)
    assert session.state == ss.STATE_FAILED
    assert session.reject_code == sp.REJECT_NOT_AUTHORIZED
    assert events["rejected"] == [sp.REJECT_NOT_AUTHORIZED]


def test_login_rejected_session_unavailable():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    wire = sp.encode(sp.LoginRejected(reject_code=sp.REJECT_SESSION_UNAVAILABLE))
    session.on_bytes(wire, now=0.1)
    assert session.state == ss.STATE_FAILED
    assert session.reject_code == sp.REJECT_SESSION_UNAVAILABLE
    assert events["rejected"] == [sp.REJECT_SESSION_UNAVAILABLE]


def test_seq_counting_across_many_sequenced_data():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    wire = sp.encode(sp.LoginAccepted(session="SESSB", sequence=100))
    session.on_bytes(wire, now=0.1)

    payloads = [b"payload-%d" % i for i in range(5)]
    for i, payload in enumerate(payloads):
        wire = sp.encode(sp.SequencedData(message=payload))
        session.on_bytes(wire, now=0.2 + i * 0.01)

    assert events["messages"] == [
        (100, b"payload-0"),
        (101, b"payload-1"),
        (102, b"payload-2"),
        (103, b"payload-3"),
        (104, b"payload-4"),
    ]
    assert session.next_seq == 105


def test_byte_dribble_feed_one_byte_at_a_time():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    accepted_wire = sp.encode(sp.LoginAccepted(session="SESSC", sequence=1))
    msg_wire = sp.encode(sp.SequencedData(message=b"hello"))
    wire = accepted_wire + msg_wire

    now = 0.0
    for i in range(len(wire)):
        session.on_bytes(wire[i:i + 1], now=now)
        now += 0.001

    assert events["accepted"] == [("SESSC", 1)]
    assert events["messages"] == [(1, b"hello")]


def test_heartbeat_emitted_after_idle_tick():
    session, events = make_session(heartbeat_interval=1.0)
    session.start(now=0.0)
    session.pending_output()  # drain the login request
    wire = sp.encode(sp.LoginAccepted(session="SESSD", sequence=1))
    session.on_bytes(wire, now=0.1)
    session.pending_output()

    # Not yet idle enough.
    session.on_tick(now=0.5)
    assert session.pending_output() == b""

    # Now past heartbeat_interval since last send (login request at t=0).
    session.on_tick(now=1.2)
    out = session.pending_output()
    assert out != b""
    decoded = sp.decode_frame(out[2:])
    assert isinstance(decoded, sp.ClientHeartbeat)


def test_peer_silent_detection():
    session, events = make_session(silence_timeout=15.0, heartbeat_interval=1.0)
    session.start(now=0.0)
    session.pending_output()
    wire = sp.encode(sp.LoginAccepted(session="SESSE", sequence=1))
    session.on_bytes(wire, now=0.1)

    session.on_tick(now=10.0)  # within timeout, just heartbeats
    assert events["silent"] == []

    session.on_tick(now=15.2)  # > 15s since last received byte (t=0.1)
    assert events["silent"] == [True]


def test_end_of_session_ends_session():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    wire = sp.encode(sp.LoginAccepted(session="SESSF", sequence=1))
    session.on_bytes(wire, now=0.1)
    wire = sp.encode(sp.EndOfSession())
    session.on_bytes(wire, now=0.2)
    assert session.state == ss.STATE_ENDED
    assert events["ended"] == [True]


def test_reset_and_reconnect_resumes_at_maintained_seq():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    session.on_bytes(sp.encode(sp.LoginAccepted(session="SESSG", sequence=50)), now=0.1)
    session.on_bytes(sp.encode(sp.SequencedData(message=b"m1")), now=0.2)
    assert session.next_seq == 51

    # Simulate a disconnect: caller calls reset(), then start() again on
    # the new TCP connection -- must resume with the same session id and
    # the next expected seq, no explicit args required.
    session.reset()
    assert session.state == ss.STATE_CONNECTED
    session.start(now=1.0)
    out = session.pending_output()
    decoded = sp.decode_frame(out[2:])
    assert decoded.requested_session == "SESSG"
    assert decoded.requested_sequence == 51


def test_start_accepts_explicit_override():
    session, events = make_session()
    session.start(now=0.0, requested_session="OTHER", requested_seq=999)
    out = session.pending_output()
    decoded = sp.decode_frame(out[2:])
    assert decoded.requested_session == "OTHER"
    assert decoded.requested_sequence == 999


def test_multiple_packets_in_one_feed_call():
    session, events = make_session()
    session.start(now=0.0)
    session.pending_output()
    wire = (
        sp.encode(sp.LoginAccepted(session="SESSH", sequence=1))
        + sp.encode(sp.SequencedData(message=b"a"))
        + sp.encode(sp.SequencedData(message=b"b"))
        + sp.encode(sp.ServerHeartbeat())
        + sp.encode(sp.SequencedData(message=b"c"))
    )
    session.on_bytes(wire, now=0.1)
    assert events["messages"] == [(1, b"a"), (2, b"b"), (3, b"c")]
    assert session.next_seq == 4
