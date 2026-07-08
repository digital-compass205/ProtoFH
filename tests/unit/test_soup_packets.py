"""Tests for jnxfeed.soup.packets (JNX_PLAN.md T2.4 / section 3.4)."""
import pytest

from jnxfeed.soup import packets as sp


# --- construction fixtures for all 10 packet types --------------------------

ALL_PACKETS = [
    sp.LoginAccepted(session="ABC123", sequence=12562),
    sp.LoginRejected(reject_code="A"),
    sp.SequencedData(message=b"T\x00\x00\x00\x05"),
    sp.ServerHeartbeat(),
    sp.EndOfSession(),
    sp.DebugPacket(payload=b"hello debug"),
    sp.LoginRequest(
        username="user01",
        password="pass0123",
        requested_session="",
        requested_sequence=1,
    ),
    sp.UnsequencedData(message=b"\x01\x02\x03"),
    sp.ClientHeartbeat(),
    sp.LogoutRequest(),
]


@pytest.mark.parametrize("pkt", ALL_PACKETS)
def test_roundtrip_all_types(pkt):
    wire = sp.encode(pkt)
    # length prefix correctness
    length = int.from_bytes(wire[0:2], "big")
    assert length == len(wire) - 2
    frame = wire[2:]
    decoded = sp.decode_frame(frame)
    assert decoded == pkt


def test_type_bytes_present_on_wire():
    cases = [
        (sp.LoginAccepted(session="S", sequence=1), sp.TYPE_LOGIN_ACCEPTED),
        (sp.LoginRejected(reject_code="A"), sp.TYPE_LOGIN_REJECTED),
        (sp.SequencedData(message=b"x"), sp.TYPE_SEQUENCED_DATA),
        (sp.ServerHeartbeat(), sp.TYPE_SERVER_HEARTBEAT),
        (sp.EndOfSession(), sp.TYPE_END_OF_SESSION),
        (sp.DebugPacket(payload=b"x"), sp.TYPE_DEBUG),
        (sp.LoginRequest("u", "p", "", 0), sp.TYPE_LOGIN_REQUEST),
        (sp.UnsequencedData(message=b"x"), sp.TYPE_UNSEQUENCED_DATA),
        (sp.ClientHeartbeat(), sp.TYPE_CLIENT_HEARTBEAT),
        (sp.LogoutRequest(), sp.TYPE_LOGOUT_REQUEST),
    ]
    for pkt, expected_type in cases:
        wire = sp.encode(pkt)
        assert wire[2:3] == expected_type


# --- padding exactness -------------------------------------------------------

def test_login_request_padding_exact_bytes():
    pkt = sp.LoginRequest(
        username="ab",
        password="xyz",
        requested_session="",
        requested_sequence=1,
    )
    wire = sp.encode(pkt)
    length = int.from_bytes(wire[0:2], "big")
    assert length == 1 + 6 + 10 + 10 + 20  # type + username + password + session + seq
    payload = wire[3:]  # skip length(2) + type(1)
    username_field = payload[0:6]
    password_field = payload[6:16]
    session_field = payload[16:26]
    seq_field = payload[26:46]
    assert username_field == b"ab    "  # right-padded with spaces
    assert password_field == b"xyz       "  # right-padded with spaces
    assert session_field == b"          "  # blank = current => all spaces
    assert seq_field == b" " * 19 + b"1"  # LEFT-padded, value at the right


def test_login_request_requested_session_left_padded_with_value():
    pkt = sp.LoginRequest(
        username="user01",
        password="pw",
        requested_session="SESS1",
        requested_sequence=234751,
    )
    wire = sp.encode(pkt)
    payload = wire[3:]
    session_field = payload[16:26]
    seq_field = payload[26:46]
    assert session_field == b"     SESS1"  # 5 spaces + value, value at right
    seq_str = str(234751)
    assert seq_field == (b" " * (20 - len(seq_str))) + seq_str.encode("ascii")


def test_login_accepted_padding_exact_bytes():
    pkt = sp.LoginAccepted(session="SESS42", sequence=12562)
    wire = sp.encode(pkt)
    length = int.from_bytes(wire[0:2], "big")
    assert length == 1 + 10 + 20
    payload = wire[3:]
    session_field = payload[0:10]
    seq_field = payload[10:30]
    assert session_field == b"    SESS42"  # LEFT-padded, value at the right
    assert seq_field == b" " * 15 + b"12562"  # LEFT-padded, 20-wide ASCII digits


def test_login_accepted_full_width_fields_no_padding_needed():
    session = "SESSIONID1"  # exactly 10 chars
    seq = "12345678901234567890"[:20]  # exactly 20 digits
    pkt = sp.LoginAccepted(session=session, sequence=int(seq))
    wire = sp.encode(pkt)
    payload = wire[3:]
    assert payload[0:10] == session.encode("ascii")
    assert payload[10:30] == seq.encode("ascii")


def test_login_request_decode_strips_padding_correctly():
    pkt = sp.LoginRequest(
        username="u1",
        password="p1",
        requested_session="SESS",
        requested_sequence=42,
    )
    wire = sp.encode(pkt)
    decoded = sp.decode_frame(wire[2:])
    assert decoded.username == "u1"
    assert decoded.password == "p1"
    assert decoded.requested_session == "SESS"
    assert decoded.requested_sequence == 42


def test_field_too_long_raises():
    with pytest.raises(ValueError):
        sp.encode(sp.LoginRequest(username="toolong", password="p", requested_session="", requested_sequence=0))
    with pytest.raises(ValueError):
        sp.encode(sp.LoginAccepted(session="waytoolongsession", sequence=1))


# --- fixed-empty-payload validation ------------------------------------------

def test_empty_payload_types_reject_nonempty_payload():
    for type_byte in (
        sp.TYPE_SERVER_HEARTBEAT,
        sp.TYPE_END_OF_SESSION,
        sp.TYPE_CLIENT_HEARTBEAT,
        sp.TYPE_LOGOUT_REQUEST,
    ):
        with pytest.raises(ValueError):
            sp.decode_frame(type_byte + b"unexpected")


def test_login_rejected_reject_codes():
    for code in (sp.REJECT_NOT_AUTHORIZED, sp.REJECT_SESSION_UNAVAILABLE):
        pkt = sp.LoginRejected(reject_code=code)
        wire = sp.encode(pkt)
        decoded = sp.decode_frame(wire[2:])
        assert decoded.reject_code == code


def test_decode_unknown_type_byte_raises():
    with pytest.raises(ValueError):
        sp.decode_frame(b"?somepayload")


def test_decode_empty_frame_raises():
    with pytest.raises(ValueError):
        sp.decode_frame(b"")


# --- FrameBuffer: whole-packet feeding ---------------------------------------

def test_framebuffer_single_packet_single_feed():
    fb = sp.FrameBuffer()
    wire = sp.encode(sp.ServerHeartbeat())
    packets = fb.feed(wire)
    assert packets == [sp.ServerHeartbeat()]
    assert fb.pending_bytes() == 0


def test_framebuffer_multiple_packets_coalesced():
    fb = sp.FrameBuffer()
    pkts = [
        sp.SequencedData(message=b"T\x00\x00\x00\x05"),
        sp.ServerHeartbeat(),
        sp.SequencedData(message=b"S\x00\x00\x00\x06DAY H"),
    ]
    wire = b"".join(sp.encode(p) for p in pkts)
    decoded = fb.feed(wire)
    assert decoded == pkts
    assert fb.pending_bytes() == 0


def test_framebuffer_byte_by_byte_feeding():
    fb = sp.FrameBuffer()
    pkt = sp.SequencedData(message=b"E\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x02"
                                    b"\x00\x00\x00\x00\x00\x00\x00\x03")
    wire = sp.encode(pkt)
    decoded = []
    for i in range(len(wire)):
        decoded.extend(fb.feed(wire[i:i + 1]))
    assert decoded == [pkt]
    assert fb.pending_bytes() == 0


def test_framebuffer_split_length_prefix():
    fb = sp.FrameBuffer()
    wire = sp.encode(sp.SequencedData(message=b"D\x00\x00\x00\x07"))
    # Feed only the first byte of the 2-byte big-endian length prefix.
    packets = fb.feed(wire[0:1])
    assert packets == []
    assert fb.pending_bytes() == 1
    # Feed the second byte of the length prefix -- still no complete packet.
    packets = fb.feed(wire[1:2])
    assert packets == []
    assert fb.pending_bytes() == 2
    # Feed the rest.
    packets = fb.feed(wire[2:])
    assert packets == [sp.SequencedData(message=b"D\x00\x00\x00\x07")]
    assert fb.pending_bytes() == 0


def test_framebuffer_split_across_multiple_packets_arbitrarily():
    fb = sp.FrameBuffer()
    pkts = [
        sp.LoginAccepted(session="SESS1", sequence=100),
        sp.SequencedData(message=b"H"),
        sp.EndOfSession(),
    ]
    wire = b"".join(sp.encode(p) for p in pkts)
    decoded = []
    # Feed in odd-sized chunks to exercise arbitrary split boundaries.
    chunk_size = 3
    for i in range(0, len(wire), chunk_size):
        decoded.extend(fb.feed(wire[i:i + chunk_size]))
    assert decoded == pkts
    assert fb.pending_bytes() == 0


def test_framebuffer_leftover_partial_packet_after_full_ones():
    fb = sp.FrameBuffer()
    full = sp.encode(sp.ClientHeartbeat())
    partial = sp.encode(sp.SequencedData(message=b"XYZ"))[:-1]  # missing last byte
    decoded = fb.feed(full + partial)
    assert decoded == [sp.ClientHeartbeat()]
    assert fb.pending_bytes() == len(partial)
