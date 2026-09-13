"""A protobuf wire-format reader, written out by hand.

OpenTelemetry exporters send protobuf by default. Accepting that without a
dependency means reading the wire format directly, and the format is small
enough to do it honestly:

    each field is a key, then a payload
    key      = varint of (field_number << 3 | wire_type)
    type 0   varint            ints, bools, enums
    type 1   8 bytes           fixed64, sfixed64, double
    type 2   length-prefixed   strings, bytes, nested messages, packed repeats
    type 5   4 bytes           fixed32, sfixed32, float

Types 3 and 4, the long-deprecated groups, are refused. Unknown field numbers are
skipped rather than rejected, which is what lets an old decoder read a message
from a newer schema; the OTLP definition has added fields over the years and
will again.

This file knows nothing about spans. `otlp.py` says which field means what.
"""

from __future__ import annotations

import struct
from typing import Iterator

VARINT, FIXED64, LENGTH, START_GROUP, END_GROUP, FIXED32 = 0, 1, 2, 3, 4, 5

_DOUBLE = struct.Struct("<d")
_FIXED64 = struct.Struct("<Q")
_FIXED32 = struct.Struct("<I")


class WireError(ValueError):
    """The bytes are not a well-formed protobuf message."""


def read_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WireError("varint runs past the end of the message")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise WireError("varint longer than ten bytes")


def fields(buf: bytes | memoryview) -> Iterator[tuple[int, int, object]]:
    """Yield (field_number, wire_type, value) for every field, in order.

    Length-delimited payloads come back as memoryview slices, so walking a
    nested message never copies it. The caller decides whether a given slice is
    a string, raw bytes, or another message to walk.
    """
    view = memoryview(buf)
    pos = 0
    end = len(view)
    while pos < end:
        key, pos = read_varint(view, pos)
        number, wire_type = key >> 3, key & 0x7
        if number == 0:
            raise WireError("field number 0 is not allowed")

        if wire_type == VARINT:
            value, pos = read_varint(view, pos)
        elif wire_type == FIXED64:
            if pos + 8 > end:
                raise WireError(f"fixed64 field {number} runs past the end")
            value = _FIXED64.unpack_from(view, pos)[0]
            pos += 8
        elif wire_type == LENGTH:
            length, pos = read_varint(view, pos)
            if pos + length > end:
                raise WireError(
                    f"field {number} claims {length} bytes but only {end - pos} remain")
            value = view[pos:pos + length]
            pos += length
        elif wire_type == FIXED32:
            if pos + 4 > end:
                raise WireError(f"fixed32 field {number} runs past the end")
            value = _FIXED32.unpack_from(view, pos)[0]
            pos += 4
        else:
            raise WireError(f"unsupported wire type {wire_type} on field {number}")
        yield number, wire_type, value


def as_str(value: memoryview) -> str:
    return bytes(value).decode("utf-8")


def as_double(raw_fixed64: int) -> float:
    return _DOUBLE.unpack(_FIXED64.pack(raw_fixed64))[0]


def as_int64(raw_varint: int) -> int:
    """Protobuf int64 carries negatives as ten-byte two's complement varints."""
    return raw_varint - (1 << 64) if raw_varint >= 1 << 63 else raw_varint


# ------------------------------------------------------------------ encoding
#
# Only what the test fixtures and the benchmark need to build messages without
# reaching for the protobuf library. Real ingest never encodes.

def encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint(number << 3 | VARINT) + encode_varint(value)


def field_bytes(number: int, payload: bytes) -> bytes:
    return encode_varint(number << 3 | LENGTH) + encode_varint(len(payload)) + payload


def field_fixed64(number: int, value: int) -> bytes:
    return encode_varint(number << 3 | FIXED64) + _FIXED64.pack(value)
