"""SoupBinTCP framing codec (JNX_PLAN.md section 3.4).

Wire format for every logical packet: ``length:2:num`` (big-endian,
excludes the length field itself) + ``type:1:char`` + a payload whose
shape depends on the type byte. A TCP stream delivers these length-framed
packets split or coalesced arbitrarily across ``recv()`` calls, so this
module also provides :class:`FrameBuffer`, an incremental reassembler
that turns a byte stream into a list of complete decoded packets.

This module is sans-I/O: it only ever touches ``bytes``, never sockets.
Pure functions/NamedTuples encode/decode all 10 SoupBinTCP packet types:

Server -> client: Login Accepted (A), Login Rejected (J), Sequenced Data
(S), Server Heartbeat (H), End of Session (Z), Debug (+).

Client -> server: Login Request (L), Unsequenced Data (U), Client
Heartbeat (R), Logout Request (O).
"""
import struct
from typing import NamedTuple

_LEN_STRUCT = struct.Struct(">H")

# --- packet type bytes ---------------------------------------------------

TYPE_LOGIN_ACCEPTED = b"A"
TYPE_LOGIN_REJECTED = b"J"
TYPE_SEQUENCED_DATA = b"S"
TYPE_SERVER_HEARTBEAT = b"H"
TYPE_END_OF_SESSION = b"Z"
TYPE_DEBUG = b"+"

TYPE_LOGIN_REQUEST = b"L"
TYPE_UNSEQUENCED_DATA = b"U"
TYPE_CLIENT_HEARTBEAT = b"R"
TYPE_LOGOUT_REQUEST = b"O"

# --- Login Rejected reason codes -----------------------------------------

REJECT_NOT_AUTHORIZED = "A"
REJECT_SESSION_UNAVAILABLE = "S"

# Field widths (bytes), per plan section 3.4.
_SESSION_WIDTH = 10
_SEQUENCE_WIDTH = 20
_USERNAME_WIDTH = 6
_PASSWORD_WIDTH = 10


# --- packet NamedTuples ---------------------------------------------------
#
# Server -> client

class LoginAccepted(NamedTuple):
    """'A' — session:10 (LEFT-padded), sequence:20 (LEFT-padded, next seq)."""
    session: str
    sequence: int


class LoginRejected(NamedTuple):
    """'J' — reject_code:1 ('A' not authorized / 'S' session unavailable)."""
    reject_code: str


class SequencedData(NamedTuple):
    """'S' — one ITCH message per packet."""
    message: bytes


class ServerHeartbeat(NamedTuple):
    """'H' — no payload."""


class EndOfSession(NamedTuple):
    """'Z' — no payload; socket is closed after this."""


class DebugPacket(NamedTuple):
    """'+' — free-form debug text; ignored by clients."""
    payload: bytes


# Client -> server

class LoginRequest(NamedTuple):
    """'L' — username/password right-padded; session/seq LEFT-padded."""
    username: str
    password: str
    requested_session: str
    requested_sequence: int


class UnsequencedData(NamedTuple):
    """'U' — unsequenced data (unused for market data)."""
    message: bytes


class ClientHeartbeat(NamedTuple):
    """'R' — no payload."""


class LogoutRequest(NamedTuple):
    """'O' — no payload."""


# --- padding helpers -------------------------------------------------------

def _to_bytes(value, encoding="ascii"):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value.encode(encoding)


def _pad_right(value, width):
    """Right-pad (value at the left, spaces fill the right)."""
    raw = _to_bytes(value)
    if len(raw) > width:
        raise ValueError("value {!r} exceeds field width {}".format(value, width))
    return raw + b" " * (width - len(raw))


def _pad_left(value, width):
    """Left-pad (value at the right, spaces fill the left)."""
    raw = _to_bytes(value)
    if len(raw) > width:
        raise ValueError("value {!r} exceeds field width {}".format(value, width))
    return b" " * (width - len(raw)) + raw


def _frame(type_byte, payload):
    body = type_byte + payload
    return _LEN_STRUCT.pack(len(body)) + body


def _require_empty_payload(payload, type_name):
    if payload:
        raise ValueError(
            "{} packet must have an empty payload, got {} bytes".format(
                type_name, len(payload)
            )
        )


# --- encoders ---------------------------------------------------------------

def encode_login_accepted(pkt):
    payload = _pad_left(pkt.session, _SESSION_WIDTH) + _pad_left(
        str(pkt.sequence), _SEQUENCE_WIDTH
    )
    return _frame(TYPE_LOGIN_ACCEPTED, payload)


def encode_login_rejected(pkt):
    payload = _to_bytes(pkt.reject_code)
    if len(payload) != 1:
        raise ValueError("reject_code must be exactly one character")
    return _frame(TYPE_LOGIN_REJECTED, payload)


def encode_sequenced_data(pkt):
    return _frame(TYPE_SEQUENCED_DATA, bytes(pkt.message))


def encode_server_heartbeat(pkt=None):
    return _frame(TYPE_SERVER_HEARTBEAT, b"")


def encode_end_of_session(pkt=None):
    return _frame(TYPE_END_OF_SESSION, b"")


def encode_debug(pkt):
    return _frame(TYPE_DEBUG, _to_bytes(pkt.payload))


def encode_login_request(pkt):
    payload = (
        _pad_right(pkt.username, _USERNAME_WIDTH)
        + _pad_right(pkt.password, _PASSWORD_WIDTH)
        + _pad_left(pkt.requested_session, _SESSION_WIDTH)
        + _pad_left(str(pkt.requested_sequence), _SEQUENCE_WIDTH)
    )
    return _frame(TYPE_LOGIN_REQUEST, payload)


def encode_unsequenced_data(pkt):
    return _frame(TYPE_UNSEQUENCED_DATA, bytes(pkt.message))


def encode_client_heartbeat(pkt=None):
    return _frame(TYPE_CLIENT_HEARTBEAT, b"")


def encode_logout_request(pkt=None):
    return _frame(TYPE_LOGOUT_REQUEST, b"")


_ENCODERS = {
    LoginAccepted: encode_login_accepted,
    LoginRejected: encode_login_rejected,
    SequencedData: encode_sequenced_data,
    ServerHeartbeat: encode_server_heartbeat,
    EndOfSession: encode_end_of_session,
    DebugPacket: encode_debug,
    LoginRequest: encode_login_request,
    UnsequencedData: encode_unsequenced_data,
    ClientHeartbeat: encode_client_heartbeat,
    LogoutRequest: encode_logout_request,
}


def encode(packet):
    """Encode any packet NamedTuple to full wire bytes (length-prefixed)."""
    try:
        encoder = _ENCODERS[type(packet)]
    except KeyError:
        raise TypeError("unknown SoupBinTCP packet type: {!r}".format(type(packet)))
    return encoder(packet)


# --- decoders ----------------------------------------------------------------
# Each decoder takes the payload only (type byte already stripped).

def _decode_login_accepted(payload):
    width = _SESSION_WIDTH + _SEQUENCE_WIDTH
    if len(payload) != width:
        raise ValueError(
            "Login Accepted payload must be {} bytes, got {}".format(width, len(payload))
        )
    session = payload[0:_SESSION_WIDTH].decode("ascii").strip()
    seq_text = payload[_SESSION_WIDTH:width].decode("ascii").strip()
    if not seq_text.isdigit():
        raise ValueError("Login Accepted sequence is not ASCII digits: {!r}".format(seq_text))
    return LoginAccepted(session=session, sequence=int(seq_text))


def _decode_login_rejected(payload):
    if len(payload) != 1:
        raise ValueError(
            "Login Rejected payload must be 1 byte, got {}".format(len(payload))
        )
    return LoginRejected(reject_code=payload.decode("ascii"))


def _decode_sequenced_data(payload):
    return SequencedData(message=bytes(payload))


def _decode_server_heartbeat(payload):
    _require_empty_payload(payload, "Server Heartbeat")
    return ServerHeartbeat()


def _decode_end_of_session(payload):
    _require_empty_payload(payload, "End of Session")
    return EndOfSession()


def _decode_debug(payload):
    return DebugPacket(payload=bytes(payload))


def _decode_login_request(payload):
    width = _USERNAME_WIDTH + _PASSWORD_WIDTH + _SESSION_WIDTH + _SEQUENCE_WIDTH
    if len(payload) != width:
        raise ValueError(
            "Login Request payload must be {} bytes, got {}".format(width, len(payload))
        )
    offset = 0
    username = payload[offset:offset + _USERNAME_WIDTH].decode("ascii").rstrip(" ")
    offset += _USERNAME_WIDTH
    password = payload[offset:offset + _PASSWORD_WIDTH].decode("ascii").rstrip(" ")
    offset += _PASSWORD_WIDTH
    requested_session = payload[offset:offset + _SESSION_WIDTH].decode("ascii").strip()
    offset += _SESSION_WIDTH
    seq_text = payload[offset:offset + _SEQUENCE_WIDTH].decode("ascii").strip()
    if not seq_text.isdigit():
        raise ValueError("Login Request requested_sequence is not ASCII digits: {!r}".format(seq_text))
    return LoginRequest(
        username=username,
        password=password,
        requested_session=requested_session,
        requested_sequence=int(seq_text),
    )


def _decode_unsequenced_data(payload):
    return UnsequencedData(message=bytes(payload))


def _decode_client_heartbeat(payload):
    _require_empty_payload(payload, "Client Heartbeat")
    return ClientHeartbeat()


def _decode_logout_request(payload):
    _require_empty_payload(payload, "Logout Request")
    return LogoutRequest()


_DECODERS = {
    TYPE_LOGIN_ACCEPTED: _decode_login_accepted,
    TYPE_LOGIN_REJECTED: _decode_login_rejected,
    TYPE_SEQUENCED_DATA: _decode_sequenced_data,
    TYPE_SERVER_HEARTBEAT: _decode_server_heartbeat,
    TYPE_END_OF_SESSION: _decode_end_of_session,
    TYPE_DEBUG: _decode_debug,
    TYPE_LOGIN_REQUEST: _decode_login_request,
    TYPE_UNSEQUENCED_DATA: _decode_unsequenced_data,
    TYPE_CLIENT_HEARTBEAT: _decode_client_heartbeat,
    TYPE_LOGOUT_REQUEST: _decode_logout_request,
}


def decode_frame(frame):
    """Decode one logical frame (type byte + payload, NO length prefix)."""
    frame = bytes(frame)
    if not frame:
        raise ValueError("empty SoupBinTCP frame (missing type byte)")
    type_byte = frame[0:1]
    payload = frame[1:]
    try:
        decoder = _DECODERS[type_byte]
    except KeyError:
        raise ValueError("unknown SoupBinTCP packet type byte: {!r}".format(type_byte))
    return decoder(payload)


# --- incremental reassembly --------------------------------------------------

class FrameBuffer:
    """Incremental SoupBinTCP frame reassembler.

    Feed raw bytes as they arrive from a TCP socket (which may split any
    packet, including the 2-byte length prefix itself, across arbitrary
    ``recv()`` boundaries, or coalesce several packets into one read).
    ``feed()`` returns the list of complete packets newly decoded from
    the accumulated buffer; partial data is retained internally until the
    rest arrives.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        """Append ``data`` and return the list of newly complete packets."""
        self._buf.extend(data)
        packets = []
        while True:
            if len(self._buf) < 2:
                break
            length = _LEN_STRUCT.unpack_from(self._buf, 0)[0]
            total = 2 + length
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[2:total])
            del self._buf[:total]
            packets.append(decode_frame(frame))
        return packets

    def pending_bytes(self):
        """Number of buffered bytes not yet forming a complete packet."""
        return len(self._buf)
