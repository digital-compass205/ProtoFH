"""Human-readable one-liners for raw ITCH messages (shared by the T3.3
probe and the T7.1 CLI views)."""
from jnxfeed import types
from jnxfeed.itch import codec as itch_codec
from jnxfeed.itch import messages as itch_messages
from jnxfeed.itch import schema as itch_schema

#: Names of all price-typed fields across every schema, for rendering.
_PRICE_FIELDS = frozenset(
    name
    for fields in itch_schema.SCHEMAS.values()
    for (name, _size, ftype) in fields
    if ftype == itch_schema.PRICE
)

#: Message class -> ITCH type char.
_TYPE_CHARS = dict(
    (cls, char) for char, cls in itch_messages.MESSAGE_CLASSES.items()
)


def describe_msg(msg, type_char=None):
    """One human-readable line for a decoded ITCH message NamedTuple."""
    parts = []
    for name, value in zip(msg._fields, msg):
        if name in _PRICE_FIELDS:
            value = types.price_to_str(value)
        parts.append("{}={}".format(name, value))
    if type_char is None:
        type_char = _TYPE_CHARS.get(type(msg), "?")
    return "{} {} {}".format(type_char, type(msg).__name__, " ".join(parts))


def describe_itch(payload):
    """One human-readable line for one raw ITCH message."""
    try:
        msg = itch_codec.decode(payload)
    except itch_codec.DecodeError as exc:
        return "?? undecodable ({}): {}".format(exc, payload.hex())
    return describe_msg(msg, chr(payload[0]))
