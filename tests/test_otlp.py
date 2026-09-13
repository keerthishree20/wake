"""Decoding OTLP, checked against the official OpenTelemetry SDK.

The SDK is a test dependency only. Wake reads the bytes itself; these tests make
sure what it reads is what the SDK meant.
"""

from __future__ import annotations

import base64

import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as svc
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from wake import otlp
from wake.encode import encode_request

from .conftest import span as make_span


def sdk_spans(service: str = "checkout"):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("tests")
    with tracer.start_as_current_span("POST /orders", kind=SpanKind.SERVER, attributes={
        "http.status_code": 201, "negative": -7, "retry": True, "ratio": 0.25,
        "tags": ["a", "b"], "big": 2**62,
    }):
        with tracer.start_as_current_span("charge", kind=SpanKind.CLIENT) as child:
            child.add_event("authorised", {"amount": 1999})
            child.set_status(Status(StatusCode.ERROR, "declined"))
    return exporter.get_finished_spans()


# ------------------------------------------------------------ field numbers

@pytest.mark.parametrize("message,expected", [
    (svc.ExportTraceServiceRequest, {"resource_spans": otlp.REQUEST_RESOURCE_SPANS}),
    (trace_pb2.ResourceSpans, {"resource": otlp.RS_RESOURCE, "scope_spans": otlp.RS_SCOPE_SPANS}),
    (trace_pb2.ScopeSpans, {"spans": otlp.SS_SPANS}),
    (trace_pb2.Span, {"trace_id": otlp.SPAN_TRACE_ID, "span_id": otlp.SPAN_SPAN_ID,
                      "parent_span_id": otlp.SPAN_PARENT_ID, "name": otlp.SPAN_NAME,
                      "kind": otlp.SPAN_KIND, "start_time_unix_nano": otlp.SPAN_START,
                      "end_time_unix_nano": otlp.SPAN_END, "attributes": otlp.SPAN_ATTRIBUTES,
                      "events": otlp.SPAN_EVENTS, "status": otlp.SPAN_STATUS}),
    (trace_pb2.Status, {"message": otlp.STATUS_MESSAGE, "code": otlp.STATUS_CODE}),
    (common_pb2.KeyValue, {"key": otlp.KV_KEY, "value": otlp.KV_VALUE}),
    (common_pb2.AnyValue, {"string_value": otlp.ANY_STRING, "bool_value": otlp.ANY_BOOL,
                           "int_value": otlp.ANY_INT, "double_value": otlp.ANY_DOUBLE,
                           "array_value": otlp.ANY_ARRAY, "kvlist_value": otlp.ANY_KVLIST,
                           "bytes_value": otlp.ANY_BYTES}),
])
def test_field_numbers_match_the_official_schema(message, expected):
    """If upstream ever renumbers a field, this fails instead of Wake quietly
    misreading every span."""
    actual = {f.name: f.number for f in message.DESCRIPTOR.fields}
    for name, number in expected.items():
        assert actual[name] == number, f"{message.DESCRIPTOR.name}.{name}"


# ------------------------------------------------------------------ protobuf

def test_spans_from_the_real_sdk_decode_exactly():
    finished = sdk_spans()
    decoded = {s.span_id: s for s in otlp.decode_protobuf(encode_spans(finished).SerializeToString())}
    assert len(decoded) == 2

    for original in finished:
        got = decoded[format(original.context.span_id, "016x")]
        assert got.trace_id == format(original.context.trace_id, "032x")
        assert got.start_ns == original.start_time and got.end_ns == original.end_time
        assert got.name == original.name
        assert got.service == "checkout"
        expected_parent = format(original.parent.span_id, "016x") if original.parent else ""
        assert got.parent_span_id == expected_parent


def test_every_attribute_type_survives():
    root = next(s for s in otlp.decode_protobuf(encode_spans(sdk_spans()).SerializeToString())
                if s.is_root)
    assert root.attributes == {"http.status_code": 201, "negative": -7, "retry": True,
                               "ratio": 0.25, "tags": ["a", "b"], "big": 2**62}
    assert root.kind == 2


def test_status_and_events_survive():
    child = next(s for s in otlp.decode_protobuf(encode_spans(sdk_spans()).SerializeToString())
                 if not s.is_root)
    assert child.status == 2 and child.status_message == "declined"
    assert child.kind == 3
    assert [(e.name, e.attributes) for e in child.events] == [("authorised", {"amount": 1999})]


def test_each_resource_names_its_own_service():
    payload = svc.ExportTraceServiceRequest()
    for name in ("checkout", "payments"):
        payload.resource_spans.extend(encode_spans(sdk_spans(name)).resource_spans)
    services = {s.service for s in otlp.decode_protobuf(payload.SerializeToString())}
    assert services == {"checkout", "payments"}


def test_a_resource_without_a_service_name_is_labelled_not_dropped():
    request = svc.ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    sp = rs.scope_spans.add().spans.add()
    sp.trace_id, sp.span_id, sp.name = b"\x01" * 16, b"\x02" * 8, "lonely"
    (decoded,) = otlp.decode_protobuf(request.SerializeToString())
    assert decoded.service == otlp.UNKNOWN_SERVICE


def test_an_all_zero_trace_id_is_rejected():
    request = svc.ExportTraceServiceRequest()
    sp = request.resource_spans.add().scope_spans.add().spans.add()
    sp.trace_id, sp.span_id = b"\x00" * 16, b"\x02" * 8
    with pytest.raises(ValueError, match="invalid trace id"):
        otlp.decode_protobuf(request.SerializeToString())


def test_garbage_is_a_decode_error_not_a_crash():
    with pytest.raises(otlp.DecodeError):
        otlp.decode(b"\x0a\xff\xff\xff", "application/x-protobuf")


# ---------------------------------------------------------------------- json

def to_otlp_json(message) -> dict:
    """The protobuf JSON mapping base64-encodes bytes. OTLP JSON wants hex ids,
    so convert the way a conforming exporter would."""
    document = MessageToDict(message)
    for rs in document.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in sp:
                        sp[key] = base64.b64decode(sp[key]).hex()
    return document


def test_otlp_json_decodes_the_same_as_protobuf():
    import json
    message = encode_spans(sdk_spans())
    from_proto = {s.span_id: s for s in otlp.decode_protobuf(message.SerializeToString())}
    from_json = {s.span_id: s for s in otlp.decode_json(json.dumps(to_otlp_json(message)))}
    assert from_proto.keys() == from_json.keys()
    for span_id, expected in from_proto.items():
        got = from_json[span_id]
        assert (got.trace_id, got.parent_span_id, got.name, got.service, got.start_ns,
                got.end_ns, got.kind, got.status, got.status_message) == \
               (expected.trace_id, expected.parent_span_id, expected.name, expected.service,
                expected.start_ns, expected.end_ns, expected.kind, expected.status,
                expected.status_message)
        assert got.attributes == expected.attributes


def test_base64_ids_are_accepted_as_a_fallback():
    """Some senders apply the plain protobuf JSON mapping and send base64. Wake
    takes those too rather than rejecting a whole batch."""
    import json
    document = MessageToDict(encode_spans(sdk_spans()))   # ids left as base64
    assert len(otlp.decode_json(json.dumps(document))) == 2


def test_json_errors_are_decode_errors():
    with pytest.raises(otlp.DecodeError, match="malformed JSON"):
        otlp.decode(b"{not json", "application/json")
    with pytest.raises(otlp.DecodeError):
        otlp.decode(b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"zz"}]}]}]}',
                    "application/json")


def test_unsupported_content_types_are_named():
    with pytest.raises(otlp.DecodeError, match="unsupported content type"):
        otlp.decode(b"", "text/plain")


def test_content_type_parameters_are_ignored():
    body = encode_spans(sdk_spans()).SerializeToString()
    assert len(otlp.decode(body, "application/x-protobuf; charset=binary")) == 2


# ------------------------------------------------------------ wake's encoder

def test_wake_encoded_requests_parse_with_the_official_library():
    """The demo and benchmark encoder is checked against the real decoder, so
    it is genuine OTLP rather than a dialect only Wake understands."""
    spans = [make_span("a", service="frontend", name="GET /", attrs=1),
             make_span("b", "a", service="db", name="SELECT", status=2, rows=3, ok=True, p=0.5)]
    spans[1].status_message = "timeout"
    parsed = svc.ExportTraceServiceRequest.FromString(encode_request(spans))

    names = {}
    for rs in parsed.resource_spans:
        service = next(a.value.string_value for a in rs.resource.attributes if a.key == "service.name")
        for ss in rs.scope_spans:
            for sp in ss.spans:
                names[sp.name] = (service, sp.span_id.hex(), sp.parent_span_id.hex(), sp.status.code,
                                  sp.status.message)
    assert names["GET /"] == ("frontend", spans[0].span_id, "", 0, "")
    assert names["SELECT"] == ("db", spans[1].span_id, spans[0].span_id, 2, "timeout")


def test_wake_encoding_round_trips_through_wake_decoding():
    spans = [make_span("a", service="x", nested={"k": [1, "two", False]}, neg=-3)]
    (back,) = otlp.decode_protobuf(encode_request(spans))
    assert back.attributes == {"nested": {"k": [1, "two", False]}, "neg": -3}


def test_enum_names_are_accepted_as_well_as_integers():
    """Found by testing against the protobuf library's own JSON output, which
    writes SPAN_KIND_CLIENT where the OTLP spec says 3."""
    document = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736", "spanId": "00f067aa0ba902b7",
        "name": "n", "kind": "SPAN_KIND_CLIENT", "status": {"code": "STATUS_CODE_ERROR"},
    }]}]}]}
    import json
    (decoded,) = otlp.decode_json(json.dumps(document))
    assert decoded.kind == 3 and decoded.status == 2


def test_an_unknown_enum_name_is_rejected():
    import json
    document = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736", "spanId": "00f067aa0ba902b7",
        "kind": "SPAN_KIND_TELEPATHIC"}]}]}]}
    with pytest.raises(otlp.DecodeError, match="unknown enum"):
        otlp.decode_json(json.dumps(document))


# ------------------------------------------------------- the two protobuf paths

def test_both_protobuf_decoders_produce_identical_spans():
    """The library path is an accelerator, not a second dialect. Real SDK output
    and Wake's own demo traffic must decode to exactly the same spans."""
    from wake import otlp_fast
    from wake.demo import generate
    assert otlp_fast.AVAILABLE

    payloads = [encode_spans(sdk_spans()).SerializeToString()]
    payloads += [encode_request([s for t in generate(40, seed=n) for s in t]) for n in range(3)]
    for payload in payloads:
        by_hand = otlp.decode_protobuf(payload)
        by_library = otlp_fast.decode_protobuf(payload)
        assert [s.to_dict() for s in by_hand] == [s.to_dict() for s in by_library]


def test_both_decoders_reject_the_same_bad_ids():
    from wake import otlp_fast
    request = svc.ExportTraceServiceRequest()
    sp = request.resource_spans.add().scope_spans.add().spans.add()
    sp.trace_id, sp.span_id = b"\x00" * 16, b"\x02" * 8
    for decoder in (otlp.decode_protobuf, otlp_fast.decode_protobuf):
        with pytest.raises(ValueError, match="invalid trace id"):
            decoder(request.SerializeToString())


def test_the_pure_decoder_can_be_forced(monkeypatch):
    monkeypatch.setenv("WAKE_PURE_PROTOBUF", "1")
    assert otlp.protobuf_decoder()[1] == "hand-written"
    monkeypatch.delenv("WAKE_PURE_PROTOBUF")
    assert otlp.protobuf_decoder()[1] == "opentelemetry-proto"
