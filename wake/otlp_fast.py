"""Protobuf decoding through the official library, when it is installed.

Wake needs nothing beyond the standard library, and `otlp.decode_protobuf`
proves the point by reading the wire format by hand. But the benchmark says
what that costs: the official library's C parser is roughly three times faster
end to end, because once parsing is free the remaining work is only building
Python objects.

So this path is used automatically when `opentelemetry-proto` is importable, and
the hand-written reader is the fallback. Set WAKE_PURE_PROTOBUF=1 to force the
fallback. The test suite decodes the same payloads both ways and requires
identical spans, so the two cannot drift apart.
"""

from __future__ import annotations

from .model import Event, Span

try:  # pragma: no cover - exercised by whichever environment runs it
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as _svc
    AVAILABLE = True
except ImportError:  # pragma: no cover
    _svc = None
    AVAILABLE = False

UNKNOWN_SERVICE = "unknown_service"


def _any(value):
    which = value.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_any(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _any(kv.value) for kv in value.kvlist_value.values}
    if which == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, which)


def _ids(trace_id: bytes, span_id: bytes, parent: bytes) -> tuple[str, str, str]:
    t, s, p = trace_id.hex(), span_id.hex(), parent.hex()
    if len(t) != 32 or t == "0" * 32:
        raise ValueError(f"invalid trace id {t!r}")
    if len(s) != 16 or s == "0" * 16:
        raise ValueError(f"invalid span id {s!r}")
    if p and (len(p) != 16 or p == "0" * 16):
        raise ValueError(f"invalid parent span id {p!r}")
    return t, s, p


def decode_protobuf(payload: bytes) -> list[Span]:
    from google.protobuf.message import DecodeError as ProtobufDecodeError
    try:
        request = _svc.ExportTraceServiceRequest.FromString(payload)
    except ProtobufDecodeError as exc:
        raise ValueError(f"malformed protobuf: {exc}") from exc

    spans: list[Span] = []
    for resource_spans in request.resource_spans:
        service = UNKNOWN_SERVICE
        for kv in resource_spans.resource.attributes:
            if kv.key == "service.name":
                service = str(_any(kv.value)) or UNKNOWN_SERVICE
        for scope_spans in resource_spans.scope_spans:
            for sp in scope_spans.spans:
                trace_id, span_id, parent = _ids(sp.trace_id, sp.span_id, sp.parent_span_id)
                spans.append(Span(
                    trace_id, span_id, parent, sp.name, service,
                    sp.start_time_unix_nano, sp.end_time_unix_nano, sp.kind,
                    sp.status.code, sp.status.message,
                    {kv.key: _any(kv.value) for kv in sp.attributes},
                    [Event(e.name, e.time_unix_nano, {kv.key: _any(kv.value) for kv in e.attributes})
                     for e in sp.events],
                ))
    return spans
