"""Stdlib classic-pcap reader (JNX_PLAN.md section 3.7) — offline tooling.

Reads classic (pre-pcapng) ``.pcap`` capture files as produced by
``tcpdump``/``wireshark`` and used for the official Japannext sample
captures. Two link-layer types are supported, matching the samples:

- **linktype 1** (Ethernet) for the UDP/MoldUDP64 capture — transparently
  skips any number of stacked VLAN tags (802.1Q / 802.1ad "QinQ").
- **linktype 113** (Linux "cooked" capture, SLL) for the TCP/GLIMPSE
  captures.

:func:`read_pcap` yields one record per captured frame:
``(timestamp, proto, src_ip, dst_ip, sport, dport, payload)`` where
``proto`` is ``"TCP"`` or ``"UDP"``; non-IP or non-TCP/UDP frames are
skipped. It is a plain generator over the file — no per-packet object
graph beyond the tuple itself, so scanning a 26 MB / 224k-packet capture
is a single fast pass.

Also provided, per plan section 3.7:

- :func:`parse_mold_header` / :func:`iter_mold_messages` — MoldUDP64
  unwrap for the UDP sample (offline fixture extraction only; the live
  transport is SoupBinTCP, not MoldUDP64).
- :func:`reassemble_tcp_streams` — minimal in-order, per-direction TCP
  segment reassembly for the clean TCP/GLIMPSE sample captures.

This module is sans-I/O in the sense of never touching a live socket —
it only reads local capture files. It is offline tooling, not part of
the live transport.
"""
import struct
from collections import OrderedDict

# --- pcap global/record header parsing -----------------------------------

# Magic bytes are stored on disk in the *writer's* native byte order, so
# the raw 4 bytes tell us both the byte order of every subsequent field
# and the per-packet timestamp resolution (microsecond vs nanosecond).
_MAGIC_TABLE = {
    b"\xa1\xb2\xc3\xd4": (">", 1000),  # big-endian fields, microsecond ts
    b"\xd4\xc3\xb2\xa1": ("<", 1000),  # little-endian fields, microsecond ts
    b"\xa1\xb2\x3c\x4d": (">", 1),     # big-endian fields, nanosecond ts
    b"\x4d\x3c\xb2\xa1": ("<", 1),     # little-endian fields, nanosecond ts
}

_GLOBAL_HEADER_REST_LEN = 20  # bytes after the 4-byte magic

LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113

PROTO_TCP = "TCP"
PROTO_UDP = "UDP"

_IP_PROTO_TCP = 6
_IP_PROTO_UDP = 17

_VLAN_ETHERTYPES = frozenset((0x8100, 0x88A8, 0x9100, 0x9200, 0x9300))
_ETHERTYPE_IPV4 = 0x0800
_MAX_VLAN_TAGS = 8  # guards against malformed frames looping forever

_TCP_FLAG_FIN = 0x01
_TCP_FLAG_SYN = 0x02


class PcapError(Exception):
    """Raised for malformed/unreadable pcap data."""


def _read_global_header(f):
    magic = f.read(4)
    if len(magic) != 4:
        raise PcapError("truncated pcap global header (missing magic)")
    try:
        endian, frac_scale = _MAGIC_TABLE[magic]
    except KeyError:
        raise PcapError("not a classic pcap file (unrecognized magic {!r})".format(magic))
    rest = f.read(_GLOBAL_HEADER_REST_LEN)
    if len(rest) != _GLOBAL_HEADER_REST_LEN:
        raise PcapError("truncated pcap global header")
    _ver_maj, _ver_min, _tz, _sigfigs, _snaplen, linktype = struct.unpack(
        endian + "HHiIII", rest
    )
    return endian, frac_scale, linktype


def _iter_raw_frames(f, endian, frac_scale):
    """Yield (timestamp_seconds, frame_bytes) for every capture record."""
    rec_struct = struct.Struct(endian + "IIII")
    read = f.read
    unpack = rec_struct.unpack
    rec_size = rec_struct.size
    while True:
        rec = read(rec_size)
        if not rec:
            return
        if len(rec) != rec_size:
            raise PcapError("truncated pcap record header")
        ts_sec, ts_frac, incl_len, _orig_len = unpack(rec)
        data = read(incl_len)
        if len(data) != incl_len:
            raise PcapError("truncated pcap record data")
        timestamp = ts_sec + (ts_frac * frac_scale) / 1e9
        yield timestamp, data


def _strip_ethernet(data):
    """Return (ethertype, payload_offset) after MAC addrs + any VLAN tags."""
    if len(data) < 14:
        return None, None
    ethertype = struct.unpack(">H", data[12:14])[0]
    offset = 14
    hops = 0
    while ethertype in _VLAN_ETHERTYPES and hops < _MAX_VLAN_TAGS:
        if len(data) < offset + 4:
            return None, None
        ethertype = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        offset += 4
        hops += 1
    return ethertype, offset


def _strip_sll(data):
    """Return (protocol_type, payload_offset) for a Linux cooked frame."""
    if len(data) < 16:
        return None, None
    protocol_type = struct.unpack(">H", data[14:16])[0]
    return protocol_type, 16


def _parse_ipv4_and_transport(ip_data):
    """Parse an IPv4 header + TCP/UDP header from ``ip_data``.

    Returns (proto, src_ip, dst_ip, sport, dport, payload) or None if this
    is not a TCP/UDP-over-IPv4 packet (or the data is too short to parse).
    """
    if len(ip_data) < 20:
        return None
    ver_ihl = ip_data[0]
    version = ver_ihl >> 4
    if version != 4:
        return None
    ihl = (ver_ihl & 0x0F) * 4
    if ihl < 20 or len(ip_data) < ihl:
        return None
    total_len = struct.unpack(">H", ip_data[2:4])[0]
    ip_proto = ip_data[9]
    src_ip = "{}.{}.{}.{}".format(*ip_data[12:16])
    dst_ip = "{}.{}.{}.{}".format(*ip_data[16:20])
    # Bound by both the IP total_length (excludes any link-layer padding)
    # and what was actually captured, in case of a short/truncated frame.
    ip_end = min(len(ip_data), total_len) if total_len >= ihl else len(ip_data)
    transport = ip_data[ihl:ip_end]

    if ip_proto == _IP_PROTO_UDP:
        if len(transport) < 8:
            return None
        sport, dport, udp_len = struct.unpack(">HHH", transport[0:6])
        payload = transport[8:udp_len] if udp_len >= 8 else transport[8:]
        return PROTO_UDP, src_ip, dst_ip, sport, dport, bytes(payload)

    if ip_proto == _IP_PROTO_TCP:
        if len(transport) < 20:
            return None
        sport, dport = struct.unpack(">HH", transport[0:4])
        seq = struct.unpack(">I", transport[4:8])[0]
        ack = struct.unpack(">I", transport[8:12])[0]
        offset_flags = struct.unpack(">H", transport[12:14])[0]
        data_offset = (offset_flags >> 12) * 4
        flags = offset_flags & 0x3F
        if data_offset < 20 or len(transport) < data_offset:
            return None
        payload = transport[data_offset:]
        return (
            PROTO_TCP,
            src_ip,
            dst_ip,
            sport,
            dport,
            bytes(payload),
            seq,
            ack,
            flags,
        )

    return None


def read_pcap(path):
    """Read a classic pcap file, yielding one record per TCP/UDP-over-IPv4
    frame: ``(timestamp, proto, src_ip, dst_ip, sport, dport, payload)``.

    ``timestamp`` is seconds since the epoch (float). ``proto`` is
    ``"TCP"`` or ``"UDP"``. Non-IPv4 frames, IPv4 frames carrying
    anything other than TCP/UDP, and frames too short to parse are
    silently skipped (matches the tolerant, "clean captures" scope of
    this offline tool per plan section 3.7).

    Handles linktype 1 (Ethernet, with any number of stacked VLAN tags)
    and linktype 113 (Linux cooked capture / SLL). Any other linktype
    raises :class:`PcapError`.
    """
    with open(path, "rb") as f:
        endian, frac_scale, linktype = _read_global_header(f)
        if linktype == LINKTYPE_ETHERNET:
            strip_link = _strip_ethernet
        elif linktype == LINKTYPE_LINUX_SLL:
            strip_link = _strip_sll
        else:
            raise PcapError(
                "unsupported pcap linktype {} (expected {} Ethernet or {} SLL)".format(
                    linktype, LINKTYPE_ETHERNET, LINKTYPE_LINUX_SLL
                )
            )

        for timestamp, frame in _iter_raw_frames(f, endian, frac_scale):
            ethertype, offset = strip_link(frame)
            if ethertype != _ETHERTYPE_IPV4:
                continue
            parsed = _parse_ipv4_and_transport(frame[offset:])
            if parsed is None:
                continue
            if parsed[0] == PROTO_UDP:
                proto, src_ip, dst_ip, sport, dport, payload = parsed
                yield (timestamp, proto, src_ip, dst_ip, sport, dport, payload)
            else:
                proto, src_ip, dst_ip, sport, dport, payload, _seq, _ack, _flags = parsed
                yield (timestamp, proto, src_ip, dst_ip, sport, dport, payload)


# --- MoldUDP64 unwrap (offline fixture extraction only) -------------------

_MOLD_HEADER_LEN = 20
_MOLD_SESSION_LEN = 10
_MOLD_SEQUENCE_LEN = 8

MOLD_HEARTBEAT_COUNT = 0
MOLD_END_OF_SESSION_COUNT = 0xFFFF


class MoldHeader(object):
    """MoldUDP64 downstream packet header (session, sequence, count).

    ``sequence`` is the sequence number of the *first* message in this
    packet's block (each subsequent block message is sequence+1, +2, ...).
    ``count`` is the number of message blocks that follow; 0 means this
    packet is a heartbeat (no blocks), 0xFFFF marks end of session.
    """
    __slots__ = ("session", "sequence", "count")

    def __init__(self, session, sequence, count):
        self.session = session
        self.sequence = sequence
        self.count = count

    def __repr__(self):
        return "MoldHeader(session={!r}, sequence={!r}, count={!r})".format(
            self.session, self.sequence, self.count
        )

    def __eq__(self, other):
        if not isinstance(other, MoldHeader):
            return NotImplemented
        return (
            self.session == other.session
            and self.sequence == other.sequence
            and self.count == other.count
        )

    def is_heartbeat(self):
        return self.count == MOLD_HEARTBEAT_COUNT

    def is_end_of_session(self):
        return self.count == MOLD_END_OF_SESSION_COUNT


def parse_mold_header(payload):
    """Parse the 20-byte MoldUDP64 header from a UDP payload."""
    if len(payload) < _MOLD_HEADER_LEN:
        raise ValueError(
            "MoldUDP64 payload too short for header: {} < {}".format(
                len(payload), _MOLD_HEADER_LEN
            )
        )
    session = payload[0:_MOLD_SESSION_LEN].decode("ascii").strip()
    sequence = int.from_bytes(
        payload[_MOLD_SESSION_LEN:_MOLD_SESSION_LEN + _MOLD_SEQUENCE_LEN], "big"
    )
    count = int.from_bytes(
        payload[_MOLD_SESSION_LEN + _MOLD_SEQUENCE_LEN:_MOLD_HEADER_LEN], "big"
    )
    return MoldHeader(session=session, sequence=sequence, count=count)


def iter_mold_messages(payload):
    """Unwrap the message blocks of one MoldUDP64 UDP payload.

    Yields ``(seq, message_bytes)`` pairs. Heartbeat packets (count 0)
    and end-of-session packets (count 0xFFFF) yield nothing.
    """
    header = parse_mold_header(payload)
    if header.count in (MOLD_HEARTBEAT_COUNT, MOLD_END_OF_SESSION_COUNT):
        return
    offset = _MOLD_HEADER_LEN
    seq = header.sequence
    for _ in range(header.count):
        if offset + 2 > len(payload):
            raise ValueError("truncated MoldUDP64 message block (missing length)")
        msg_len = int.from_bytes(payload[offset:offset + 2], "big")
        offset += 2
        message = payload[offset:offset + msg_len]
        if len(message) != msg_len:
            raise ValueError("truncated MoldUDP64 message block (short body)")
        offset += msg_len
        yield seq, message
        seq += 1


# --- minimal in-order TCP reassembly (clean captures only) ----------------

def reassemble_tcp_streams(path):
    """Reassemble TCP payload bytes per direction from a pcap capture.

    Per plan section 3.7: "TCP captures need minimal in-order
    reassembly (clean captures — per-direction seq splice suffices)."
    A segment is accepted only if its TCP sequence number equals the
    next expected sequence number for its direction; an older segment
    (a retransmit already accounted for) is dropped. A segment that
    arrives *ahead* of the expected sequence indicates packet loss in
    the capture, which the samples this tool targets do not have, so
    that case raises :class:`PcapError` rather than silently producing
    a corrupt stream.

    Returns an ``OrderedDict`` keyed by
    ``(src_ip, sport, dst_ip, dport)`` (the direction the segment
    travelled) mapping to the reassembled payload bytes for that
    direction, in first-seen order.
    """
    with open(path, "rb") as f:
        endian, frac_scale, linktype = _read_global_header(f)
        if linktype == LINKTYPE_ETHERNET:
            strip_link = _strip_ethernet
        elif linktype == LINKTYPE_LINUX_SLL:
            strip_link = _strip_sll
        else:
            raise PcapError(
                "unsupported pcap linktype {} (expected {} Ethernet or {} SLL)".format(
                    linktype, LINKTYPE_ETHERNET, LINKTYPE_LINUX_SLL
                )
            )

        expected_seq = {}
        streams = OrderedDict()

        for _timestamp, frame in _iter_raw_frames(f, endian, frac_scale):
            ethertype, offset = strip_link(frame)
            if ethertype != _ETHERTYPE_IPV4:
                continue
            parsed = _parse_ipv4_and_transport(frame[offset:])
            if parsed is None or parsed[0] != PROTO_TCP:
                continue
            _proto, src_ip, dst_ip, sport, dport, payload, seq, _ack, flags = parsed

            key = (src_ip, sport, dst_ip, dport)
            consumed = len(payload)
            if flags & _TCP_FLAG_SYN:
                consumed += 1
            if flags & _TCP_FLAG_FIN:
                consumed += 1

            if key not in expected_seq:
                # First segment seen for this direction: synchronize here.
                expected_seq[key] = seq
                streams[key] = bytearray()

            next_expected = expected_seq[key]
            delta = (seq - next_expected) & 0xFFFFFFFF
            if delta == 0:
                if payload:
                    streams[key].extend(payload)
                expected_seq[key] = (next_expected + consumed) & 0xFFFFFFFF
            elif delta < 0x80000000:
                # seq is ahead of what we expect: a gap in a supposedly
                # clean capture.
                raise PcapError(
                    "TCP sequence gap on {}: expected {}, got {}".format(
                        key, next_expected, seq
                    )
                )
            # else: seq is behind expected (older/duplicate segment) -> drop.

        return OrderedDict((k, bytes(v)) for k, v in streams.items())
