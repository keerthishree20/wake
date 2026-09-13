"""OTLP trace requests, in both of their encodings, to Wake spans.

Field numbers are from `opentelemetry/proto/trace/v1/trace.proto` and its
neighbours. The test suite checks them against the descriptors in the official
generated code, so a renumbering upstream fails a test here instead of quietly
misreading spans.

Two encodings arrive on the same endpoint:

* **protobuf**, `application/x-protobuf`, which every official exporter sends by
  default.
* **JSON**, `application/json`. OTLP's JSON is not the standard protobuf JSON
  mapping: trace and span ids are hex strings rather than base64, and enums are
  integers. Senders built on the standard mapping get both of those "wrong", so
  base64 ids and enum names are accepted as fallbacks. Both were found by
  testing against JSON produced by the official protobuf library, not guessed.
"""

from __future__ import annotations

import base64
import binascii
import json

from . import protowire as pw
from .model import Event, Span

# ExportTraceServiceRequest
REQUEST_RESOURCE_SPANS = 1
# ResourceSpans
RS_RESOURCE, RS_SCOPE_SPANS = 1, 2
# Resource
RESOURCE_ATTRIBUTES = 1
# ScopeSpans
SS_SPANS = 2
# Span
SPAN_TRACE_ID, SPAN_SPAN_ID, SPAN_PARENT_ID, SPAN_NAME, SPAN_KIND = 1, 2, 4, 5, 6
SPAN_START, SPAN_END, SPAN_ATTRIBUTES, SPAN_EVENTS, SPAN_STATUS = 7, 8, 9, 11, 15
# Span.Event
EVENT_TIME, EVENT_NAME, EVENT_ATTRIBUTES = 1, 2, 3
# Status
STATUS_MESSAGE, STATUS_CODE = 2, 3
# KeyValue
KV_KEY, KV_VALUE = 1, 2
# AnyValue
ANY_STRING, ANY_BOOL, ANY_INT, ANY_DOUBLE, ANY_ARRAY, ANY_KVLIST, ANY_BYTES = 1, 2, 3, 4, 5, 6, 7
# ArrayValue and KeyValueList both hold their items in field 1
LIST_VALUES = 1

UNKNOWN_SERVICE = "unknown_service"


class DecodeError(ValueError):
    """The request could not be read as OTLP traces."""


# ------------------------------------------------------------------ protobuf

def decode_protobuf(payload: bytes) -> list[Span]:
    spans: list[Span] = []
    try:
        for number, _wire, value in pw.fields(payload):
            if number == REQUEST_RESOURCE_SPANS:
                _resource_spans(value, spans)
    except (pw.WireError, UnicodeDecodeError) as exc:
        raise DecodeError(f"malformed protobuf: {exc}") from exc
    return spans


def _resource_spans(buf: memoryview, out: list[Span]) -> None:
    resource_attrs: dict = {}
    scope_blocks: list[memoryview] = []
    # Resource and scope spans can arrive in either order on the wire, and the
    # service name lives in the resource. Collect first, then build spans.
    for number, _wire, value in pw.fields(buf):
        if number == RS_RESOURCE:
            for inner, _w, item in pw.fields(value):
                if inner == RESOURCE_ATTRIBUTES:
                    key, attr = _key_value(item)
                    resource_attrs[key] = attr
        elif number == RS_SCOPE_SPANS:
            scope_blocks.append(value)

    service = str(resource_attrs.get("service.name") or UNKNOWN_SERVICE)
    for block in scope_blocks:
        for number, _wire, value in pw.fields(block):
            if number == SS_SPANS:
                out.append(_span(value, service))


def _span(buf: memoryview, service: str) -> Span:
    trace_id = span_id = parent = name = message = ""
    kind = status = start = end = 0
    attributes: dict = {}
    events: list[Event] = []

    for number, _wire, value in pw.fields(buf):
        if number == SPAN_TRACE_ID:
            trace_id = bytes(value).hex()
        elif number == SPAN_SPAN_ID:
            span_id = bytes(value).hex()
        elif number == SPAN_PARENT_ID:
            parent = bytes(value).hex()
        elif number == SPAN_NAME:
            name = pw.as_str(value)
        elif number == SPAN_KIND:
            kind = value
        elif number == SPAN_START:
            start = value
        elif number == SPAN_END:
            end = value
        elif number == SPAN_ATTRIBUTES:
            key, attr = _key_value(value)
            attributes[key] = attr
        elif number == SPAN_EVENTS:
            events.append(_event(value))
        elif number == SPAN_STATUS:
            for inner, _w, item in pw.fields(value):
                if inner == STATUS_CODE:
                    status = item
                elif inner == STATUS_MESSAGE:
                    message = pw.as_str(item)

    _validate_ids(trace_id, span_id, parent)
    return Span(trace_id, span_id, parent, name, service, start, end, kind, status,
                message, attributes, events)


def _event(buf: memoryview) -> Event:
    name, time_ns, attributes = "", 0, {}
    for number, _wire, value in pw.fields(buf):
        if number == EVENT_TIME:
            time_ns = value
        elif number == EVENT_NAME:
            name = pw.as_str(value)
        elif number == EVENT_ATTRIBUTES:
            key, attr = _key_value(value)
            attributes[key] = attr
    return Event(name, time_ns, attributes)


def _key_value(buf: memoryview) -> tuple[str, object]:
    key, value = "", None
    for number, _wire, item in pw.fields(buf):
        if number == KV_KEY:
            key = pw.as_str(item)
        elif number == KV_VALUE:
            value = _any_value(item)
    return key, value


def _any_value(buf: memoryview):
    for number, _wire, item in pw.fields(buf):
        if number == ANY_STRING:
            return pw.as_str(item)
        if number == ANY_BOOL:
            return bool(item)
        if number == ANY_INT:
            return pw.as_int64(item)
        if number == ANY_DOUBLE:
            return pw.as_double(item)
        if number == ANY_BYTES:
            return bytes(item).hex()
        if number == ANY_ARRAY:
            return [_any_value(v) for n, _w, v in pw.fields(item) if n == LIST_VALUES]
        if number == ANY_KVLIST:
            return dict(_key_value(v) for n, _w, v in pw.fields(item) if n == LIST_VALUES)
    return None


# ---------------------------------------------------------------------- json

def decode_json(payload: bytes | str) -> list[Span]:
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DecodeError(f"malformed JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DecodeError("expected a JSON object with resourceSpans")

    spans: list[Span] = []
    try:
        for resource_spans in document.get("resourceSpans") or []:
            resource = resource_spans.get("resource") or {}
            attrs = _json_attributes(resource.get("attributes"))
            service = str(attrs.get("service.name") or UNKNOWN_SERVICE)
            for scope_spans in resource_spans.get("scopeSpans") or []:
                for raw in scope_spans.get("spans") or []:
                    spans.append(_json_span(raw, service))
    except (KeyError, TypeError, ValueError) as exc:
        raise DecodeError(f"malformed OTLP JSON: {exc}") from exc
    return spans


def _json_span(raw: dict, service: str) -> Span:
    status = raw.get("status") or {}
    trace_id = _json_id(raw.get("traceId"), 32)
    span_id = _json_id(raw.get("spanId"), 16)
    parent = _json_id(raw.get("parentSpanId"), 16)
    _validate_ids(trace_id, span_id, parent)
    return Span(
        trace_id=trace_id, span_id=span_id, parent_span_id=parent,
        name=str(raw.get("name") or ""), service=service,
        start_ns=int(raw.get("startTimeUnixNano") or 0),
        end_ns=int(raw.get("endTimeUnixNano") or 0),
        kind=_json_enum(raw.get("kind"), _SPAN_KINDS),
        status=_json_enum(status.get("code"), _STATUS_CODES),
        status_message=str(status.get("message") or ""),
        attributes=_json_attributes(raw.get("attributes")),
        events=[Event(str(e.get("name") or ""), int(e.get("timeUnixNano") or 0),
                      _json_attributes(e.get("attributes")))
                for e in raw.get("events") or []],
    )


_SPAN_KINDS = {"SPAN_KIND_UNSPECIFIED": 0, "SPAN_KIND_INTERNAL": 1, "SPAN_KIND_SERVER": 2,
               "SPAN_KIND_CLIENT": 3, "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5}
_STATUS_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


def _json_enum(value, names: dict[str, int]) -> int:
    """OTLP JSON requires enums as integers. The protobuf library's own JSON
    conversion writes their names instead, and senders built on it do the same,
    so both are accepted."""
    if value is None or value == "":
        return 0
    if isinstance(value, str) and not value.lstrip("-").isdigit():
        if value not in names:
            raise ValueError(f"unknown enum value {value!r}")
        return names[value]
    return int(value)


def _json_id(value, hex_length: int) -> str:
    if not value:
        return ""
    text = str(value)
    if len(text) == hex_length:
        try:
            bytes.fromhex(text)
            return text.lower()
        except ValueError:
            pass
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"id {text!r} is neither hex nor base64") from None
    return decoded.hex()


def _json_attributes(items) -> dict:
    return {item["key"]: _json_any(item.get("value") or {}) for item in items or []}


def _json_any(value: dict):
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])        # int64s arrive as strings in OTLP JSON
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "bytesValue" in value:
        return base64.b64decode(value["bytesValue"]).hex()
    if "arrayValue" in value:
        return [_json_any(v) for v in value["arrayValue"].get("values") or []]
    if "kvlistValue" in value:
        return _json_attributes(value["kvlistValue"].get("values"))
    return None


# ------------------------------------------------------------------ shared

def _validate_ids(trace_id: str, span_id: str, parent: str) -> None:
    """Refuse spans that cannot be stitched. An all-zero id is invalid in the
    spec, and a span without ids would otherwise land in one giant bucket."""
    if len(trace_id) != 32 or trace_id == "0" * 32:
        raise ValueError(f"invalid trace id {trace_id!r}")
    if len(span_id) != 16 or span_id == "0" * 16:
        raise ValueError(f"invalid span id {span_id!r}")
    if parent and (len(parent) != 16 or parent == "0" * 16):
        raise ValueError(f"invalid parent span id {parent!r}")


def protobuf_decoder():
    """The library-backed decoder when available, else the hand-written one."""
    import os
    from . import otlp_fast
    if otlp_fast.AVAILABLE and os.environ.get("WAKE_PURE_PROTOBUF") != "1":
        return otlp_fast.decode_protobuf, "opentelemetry-proto"
    return decode_protobuf, "hand-written"


def decode(payload: bytes, content_type: str) -> list[Span]:
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if kind in ("application/x-protobuf", "application/protobuf"):
        decoder, _name = protobuf_decoder()
        try:
            return decoder(payload)
        except ValueError as exc:
            raise DecodeError(str(exc)) from exc
    if kind == "application/json":
        return decode_json(payload)
    raise DecodeError(
        f"unsupported content type {content_type!r}; send application/x-protobuf or application/json")
