"""Spans to OTLP protobuf.

Wake only ever decodes on its ingest path. This exists so the demo traffic and
the benchmark can send genuine OTLP without depending on the OpenTelemetry SDK,
and the test suite checks its output against the SDK's own decoder, so it is not
a private dialect that only Wake understands.
"""

from __future__ import annotations

import struct
from collections import defaultdict

from . import protowire as pw
from .model import Span

_DOUBLE = struct.Struct("<d")


def _any(value) -> bytes:
    if isinstance(value, bool):
        return pw.field_varint(2, int(value))
    if isinstance(value, int):
        return pw.field_varint(3, value)
    if isinstance(value, float):
        return pw.encode_varint(4 << 3 | pw.FIXED64) + _DOUBLE.pack(value)
    if isinstance(value, (list, tuple)):
        return pw.field_bytes(5, b"".join(pw.field_bytes(1, _any(v)) for v in value))
    if isinstance(value, dict):
        return pw.field_bytes(6, b"".join(pw.field_bytes(1, _kv(k, v)) for k, v in value.items()))
    return pw.field_bytes(1, str(value).encode())


def _kv(key: str, value) -> bytes:
    return pw.field_bytes(1, key.encode()) + pw.field_bytes(2, _any(value))


def encode_span(span: Span) -> bytes:
    out = [
        pw.field_bytes(1, bytes.fromhex(span.trace_id)),
        pw.field_bytes(2, bytes.fromhex(span.span_id)),
    ]
    if span.parent_span_id:
        out.append(pw.field_bytes(4, bytes.fromhex(span.parent_span_id)))
    out.append(pw.field_bytes(5, span.name.encode()))
    if span.kind:
        out.append(pw.field_varint(6, span.kind))
    out.append(pw.field_fixed64(7, span.start_ns))
    out.append(pw.field_fixed64(8, span.end_ns))
    out.extend(pw.field_bytes(9, _kv(k, v)) for k, v in span.attributes.items())
    for event in span.events:
        body = pw.field_fixed64(1, event.time_ns) + pw.field_bytes(2, event.name.encode())
        body += b"".join(pw.field_bytes(3, _kv(k, v)) for k, v in event.attributes.items())
        out.append(pw.field_bytes(11, body))
    if span.status or span.status_message:
        status = b""
        if span.status_message:
            status += pw.field_bytes(2, span.status_message.encode())
        if span.status:
            status += pw.field_varint(3, span.status)
        out.append(pw.field_bytes(15, status))
    return b"".join(out)


def encode_request(spans: list[Span]) -> bytes:
    """An ExportTraceServiceRequest, one ResourceSpans per service."""
    by_service: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_service[span.service].append(span)

    body = []
    for service, group in by_service.items():
        resource = pw.field_bytes(1, _kv("service.name", service))
        scope = pw.field_bytes(1, pw.field_bytes(1, b"wake.demo"))
        scope += b"".join(pw.field_bytes(2, encode_span(s)) for s in group)
        resource_spans = pw.field_bytes(1, resource) + pw.field_bytes(2, scope)
        body.append(pw.field_bytes(1, resource_spans))
    return b"".join(body)
