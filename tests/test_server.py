"""The HTTP surface, including the real OpenTelemetry exporter sending to it."""

from __future__ import annotations

import gzip
import http.client
import json
import time
import urllib.request

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry import trace as otel_trace

from wake.encode import encode_request

from .conftest import TRACE, span


def base(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def post(server, body: bytes, content_type: str, **headers) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    connection.request("POST", "/v1/traces", body=body,
                       headers={"Content-Type": content_type, **headers})
    response = connection.getresponse()
    return response.status, response.read()


def get(server, path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(base(server) + path, timeout=10) as response:
            return response.status, response.read(), response.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type")


def wait_stored(server, count: int, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server.collector.store.count() >= count:
            return
        time.sleep(0.02)
    pytest.fail(f"expected {count} stored traces, have {server.collector.store.count()}")


def two_services():
    return [span("a", service="frontend", name="GET /", end=20_000_000),
            span("b", "a", service="payments", name="charge", start=1_000_000, end=9_000_000)]


def test_protobuf_ingest_then_query(server):
    status, body = post(server, encode_request(two_services()), "application/x-protobuf")
    assert status == 200 and body == b""
    code, payload, _ = get(server, f"/api/traces/{TRACE}")
    assert code == 200
    trace = json.loads(payload)
    assert trace["span_count"] == 2 and trace["services"] == ["frontend", "payments"]


def test_json_ingest_answers_with_json(server):
    document = {"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "json-svc"}}]},
        "scopeSpans": [{"spans": [{"traceId": TRACE, "spanId": "00f067aa0ba902b7", "name": "x",
                                   "startTimeUnixNano": "1", "endTimeUnixNano": "2000000"}]}]}]}
    status, body = post(server, json.dumps(document).encode(), "application/json")
    assert status == 200 and body == b"{}"
    assert json.loads(get(server, f"/api/traces/{TRACE}")[1])["services"] == ["json-svc"]


def test_gzip_bodies_are_accepted(server):
    status, _ = post(server, gzip.compress(encode_request(two_services())),
                     "application/x-protobuf", **{"Content-Encoding": "gzip"})
    assert status == 200


def test_bad_bodies_get_useful_errors(server):
    status, body = post(server, b"\x0a\xff\xff", "application/x-protobuf")
    assert status == 400 and b"malformed protobuf" in body
    status, body = post(server, b"hello", "text/plain")
    assert status == 415 and b"application/x-protobuf" in body
    status, _ = post(server, b"not gzip", "application/x-protobuf", **{"Content-Encoding": "gzip"})
    assert status == 400
    assert server.collector.stats()["rejected_batches"] >= 2


def test_an_oversized_body_is_refused_before_it_is_read(server):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    connection.putrequest("POST", "/v1/traces")
    connection.putheader("Content-Type", "application/x-protobuf")
    connection.putheader("Content-Length", str(100 * 1024 * 1024))
    connection.endheaders()
    assert connection.getresponse().status == 413


def test_traces_flush_to_the_store_once_quiet(server):
    post(server, encode_request(two_services()), "application/x-protobuf")
    wait_stored(server, 1)
    listing = json.loads(get(server, "/api/traces?service=payments")[1])["traces"]
    assert [t["trace_id"] for t in listing] == [TRACE]
    assert listing[0]["in_flight"] is False


def test_the_pages_and_charts_render(server):
    post(server, encode_request(two_services()), "application/x-protobuf")
    wait_stored(server, 1)
    for path, kind in [("/", "text/html"), (f"/traces/{TRACE}", "text/html"),
                       (f"/traces/{TRACE}/flame.svg", "image/svg+xml"),
                       ("/flame.svg", "image/svg+xml"), ("/flame.folded", "text/plain")]:
        code, body, content_type = get(server, path)
        assert code == 200, path
        assert content_type.startswith(kind), path
        assert body
    assert b"frontend:GET_/" in get(server, "/flame.folded")[1]


def test_lookups_validate_their_input(server):
    assert get(server, "/api/traces/not-an-id")[0] == 400
    assert get(server, "/api/traces/" + "a" * 32)[0] == 404
    assert get(server, "/nope")[0] == 404


def test_stats_count_what_happened(server):
    post(server, encode_request(two_services()), "application/x-protobuf")
    post(server, encode_request(two_services()), "application/x-protobuf")   # a retried batch
    stats = json.loads(get(server, "/api/stats")[1])
    assert stats["spans_received"] == 4
    assert stats["duplicate_spans"] == 2


def test_the_official_opentelemetry_exporter_sends_to_wake(server):
    """The proof that matters: the real SDK and its real HTTP exporter, pointed at
    Wake, with two services in one distributed trace."""
    endpoint = base(server) + "/v1/traces"

    def provider(service: str) -> TracerProvider:
        p = TracerProvider(resource=Resource.create({"service.name": service}))
        p.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        return p

    frontend, payments = provider("frontend"), provider("payments")
    with frontend.get_tracer("t").start_as_current_span("POST /checkout") as root:
        # Propagate the context to "another service", as an HTTP header would.
        context = otel_trace.set_span_in_context(root)
        with payments.get_tracer("t").start_as_current_span("charge card", context=context):
            pass
    frontend.force_flush()
    payments.force_flush()

    trace_id = format(root.get_span_context().trace_id, "032x")
    code, payload, _ = get(server, f"/api/traces/{trace_id}")
    assert code == 200
    tree = json.loads(payload)
    assert tree["span_count"] == 2
    assert set(tree["services"]) == {"frontend", "payments"}
    (top,) = tree["roots"]
    assert top["name"] == "POST /checkout" and top["service"] == "frontend"
    assert top["children"][0]["name"] == "charge card"
    assert top["children"][0]["service"] == "payments"
