"""ITCH binary codec: decode/encode a single wire message.

Driven entirely by the declarative tables in schema.py — one
`struct.Struct` is precompiled per message type at import time. This
module is zero-policy (plan section 1): it turns exactly one message's
worth of bytes into a NamedTuple and back, nothing else. No filtering,
no book logic, no knowledge of SoupBinTCP framing.

decode(buf) accepts bytes covering *exactly* one message (the caller —
SoupBinTCP framing, T2.4 — is responsible for delimiting messages).
encode(msg) is the exact inverse: `decode(encode(m)) == m` for every
message type (T2.3 acceptance).
"""
import struct
from collections import OrderedDict

from jnxfeed.itch import messages, schema


class DecodeError(Exception):
    """Base class for all ITCH decode/encode errors."""


class UnknownMessageType(DecodeError):
    """The first byte of a buffer doesn't match any known ITCH message type."""


class InvalidMessageLength(DecodeError):
    """A buffer's length doesn't match its message type's fixed wire length.

    Covers both truncated (too short) and overlong (too long) input —
    decode() requires a buffer covering exactly one message.
    """


class EncodeError(Exception):
    """Base class for all ITCH encode errors."""


class UnknownMessageClass(EncodeError):
    """encode() was given something that isn't a known ITCH NamedTuple."""


class AlphaFieldTooLong(EncodeError):
    """An alpha field's string value doesn't fit in its wire width."""


# --- struct.Struct compilation -------------------------------------------

# Big-endian unsigned integer format codes for the num/price field sizes
# that appear in schema.py (plan section 3.1: sizes 1/2/4/8).
_NUM_STRUCT_CODES = {1: "B", 2: "H", 4: "I", 8: "Q"}


def _field_struct_code(msg_type, field):
    name, size, ftype = field
    if ftype == schema.ALPHA:
        return "{}s".format(size)
    try:
        return _NUM_STRUCT_CODES[size]
    except KeyError:
        raise ValueError(
            "{}.{}: unsupported field size {} bytes".format(msg_type, name, size)
        )


def _struct_format(msg_type):
    codes = [_field_struct_code(msg_type, f) for f in schema.SCHEMAS[msg_type]]
    return ">" + "".join(codes)


#: Message type char -> precompiled struct.Struct for the fields *after*
#: the 1-byte message type (the type byte itself is handled separately).
STRUCTS = OrderedDict(
    (msg_type, struct.Struct(_struct_format(msg_type)))
    for msg_type in schema.MESSAGE_TYPES
)

# Self-check at import time: struct size (+1 for the type byte) must
# match the plan's [len] column exactly. A mismatch here is a bug in
# schema.py, not bad input, so it's not wrapped in DecodeError.
for _msg_type in schema.MESSAGE_TYPES:
    _expected = schema.total_length(_msg_type)
    _actual = STRUCTS[_msg_type].size + 1
    if _actual != _expected:
        raise AssertionError(
            "{}: struct size {} + 1 != schema total_length {}".format(
                _msg_type, STRUCTS[_msg_type].size, _expected
            )
        )
del _msg_type, _expected, _actual

#: Message type char -> NamedTuple class -> type char, for encode().
_CLASS_TO_TYPE = OrderedDict(
    (cls, msg_type) for msg_type, cls in messages.MESSAGE_CLASSES.items()
)


# --- decode ----------------------------------------------------------------

def decode(buf):
    """Decode exactly one ITCH wire message from `buf` (bytes-like).

    `buf` must cover exactly one message: 1 type byte + that type's
    fixed payload, no more, no less. Returns the matching NamedTuple
    from jnxfeed.itch.messages.

    Raises UnknownMessageType if the first byte isn't a known ITCH
    message type, or InvalidMessageLength if `buf`'s length doesn't
    match that type's fixed wire length (this is how truncated input is
    reported).
    """
    if len(buf) < 1:
        raise InvalidMessageLength("empty buffer: need at least 1 byte")

    type_byte = buf[0]
    # bytes[0] is already an int on Python 3; memoryview/bytearray too.
    msg_type = chr(type_byte)

    st = STRUCTS.get(msg_type)
    if st is None:
        raise UnknownMessageType(
            "unknown ITCH message type byte: {!r}".format(msg_type)
        )

    expected_len = st.size + 1
    if len(buf) != expected_len:
        raise InvalidMessageLength(
            "{}: expected {} bytes, got {}".format(msg_type, expected_len, len(buf))
        )

    raw_values = st.unpack(bytes(buf[1:]))
    fields = schema.SCHEMAS[msg_type]
    cls = messages.MESSAGE_CLASSES[msg_type]

    values = []
    for (name, size, ftype), raw_value in zip(fields, raw_values):
        if ftype == schema.ALPHA:
            values.append(_decode_alpha(msg_type, name, raw_value))
        else:
            values.append(raw_value)
    return cls(*values)


def _decode_alpha(msg_type, name, raw_bytes):
    try:
        text = raw_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise DecodeError(
            "{}.{}: alpha field is not ASCII: {!r}".format(msg_type, name, raw_bytes)
        )
    # Left-justified, right-padded with spaces on the wire (plan 3.1);
    # strip trailing spaces only — leading/internal spaces are data.
    return text.rstrip(" ")


# --- encode ------------------------------------------------------------

def encode(msg):
    """Encode a NamedTuple from jnxfeed.itch.messages to its wire bytes.

    Alpha fields are re-padded with trailing spaces to their schema
    width. Inverse of decode(): `decode(encode(m)) == m` for every
    message type.
    """
    msg_type = _CLASS_TO_TYPE.get(type(msg))
    if msg_type is None:
        raise UnknownMessageClass(
            "not a known ITCH message type: {!r}".format(type(msg))
        )

    st = STRUCTS[msg_type]
    fields = schema.SCHEMAS[msg_type]

    raw_values = []
    for (name, size, ftype), value in zip(fields, msg):
        if ftype == schema.ALPHA:
            raw_values.append(_encode_alpha(msg_type, name, size, value))
        else:
            raw_values.append(value)

    return msg_type.encode("ascii") + st.pack(*raw_values)


def _encode_alpha(msg_type, name, size, value):
    data = value.encode("ascii")
    if len(data) > size:
        raise AlphaFieldTooLong(
            "{}.{}: {!r} is {} bytes, field width is {}".format(
                msg_type, name, value, len(data), size
            )
        )
    return data + b" " * (size - len(data))
