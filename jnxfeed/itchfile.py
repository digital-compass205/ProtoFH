"""ITCH Binary Data File I/O (JNX_PLAN.md section 3.7 / Data File Formats
spec section 10) — offline tooling only, not part of the live transport.

The format is Japannext's SFTP full-day file format and this project's
native fixture/replay/capture format: a flat stream of
``length:2:num`` (big-endian) + that many bytes of one ITCH message,
repeated to end of file. No other framing, no header/trailer.

Reading and writing are both streaming (constant memory regardless of
file size): :func:`iter_messages` is a generator over an already-open
binary file object, and :class:`ItchFileWriter` writes/flushes message
by message, including an append mode for resuming a capture.
"""
import struct

_LEN_STRUCT = struct.Struct(">H")

#: Largest message length the 2-byte length prefix can express.
MAX_MESSAGE_LEN = 0xFFFF


class ItchFileError(Exception):
    """Raised for malformed/truncated ITCH Binary Data File content."""


def write_message(f, message):
    """Write one length-prefixed message to an open binary file object."""
    length = len(message)
    if length > MAX_MESSAGE_LEN:
        raise ValueError(
            "message of {} bytes exceeds max length {}".format(length, MAX_MESSAGE_LEN)
        )
    f.write(_LEN_STRUCT.pack(length))
    f.write(message)


def iter_messages(f):
    """Generator yielding each message's raw bytes from an open binary
    file object, in file order, until EOF.

    Raises :class:`ItchFileError` on a truncated length prefix or a
    message body shorter than its declared length (a corrupt/partial
    file), but a clean EOF exactly on a message boundary ends the
    generator normally.
    """
    read = f.read
    while True:
        header = read(2)
        if not header:
            return
        if len(header) != 2:
            raise ItchFileError("truncated length prefix at end of file")
        length = _LEN_STRUCT.unpack(header)[0]
        message = read(length)
        if len(message) != length:
            raise ItchFileError(
                "truncated message body: expected {} bytes, got {}".format(
                    length, len(message)
                )
            )
        yield message


def read_file(path):
    """Generator yielding every message's raw bytes from the ``.itch``
    file at ``path``. Opens and closes the file itself.
    """
    with open(path, "rb") as f:
        for message in iter_messages(f):
            yield message


class ItchFileWriter(object):
    """Streaming writer for the ITCH Binary Data File format.

    Usable as a context manager::

        with ItchFileWriter("out.itch") as w:
            w.write(message_bytes)

    Pass ``append=True`` to open an existing file in append mode and
    continue writing to it (e.g. resuming a capture after a
    disconnect) rather than truncating it.
    """

    def __init__(self, path, append=False):
        mode = "ab" if append else "wb"
        self._f = open(path, mode)

    def write(self, message):
        write_message(self._f, message)

    def flush(self):
        self._f.flush()

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False
