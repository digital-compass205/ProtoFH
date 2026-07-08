"""Tests for jnxfeed.itchfile (JNX_PLAN.md T3.1 / Data File Formats sec 10)."""
import pytest

from jnxfeed import itchfile


SYNTHETIC_MESSAGES = [
    b"T\x00\x00\x00\x05",
    b"S\x00\x00\x00\x06DAY O",
    b"A" + b"\x00" * 28,
    b"",  # a zero-length message must round-trip too
    b"\xff" * 300,  # exceeds a single struct read chunk boundary
]


def test_write_message_and_iter_messages_roundtrip(tmp_path):
    path = tmp_path / "sample.itch"
    with open(str(path), "wb") as f:
        for msg in SYNTHETIC_MESSAGES:
            itchfile.write_message(f, msg)

    with open(str(path), "rb") as f:
        decoded = list(itchfile.iter_messages(f))
    assert decoded == SYNTHETIC_MESSAGES


def test_read_file_roundtrip(tmp_path):
    path = tmp_path / "sample.itch"
    with open(str(path), "wb") as f:
        for msg in SYNTHETIC_MESSAGES:
            itchfile.write_message(f, msg)

    assert list(itchfile.read_file(str(path))) == SYNTHETIC_MESSAGES


def test_itch_file_writer_context_manager_roundtrip(tmp_path):
    path = tmp_path / "sample.itch"
    with itchfile.ItchFileWriter(str(path)) as w:
        for msg in SYNTHETIC_MESSAGES:
            w.write(msg)

    assert list(itchfile.read_file(str(path))) == SYNTHETIC_MESSAGES


def test_itch_file_writer_append_support(tmp_path):
    path = tmp_path / "sample.itch"
    first_batch = SYNTHETIC_MESSAGES[:2]
    second_batch = SYNTHETIC_MESSAGES[2:]

    with itchfile.ItchFileWriter(str(path)) as w:
        for msg in first_batch:
            w.write(msg)

    # Simulate resuming a capture after a disconnect.
    with itchfile.ItchFileWriter(str(path), append=True) as w:
        for msg in second_batch:
            w.write(msg)

    assert list(itchfile.read_file(str(path))) == SYNTHETIC_MESSAGES


def test_iter_messages_streaming_generator_is_lazy(tmp_path):
    path = tmp_path / "sample.itch"
    with itchfile.ItchFileWriter(str(path)) as w:
        for msg in SYNTHETIC_MESSAGES:
            w.write(msg)

    with open(str(path), "rb") as f:
        gen = itchfile.iter_messages(f)
        first = next(gen)
        assert first == SYNTHETIC_MESSAGES[0]
        rest = list(gen)
        assert rest == SYNTHETIC_MESSAGES[1:]


def test_iter_messages_empty_file_yields_nothing(tmp_path):
    path = tmp_path / "empty.itch"
    path.write_bytes(b"")
    with open(str(path), "rb") as f:
        assert list(itchfile.iter_messages(f)) == []


def test_iter_messages_truncated_length_prefix_raises(tmp_path):
    path = tmp_path / "bad.itch"
    path.write_bytes(b"\x00")  # only 1 of 2 length-prefix bytes
    with open(str(path), "rb") as f:
        with pytest.raises(itchfile.ItchFileError):
            list(itchfile.iter_messages(f))


def test_iter_messages_truncated_body_raises(tmp_path):
    path = tmp_path / "bad.itch"
    # Declares a 10-byte message but only provides 3.
    path.write_bytes(b"\x00\x0aABC")
    with open(str(path), "rb") as f:
        with pytest.raises(itchfile.ItchFileError):
            list(itchfile.iter_messages(f))


def test_write_message_rejects_oversized_message(tmp_path):
    path = tmp_path / "sample.itch"
    with open(str(path), "wb") as f:
        with pytest.raises(ValueError):
            itchfile.write_message(f, b"\x00" * (itchfile.MAX_MESSAGE_LEN + 1))


def test_length_prefix_is_big_endian(tmp_path):
    path = tmp_path / "sample.itch"
    with open(str(path), "wb") as f:
        itchfile.write_message(f, b"ABCDE")  # length 5
    raw = path.read_bytes()
    assert raw[0:2] == b"\x00\x05"
    assert raw[2:7] == b"ABCDE"
