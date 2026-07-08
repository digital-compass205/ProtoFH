"""Tests for jnxfeed.pcapio (JNX_PLAN.md T3.1 / section 3.7).

Two layers of coverage:
- Synthetic, hand-built classic-pcap byte streams exercise the parser
  logic (VLAN skipping, SLL, Mold unwrap, TCP reassembly) without
  depending on any external files.
- Real official sample captures in /workspace/jnx-specs (if present)
  are read end-to-end and checked against the pre-verified ground
  truth numbers from JNX_PLAN.md. If the specs directory is missing,
  those tests are skipped with a clear message rather than failing.
"""
import os
import struct
import time

import pytest

from jnxfeed import pcapio
from jnxfeed.soup import packets as sp

SPECS_DIR = "/workspace/jnx-specs"
UDP_SAMPLE = os.path.join(SPECS_DIR, "Japannext_PTS_ITCH_Equities_v1.7.UDP.pcap")
TCP_SAMPLE = os.path.join(SPECS_DIR, "Japannext_PTS_ITCH_Equities_v1.7.TCP.pcap")
GLIMPSE_SAMPLE = os.path.join(SPECS_DIR, "Japannext_PTS_GLIMPSE_Equities_v1.4.pcap")

requires_specs = pytest.mark.skipif(
    not os.path.isdir(SPECS_DIR),
    reason="official sample pcaps not available at {} (offline fixture dir missing)".format(
        SPECS_DIR
    ),
)


# --- synthetic pcap builders --------------------------------------------

_PCAP_MAGIC = struct.pack("<I", 0xA1B2C3D4)  # little-endian fields, microsecond ts


def _global_header(linktype, snaplen=65535):
    return _PCAP_MAGIC + struct.pack("<HHiIII", 2, 4, 0, 0, snaplen, linktype)


def _record(raw, ts_sec=1000, ts_usec=0):
    return struct.pack("<IIII", ts_sec, ts_usec, len(raw), len(raw)) + raw


def _build_pcap(linktype, frames):
    body = _global_header(linktype)
    for raw in frames:
        body += _record(raw)
    return body


def _ipv4_header(src_ip, dst_ip, proto, payload_len, ident=0):
    total_len = 20 + payload_len
    src = bytes(int(x) for x in src_ip.split("."))
    dst = bytes(int(x) for x in dst_ip.split("."))
    return struct.pack(
        ">BBHHHBBH4s4s", 0x45, 0, total_len, ident, 0x4000, 64, proto, 0, src, dst
    )


def _udp_ethernet_frame(vlan_tags, src_ip, dst_ip, sport, dport, payload):
    """vlan_tags: list of (tpid, tci) pairs, outermost first, may be empty."""
    dst_mac = b"\x01\x00\x5e\x42\x01\x02"
    src_mac = b"\x00\x1c\x73\x6a\x72\x63"
    tags = b"".join(struct.pack(">HH", tpid, tci) for tpid, tci in vlan_tags)
    eth = dst_mac + src_mac + tags + struct.pack(">H", 0x0800)
    ip_hdr = _ipv4_header(src_ip, dst_ip, 17, 8 + len(payload))
    udp_hdr = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0)
    return eth + ip_hdr + udp_hdr + payload


def _tcp_sll_frame(src_ip, dst_ip, sport, dport, seq, ack, flags, payload):
    sll = struct.pack(">HHH8sH", 0, 1, 6, b"\x00" * 8, 0x0800)
    ip_hdr = _ipv4_header(src_ip, dst_ip, 6, 20 + len(payload))
    offset_flags = (5 << 12) | flags
    tcp_hdr = struct.pack(">HHIIHHHH", sport, dport, seq, ack, offset_flags, 65535, 0, 0)
    return sll + ip_hdr + tcp_hdr + payload


TCP_SYN = 0x02
TCP_ACK = 0x10
TCP_PSH = 0x08
TCP_FIN = 0x01


# --- Ethernet + VLAN + UDP --------------------------------------------------

def test_read_pcap_ethernet_no_vlan_udp(tmp_path):
    payload = b"hello-mold-payload-not-real-mold"
    frame = _udp_ethernet_frame([], "10.66.0.104", "232.66.1.2", 40632, 11002, payload)
    path = tmp_path / "sample.pcap"
    path.write_bytes(_build_pcap(pcapio.LINKTYPE_ETHERNET, [frame]))

    records = list(pcapio.read_pcap(str(path)))
    assert len(records) == 1
    ts, proto, src_ip, dst_ip, sport, dport, pl = records[0]
    assert proto == pcapio.PROTO_UDP
    assert src_ip == "10.66.0.104"
    assert dst_ip == "232.66.1.2"
    assert sport == 40632
    assert dport == 11002
    assert pl == payload


def test_read_pcap_ethernet_double_vlan_udp():
    """Mirrors the real UDP sample's triple-tagged frames (2x 0x88a8 + 0x8100)."""
    payload = b"\x00" * 20  # placeholder mold-shaped payload
    frame = _udp_ethernet_frame(
        [(0x88A8, 0x0019), (0x88A8, 0x0019), (0x8100, 0x0261)],
        "10.66.0.104",
        "232.66.1.2",
        40632,
        11002,
        payload,
    )
    import io
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pcap", delete=False) as f:
        f.write(_build_pcap(pcapio.LINKTYPE_ETHERNET, [frame]))
        path = f.name
    try:
        records = list(pcapio.read_pcap(path))
        assert len(records) == 1
        _ts, proto, src_ip, dst_ip, sport, dport, pl = records[0]
        assert proto == pcapio.PROTO_UDP
        assert (src_ip, dst_ip, sport, dport) == ("10.66.0.104", "232.66.1.2", 40632, 11002)
        assert pl == payload
    finally:
        os.unlink(path)


def test_read_pcap_skips_non_ip_ethertype(tmp_path):
    # ARP frame (ethertype 0x0806) should be silently skipped.
    dst_mac = b"\xff" * 6
    src_mac = b"\x00" * 6
    frame = dst_mac + src_mac + struct.pack(">H", 0x0806) + b"\x00" * 28
    path = tmp_path / "sample.pcap"
    path.write_bytes(_build_pcap(pcapio.LINKTYPE_ETHERNET, [frame]))
    assert list(pcapio.read_pcap(str(path))) == []


# --- SLL + TCP ---------------------------------------------------------------

def test_read_pcap_sll_tcp(tmp_path):
    payload = sp.encode(sp.LoginRequest("user01", "pw", "", 1))[2:]  # drop soup length prefix
    frame = _tcp_sll_frame(
        "10.70.96.65", "10.66.0.99", 46846, 15001, 1651264418, 2770081333, TCP_PSH | TCP_ACK, payload
    )
    path = tmp_path / "sample.pcap"
    path.write_bytes(_build_pcap(pcapio.LINKTYPE_LINUX_SLL, [frame]))

    records = list(pcapio.read_pcap(str(path)))
    assert len(records) == 1
    _ts, proto, src_ip, dst_ip, sport, dport, pl = records[0]
    assert proto == pcapio.PROTO_TCP
    assert (src_ip, dst_ip, sport, dport) == ("10.70.96.65", "10.66.0.99", 46846, 15001)
    assert pl == payload


# --- MoldUDP64 unwrap ---------------------------------------------------------

def _mold_payload(session, sequence, count, messages=()):
    header = session.encode("ascii").ljust(10)[:10] + sequence.to_bytes(8, "big") + count.to_bytes(2, "big")
    body = b"".join(struct.pack(">H", len(m)) + m for m in messages)
    return header + body


def test_parse_mold_header_and_messages():
    messages = [b"T\x00\x00\x00\x05", b"H\x00\x00\x00\x06DAY T"]
    payload = _mold_payload("1697659284", 12562, len(messages), messages)
    header = pcapio.parse_mold_header(payload)
    assert header.session == "1697659284"
    assert header.sequence == 12562
    assert header.count == 2
    assert not header.is_heartbeat()
    assert not header.is_end_of_session()

    decoded = list(pcapio.iter_mold_messages(payload))
    assert decoded == [(12562, messages[0]), (12563, messages[1])]


def test_mold_heartbeat_yields_no_messages():
    payload = _mold_payload("1697659284", 12562, 0)
    header = pcapio.parse_mold_header(payload)
    assert header.is_heartbeat()
    assert list(pcapio.iter_mold_messages(payload)) == []


def test_mold_end_of_session_yields_no_messages():
    payload = _mold_payload("1697659284", 234752, 0xFFFF)
    header = pcapio.parse_mold_header(payload)
    assert header.is_end_of_session()
    assert list(pcapio.iter_mold_messages(payload)) == []


def test_mold_header_too_short_raises():
    with pytest.raises(ValueError):
        pcapio.parse_mold_header(b"short")


def test_mold_truncated_message_block_raises():
    # Declares count=1 but no message bytes follow.
    payload = "1697659284".encode("ascii") + (12562).to_bytes(8, "big") + (1).to_bytes(2, "big")
    with pytest.raises(ValueError):
        list(pcapio.iter_mold_messages(payload))


# --- TCP reassembly ------------------------------------------------------------

def test_reassemble_tcp_streams_in_order(tmp_path):
    client = ("10.70.96.65", 46846)
    server = ("10.66.0.99", 15001)
    client_isn = 1000
    server_isn = 5000

    frames = [
        # handshake
        _tcp_sll_frame(client[0], server[0], client[1], server[1], client_isn, 0, TCP_SYN, b""),
        _tcp_sll_frame(server[0], client[0], server[1], client[1], server_isn, client_isn + 1, TCP_SYN | TCP_ACK, b""),
        _tcp_sll_frame(client[0], server[0], client[1], server[1], client_isn + 1, server_isn + 1, TCP_ACK, b""),
        # client sends "ABC"
        _tcp_sll_frame(client[0], server[0], client[1], server[1], client_isn + 1, server_isn + 1, TCP_PSH | TCP_ACK, b"ABC"),
        # server ACKs, then sends "XY"
        _tcp_sll_frame(server[0], client[0], server[1], client[1], server_isn + 1, client_isn + 4, TCP_ACK, b""),
        _tcp_sll_frame(server[0], client[0], server[1], client[1], server_isn + 1, client_isn + 4, TCP_PSH | TCP_ACK, b"XY"),
        # a duplicate retransmit of the client's earlier segment -- must be dropped
        _tcp_sll_frame(client[0], server[0], client[1], server[1], client_isn + 1, server_isn + 1, TCP_PSH | TCP_ACK, b"ABC"),
        # client sends more: "DEF"
        _tcp_sll_frame(client[0], server[0], client[1], server[1], client_isn + 4, server_isn + 3, TCP_PSH | TCP_ACK, b"DEF"),
    ]
    path = tmp_path / "sample.pcap"
    path.write_bytes(_build_pcap(pcapio.LINKTYPE_LINUX_SLL, frames))

    streams = pcapio.reassemble_tcp_streams(str(path))
    client_key = (client[0], client[1], server[0], server[1])
    server_key = (server[0], server[1], client[0], client[1])
    assert streams[client_key] == b"ABCDEF"
    assert streams[server_key] == b"XY"


def test_reassemble_tcp_streams_gap_raises(tmp_path):
    frames = [
        _tcp_sll_frame("10.0.0.1", "10.0.0.2", 1111, 2222, 100, 0, TCP_PSH | TCP_ACK, b"AAA"),
        # skips ahead: expected seq 103, but this segment starts at 200
        _tcp_sll_frame("10.0.0.1", "10.0.0.2", 1111, 2222, 200, 0, TCP_PSH | TCP_ACK, b"BBB"),
    ]
    path = tmp_path / "sample.pcap"
    path.write_bytes(_build_pcap(pcapio.LINKTYPE_LINUX_SLL, frames))
    with pytest.raises(pcapio.PcapError):
        pcapio.reassemble_tcp_streams(str(path))


def test_read_pcap_unsupported_linktype_raises(tmp_path):
    path = tmp_path / "sample.pcap"
    path.write_bytes(_global_header(9))  # linktype 9 = PPP, unsupported here
    with pytest.raises(pcapio.PcapError):
        list(pcapio.read_pcap(str(path)))


def test_read_pcap_bad_magic_raises(tmp_path):
    path = tmp_path / "sample.pcap"
    path.write_bytes(b"not-a-pcap-file-------")
    with pytest.raises(pcapio.PcapError):
        list(pcapio.read_pcap(str(path)))


# --- real official samples ----------------------------------------------------

@requires_specs
def test_udp_sample_packet_and_heartbeat_counts():
    """Ground truth from JNX_PLAN.md: 224,754 UDP packets, 8,842 heartbeats."""
    total_udp = 0
    heartbeats = 0
    t0 = time.time()
    for _ts, proto, _src, _dst, _sport, _dport, payload in pcapio.read_pcap(UDP_SAMPLE):
        if proto != pcapio.PROTO_UDP:
            continue
        total_udp += 1
        header = pcapio.parse_mold_header(payload)
        if header.is_heartbeat():
            heartbeats += 1
    elapsed = time.time() - t0
    assert total_udp == 224754
    assert heartbeats == 8842
    # Sanity budget only -- not a strict perf gate, just guards against an
    # accidental O(n^2) regression on a 224k-packet capture.
    assert elapsed < 30, "UDP sample scan took {:.1f}s, expected a fast single pass".format(elapsed)


@requires_specs
def test_udp_sample_session_and_first_sequence():
    """First packet's Mold header carries session/seq ground truth values."""
    for _ts, proto, _src, _dst, _sport, dport, payload in pcapio.read_pcap(UDP_SAMPLE):
        if proto != pcapio.PROTO_UDP:
            continue
        header = pcapio.parse_mold_header(payload)
        assert header.session == "1697659284"
        assert header.sequence == 12562
        assert dport == 11002
        break
    else:
        pytest.fail("no UDP packets found in sample")


@requires_specs
def test_tcp_sample_reassembly_both_directions():
    streams = pcapio.reassemble_tcp_streams(TCP_SAMPLE)
    assert len(streams) == 2
    by_dport = {}
    for (src_ip, sport, dst_ip, dport), data in streams.items():
        by_dport[dport] = data
        assert len(data) > 0, "no payload recovered for direction {}".format(
            (src_ip, sport, dst_ip, dport)
        )
    # Client -> server (dport 15001, the ground-truth ITCH-TCP port) starts
    # with a SoupBinTCP Login Request.
    client_to_server = by_dport[15001]
    fb = sp.FrameBuffer()
    decoded = fb.feed(client_to_server[:200])
    assert decoded, "expected at least one decoded SoupBinTCP packet"
    assert isinstance(decoded[0], sp.LoginRequest)

    # Server -> client starts with a Login Accepted.
    server_to_client = next(data for dport, data in by_dport.items() if dport != 15001)
    fb2 = sp.FrameBuffer()
    decoded2 = fb2.feed(server_to_client[:200])
    assert decoded2
    assert isinstance(decoded2[0], sp.LoginAccepted)


@requires_specs
def test_glimpse_sample_reassembly_both_directions():
    streams = pcapio.reassemble_tcp_streams(GLIMPSE_SAMPLE)
    assert len(streams) == 2
    by_dport = {}
    for (src_ip, sport, dst_ip, dport), data in streams.items():
        by_dport[dport] = data
        assert len(data) > 0

    client_to_server = by_dport[15002]
    fb = sp.FrameBuffer()
    decoded = fb.feed(client_to_server[:200])
    assert decoded
    assert isinstance(decoded[0], sp.LoginRequest)
    # GLIMPSE requires a blank requested session (spec section 3.5).
    assert decoded[0].requested_session == ""

    server_to_client = next(data for dport, data in by_dport.items() if dport != 15002)
    fb2 = sp.FrameBuffer()
    decoded2 = fb2.feed(server_to_client[:200])
    assert decoded2
    assert isinstance(decoded2[0], sp.LoginAccepted)
