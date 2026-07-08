"""Fixture extraction from the official Japannext sample captures (T3.2).

Offline tooling: converts the official MoldUDP64 UDP sample pcap into an
ITCH Binary Data File (``.itch``, see jnxfeed.itchfile) plus a small
sliced fixture for fast unit tests, and writes a JSON golden manifest
covering all three official samples (UDP, TCP/Soup, GLIMPSE).

Every ITCH message encountered is decoded through
``jnxfeed.itch.codec.decode`` as it is scanned — extraction doubles as a
full decode validation pass, so a single decode error anywhere in a
sample aborts the run. The golden tests
(tests/unit/test_golden_samples.py) call the same scan functions and
assert the resulting manifests against the committed golden JSON and the
pre-verified numbers in JNX_PLAN.md.

Usage::

    python -m jnxfeed.cli.fixtures [--specs-dir DIR] [--fixtures-dir DIR]
                                   [--slice-count N]

Writes into the fixtures dir (default tests/fixtures/):

- ``sample_udp.itch``           — full UDP sample (NOT committed to git)
- ``sample_udp_head.itch``      — first N messages (committed)
- ``sample_udp_head.manifest.json`` — mini-manifest for the slice
- ``golden_manifest.json``      — full-sample golden manifest
"""
import argparse
import json
import os
import sys
from collections import OrderedDict

from jnxfeed import itchfile, pcapio
from jnxfeed.itch import codec, messages
from jnxfeed.soup import packets as soup_packets

#: Sample-derived server ports (plan section 3.4) — simulator/sample
#: defaults only, not production configuration.
ITCH_TCP_PORT = 15001
GLIMPSE_TCP_PORT = 15002

DEFAULT_SPECS_DIR = "/workspace/jnx-specs"
UDP_SAMPLE_NAME = "Japannext_PTS_ITCH_Equities_v1.7.UDP.pcap"
TCP_SAMPLE_NAME = "Japannext_PTS_ITCH_Equities_v1.7.TCP.pcap"
GLIMPSE_SAMPLE_NAME = "Japannext_PTS_GLIMPSE_Equities_v1.4.pcap"

FULL_ITCH_NAME = "sample_udp.itch"
SLICE_ITCH_NAME = "sample_udp_head.itch"
SLICE_MANIFEST_NAME = "sample_udp_head.manifest.json"
GOLDEN_MANIFEST_NAME = "golden_manifest.json"

DEFAULT_SLICE_COUNT = 2000

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_FIXTURES_DIR = os.path.join(_REPO_ROOT, "tests", "fixtures")


class SampleError(Exception):
    """Raised when a sample capture violates an expected invariant
    (sequence gap, multiple sessions, missing login, ...)."""


# --- UDP (MoldUDP64) sample ------------------------------------------------

def iter_udp_sample(pcap_path):
    """Yield ``(session, seq, message_bytes)`` for every ITCH message in
    the Mold-wrapped UDP sample pcap, in capture order."""
    for record in pcapio.read_pcap(pcap_path):
        proto = record[1]
        payload = record[6]
        if proto != pcapio.PROTO_UDP:
            continue
        header = pcapio.parse_mold_header(payload)
        for seq, message in pcapio.iter_mold_messages(payload):
            yield header.session, seq, message


def scan_udp_sample(pcap_path, out_path=None, slice_path=None,
                    slice_count=DEFAULT_SLICE_COUNT):
    """Single pass over the UDP sample: decode-validate every message,
    check sequence contiguity and single-session, and optionally write
    the full ``.itch`` file and/or a first-``slice_count``-messages slice.

    Returns ``(manifest, slice_manifest)`` as OrderedDicts;
    ``slice_manifest`` is None when ``slice_path`` is None.
    """
    session = None
    first_seq = None
    last_seq = None
    count = 0
    type_counts = {}
    slice_manifest = None

    writer = itchfile.ItchFileWriter(out_path) if out_path else None
    slice_writer = itchfile.ItchFileWriter(slice_path) if slice_path else None
    slice_counts = {}
    slice_last_seq = None
    try:
        for msg_session, seq, message in iter_udp_sample(pcap_path):
            if session is None:
                session = msg_session
                first_seq = seq
            elif msg_session != session:
                raise SampleError(
                    "multiple Mold sessions in {}: {!r} then {!r}".format(
                        pcap_path, session, msg_session
                    )
                )
            elif seq != last_seq + 1:
                raise SampleError(
                    "sequence gap in {}: expected {}, got {}".format(
                        pcap_path, last_seq + 1, seq
                    )
                )
            last_seq = seq

            msg_type = chr(message[0])
            codec.decode(message)  # zero-tolerance validation pass
            type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
            count += 1

            if writer is not None:
                writer.write(message)
            if slice_writer is not None and count <= slice_count:
                slice_writer.write(message)
                slice_counts[msg_type] = slice_counts.get(msg_type, 0) + 1
                slice_last_seq = seq
    finally:
        if writer is not None:
            writer.close()
        if slice_writer is not None:
            slice_writer.close()

    manifest = OrderedDict([
        ("source", os.path.basename(pcap_path)),
        ("session", session),
        ("first_seq", first_seq),
        ("last_seq", last_seq),
        ("message_count", count),
        ("type_counts", OrderedDict(sorted(type_counts.items()))),
    ])
    if slice_path is not None:
        slice_manifest = OrderedDict([
            ("source", os.path.basename(pcap_path)),
            ("session", session),
            ("first_seq", first_seq),
            ("last_seq", slice_last_seq),
            ("message_count", min(count, slice_count)),
            ("type_counts", OrderedDict(sorted(slice_counts.items()))),
        ])
    return manifest, slice_manifest


# --- TCP (SoupBinTCP) samples -----------------------------------------------

def scan_soup_sample(pcap_path, server_port):
    """Scan the server->client half of a SoupBinTCP sample capture.

    Reassembles the TCP stream flowing *from* ``server_port``, feeds it
    through the Soup FrameBuffer, requires the first packet to be Login
    Accepted, and decode-validates the ITCH message inside every
    Sequenced Data packet. Returns a manifest OrderedDict.
    """
    streams = pcapio.reassemble_tcp_streams(pcap_path)
    server_keys = [key for key in streams if key[1] == server_port]
    if not server_keys:
        raise SampleError(
            "no TCP stream from server port {} in {} (directions: {})".format(
                server_port, pcap_path, list(streams)
            )
        )
    if len(server_keys) > 1:
        raise SampleError(
            "multiple server streams from port {} in {}".format(server_port, pcap_path)
        )

    fb = soup_packets.FrameBuffer()
    packets = fb.feed(streams[server_keys[0]])
    # A capture may stop mid-frame (the official GLIMPSE sample does:
    # the capture ends with a client RST partway through the snapshot).
    # Record the dangling byte count instead of failing — every *framed*
    # packet still gets fully validated.
    trailing_bytes = fb.pending_bytes()
    if not packets or not isinstance(packets[0], soup_packets.LoginAccepted):
        raise SampleError(
            "expected Login Accepted as first server packet in {}, got {!r}".format(
                pcap_path, type(packets[0]).__name__ if packets else None
            )
        )
    login = packets[0]

    count = 0
    type_counts = {}
    heartbeats = 0
    end_of_session = False
    last_msg = None
    for packet in packets[1:]:
        if isinstance(packet, soup_packets.SequencedData):
            msg_type = chr(packet.message[0])
            last_msg = codec.decode(packet.message)
            type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
            count += 1
        elif isinstance(packet, soup_packets.ServerHeartbeat):
            heartbeats += 1
        elif isinstance(packet, soup_packets.EndOfSession):
            end_of_session = True
        elif isinstance(packet, (soup_packets.LoginAccepted,
                                 soup_packets.LoginRejected)):
            raise SampleError(
                "unexpected mid-stream {} in {}".format(
                    type(packet).__name__, pcap_path
                )
            )
        # DebugPacket: ignored per plan section 3.4.

    manifest = OrderedDict([
        ("source", os.path.basename(pcap_path)),
        ("server_port", server_port),
        ("session", login.session),
        ("login_sequence", login.sequence),
        ("message_count", count),
        ("type_counts", OrderedDict(sorted(type_counts.items()))),
        ("server_heartbeats", heartbeats),
        ("end_of_session", end_of_session),
        ("trailing_bytes", trailing_bytes),
        ("last_message_type", type(last_msg).__name__ if last_msg is not None else None),
    ])
    if isinstance(last_msg, messages.EndOfSnapshot):
        manifest["end_of_snapshot_sequence"] = last_msg.sequence_number
    return manifest


# --- manifest + CLI ---------------------------------------------------------

def build_golden_manifest(specs_dir, fixtures_dir=None,
                          slice_count=DEFAULT_SLICE_COUNT,
                          write_itch=True):
    """Scan all three official samples; optionally write the ``.itch``
    outputs and slice manifest into ``fixtures_dir``. Returns the golden
    manifest OrderedDict (udp/tcp/glimpse sections)."""
    udp_pcap = os.path.join(specs_dir, UDP_SAMPLE_NAME)
    tcp_pcap = os.path.join(specs_dir, TCP_SAMPLE_NAME)
    glimpse_pcap = os.path.join(specs_dir, GLIMPSE_SAMPLE_NAME)

    out_path = slice_path = None
    if write_itch:
        if fixtures_dir is None:
            raise ValueError("write_itch=True requires a fixtures_dir")
        if not os.path.isdir(fixtures_dir):
            os.makedirs(fixtures_dir)
        out_path = os.path.join(fixtures_dir, FULL_ITCH_NAME)
        slice_path = os.path.join(fixtures_dir, SLICE_ITCH_NAME)

    udp_manifest, slice_manifest = scan_udp_sample(
        udp_pcap, out_path=out_path, slice_path=slice_path,
        slice_count=slice_count,
    )
    tcp_manifest = scan_soup_sample(tcp_pcap, ITCH_TCP_PORT)
    glimpse_manifest = scan_soup_sample(glimpse_pcap, GLIMPSE_TCP_PORT)

    manifest = OrderedDict([
        ("udp", udp_manifest),
        ("tcp", tcp_manifest),
        ("glimpse", glimpse_manifest),
    ])

    if write_itch:
        _write_json(os.path.join(fixtures_dir, SLICE_MANIFEST_NAME), slice_manifest)
        _write_json(os.path.join(fixtures_dir, GOLDEN_MANIFEST_NAME), manifest)
    return manifest


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m jnxfeed.cli.fixtures",
        description="Extract .itch fixtures + golden manifest from the "
                    "official Japannext sample pcaps.",
    )
    parser.add_argument("--specs-dir", default=DEFAULT_SPECS_DIR,
                        help="directory holding the official sample pcaps "
                             "(default: %(default)s)")
    parser.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES_DIR,
                        help="output directory (default: %(default)s)")
    parser.add_argument("--slice-count", type=int, default=DEFAULT_SLICE_COUNT,
                        help="messages in the sliced fixture (default: %(default)s)")
    args = parser.parse_args(argv)

    manifest = build_golden_manifest(
        args.specs_dir, fixtures_dir=args.fixtures_dir,
        slice_count=args.slice_count,
    )
    json.dump(manifest, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
