"""The protobuf wire reader."""

from __future__ import annotations

import pytest

from wake import protowire as pw


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 2**31, 2**63 - 1, 2**64 - 1])
def test_varints_round_trip(value):
    encoded = pw.encode_varint(value)
    decoded, end = pw.read_varint(encoded, 0)
    assert decoded == value and end == len(encoded)


def test_a_negative_int64_is_ten_bytes_and_comes_back_negative():
    encoded = pw.encode_varint(-5)
    assert len(encoded) == 10
    raw, _ = pw.read_varint(encoded, 0)
    assert pw.as_int64(raw) == -5


def test_fields_are_read_in_order_with_their_types():
    message = pw.field_varint(1, 150) + pw.field_bytes(2, b"hi") + pw.field_fixed64(3, 2**40)
    got = [(n, t, bytes(v) if isinstance(v, memoryview) else v) for n, t, v in pw.fields(message)]
    assert got == [(1, pw.VARINT, 150), (2, pw.LENGTH, b"hi"), (3, pw.FIXED64, 2**40)]


def test_the_known_byte_sequence_from_the_protobuf_docs():
    """Field 1, varint 150, is 08 96 01 on the wire."""
    assert list(pw.fields(bytes([0x08, 0x96, 0x01]))) == [(1, 0, 150)]


def test_nested_messages_are_views_not_copies():
    inner = pw.field_varint(1, 7)
    outer = pw.field_bytes(5, inner)
    ((number, _wire, view),) = list(pw.fields(outer))
    assert number == 5 and isinstance(view, memoryview)
    assert list(pw.fields(view)) == [(1, 0, 7)]


def test_unknown_fields_are_skippable():
    """A newer schema adds fields. An older reader must step over them."""
    message = pw.field_bytes(99, b"from the future") + pw.field_varint(1, 42)
    numbers = [n for n, _t, _v in pw.fields(message)]
    assert numbers == [99, 1]


@pytest.mark.parametrize("raw,message", [
    (bytes([0x08]), "varint runs past"),
    (bytes([0x12, 0x05, 0x61]), "claims 5 bytes"),
    (bytes([0x19, 0x00, 0x00]), "fixed64 field 3 runs past"),
    (bytes([0x0B]), "unsupported wire type 3"),
    (bytes([0x00, 0x01]), "field number 0"),
    (bytes([0x08] + [0xFF] * 11), "longer than ten bytes"),
])
def test_malformed_input_is_refused_with_a_reason(raw, message):
    with pytest.raises(pw.WireError, match=message):
        list(pw.fields(raw))


def test_doubles_decode_from_their_fixed64_bits():
    import struct
    bits = struct.unpack("<Q", struct.pack("<d", 3.25))[0]
    assert pw.as_double(bits) == 3.25
