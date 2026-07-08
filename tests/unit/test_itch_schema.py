"""T2.1 acceptance tests: schema lengths vs. plan section 3.2, and
NamedTuple fields matching schema order."""
from jnxfeed.itch import messages, schema


def test_all_twelve_message_types_present():
    expected = ("T", "S", "L", "R", "H", "Y", "A", "F", "E", "D", "U", "G")
    assert schema.MESSAGE_TYPES == expected
    assert tuple(messages.MESSAGE_CLASSES.keys()) == expected


def test_schema_lengths_match_plan_table():
    # Plan section 3.2 `[len]` column, byte-verified against the official
    # UDP sample for A/E/D/U/T/S/H/Y.
    expected_lengths = {
        "T": 5, "S": 10, "L": 17, "R": 45, "H": 14, "Y": 14,
        "A": 30, "F": 35, "E": 25, "D": 13, "U": 29, "G": 9,
    }
    assert expected_lengths == schema.LENGTHS
    for msg_type, expected_len in expected_lengths.items():
        assert schema.total_length(msg_type) == expected_len, msg_type


def test_named_tuple_fields_match_schema_order():
    for msg_type in schema.MESSAGE_TYPES:
        cls = messages.MESSAGE_CLASSES[msg_type]
        assert cls._fields == schema.field_names(msg_type), msg_type


def test_field_sizes_are_struct_representable():
    valid_num_sizes = (1, 2, 4, 8)
    for msg_type, fields in schema.SCHEMAS.items():
        for name, size, ftype in fields:
            assert ftype in schema.FIELD_TYPES, (msg_type, name)
            if ftype in (schema.NUM, schema.PRICE):
                assert size in valid_num_sizes, (msg_type, name, size)
            if ftype == schema.PRICE:
                assert size == 4, (msg_type, name, "price fields are 4 bytes")
            assert size >= 1, (msg_type, name)


def test_orderbook_id_is_alpha_not_num():
    # Spec v1.7 change (plan section 3.1): Orderbook Id is a 4-byte Alpha
    # (SICC code), not an integer.
    for msg_type, fields in schema.SCHEMAS.items():
        by_name = {name: (size, ftype) for name, size, ftype in fields}
        if "orderbook_id" in by_name:
            size, ftype = by_name["orderbook_id"]
            assert (size, ftype) == (4, schema.ALPHA), msg_type
