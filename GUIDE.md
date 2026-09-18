# Wake — Complete Project Guide

## Table of Contents
1. [What is Wake?](#what-is-wake)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [Architecture](#architecture)
5. [Life of a Span](#life-of-a-span)
6. [Database Schema](#database-schema)
7. [Code Walkthrough](#code-walkthrough)
8. [Endpoints](#endpoints)
9. [Sending Traces From Your Own Service](#sending-traces-from-your-own-service)
10. [Testing Strategy](#testing-strategy)
11. [Benchmarks](#benchmarks)
12. [Extending Wake](#extending-wake)
13. [Troubleshooting](#troubleshooting)

---

## What is Wake?

Wake is an OpenTelemetry trace collector with no runtime dependencies. Services instrumented with
OpenTelemetry send it spans over OTLP/HTTP. Wake stitches spans from different services into one
trace, repairs the common problems real traffic has, stores traces in SQLite, and serves pages with
flame charts and flame graphs.

---

## Quick Start

Requires Python 3.10 or newer. On this machine `python3` is 3.6, so the Makefile uses `python3.12`.

```bash
make install     # .venv with test tools, including the real OpenTelemetry SDK for tests
make test
make serve       # collector and pages on http://127.0.0.1:4318
make demo        # in another terminal: a shop with seven services, errors, skew and orphans
```

Open http://127.0.0.1:4318 to see the traces.

Optional faster protobuf decoding:

```bash
.venv/bin/pip install -e ".[fast]"   # installs opentelemetry-proto
WAKE_PURE_PROTOBUF=1 make serve      # force the hand-written decoder anyway
```

---

## Core Concepts

### Span
One timed operation, such as an HTTP handler or a database query. It has a trace id, its own span
id, a parent span id, a service name, a start and end time, attributes, events and a status.

### Trace
Every span sharing one trace id: the whole journey of one request through all services. Parent ids
link spans into a tree.

### OTLP
The OpenTelemetry Protocol. Wake accepts it over HTTP at `POST /v1/traces`, in protobuf or JSON,
optionally gzipped.

### Flame chart versus flame graph
- A **flame chart** shows one trace on a real time axis. Position is when a span ran, width is how
  long, and depth is who called whom. Parallel calls get separate lanes.
- A **flame graph** merges many traces. Identical call paths collapse into one bar whose width is
  total time. Children are sorted by name, not time.

### Self time
A span's duration minus the time its children cover. Parallel children overlap, so covered time is
the union of their intervals, clipped to the parent.

---

## Architecture

```
  instrumented services
  ┌──────────┐ ┌──────────┐
  │ OTel SDK │ │ OTel SDK │   POST /v1/traces (protobuf or JSON)
  └────┬─────┘ └────┬─────┘
       └──────┬─────┘
              ▼
  ┌───────────────────────────────── Wake (one process) ─────────────────┐
  │ server.py   ThreadingHTTPServer, background flusher                   │
  │   │                                                                    │
  │   ├─ otlp.py         decode: hand-written protobuf, or otlp_fast.py    │
  │   │                  with the official library, or JSON                │
  │   ├─ store.py        TraceBuffer: traces still receiving spans         │
  │   │                  flushed when idle, or oldest first over budget    │
  │   │                  TraceStore: SQLite, one row per trace             │
  │   ├─ assemble.py     stitch, dedupe, orphans, clock skew, self time    │
  │   └─ flame.py        SVG flame charts, flame graphs, folded stacks     │
  │      pages.py        server-rendered HTML                              │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## Life of a Span

1. An exporter posts a batch to `/v1/traces`. Wake checks the content type and size, decompresses
   gzip, and decodes the body into `Span` objects.
2. `Collector.ingest()` adds the spans to the `TraceBuffer`, grouped by trace id. The first copy of a
   duplicated span wins.
3. A trace with no new span for `--idle` seconds, 5 by default, is due. So is the least recently
   touched trace whenever the buffer holds more than `--max-spans` spans. Those early flushes are
   counted and flagged.
4. The background flusher writes due traces to SQLite in one transaction per batch, with a summary
   row and all spans as one JSON array.
5. A span arriving after its trace was stored merges into the stored trace on the next flush.
6. When you open a trace page, the stored spans are passed to `assemble()`, which builds the tree and
   records every repair, and `flame_chart()` draws it.

---

## Database Schema

Defined in `wake/store.py`. The database runs in WAL mode.

### `traces`
| column | purpose |
|---|---|
| `trace_id` | primary key |
| `root_service`, `root_name` | the root span, for the listing |
| `start_ns`, `duration_ns` | indexed for sorting and duration search |
| `span_count`, `error_count`, `services` | summary fields |
| `flushed_early` | set when the buffer budget forced the flush |
| `spans` | every span as one compact JSON array. Last on purpose, so searches never read it |

### `trace_services`
`(service, trace_id)` pairs, so filtering by service uses an index instead of scanning.

---

## Code Walkthrough

| file | responsibility |
|---|---|
| `wake/protowire.py` | the protobuf wire format by hand: varints, fixed-width fields, length-delimited nesting, unknown fields skipped |
| `wake/otlp.py` | OTLP protobuf and JSON into spans. `decode(payload, content_type)` picks the decoder. Accepts hex or base64 ids and enum numbers or names |
| `wake/otlp_fast.py` | the same through the official `opentelemetry-proto` library, used automatically when installed |
| `wake/model.py` | `Span` and `Event`, plus the API form `to_dict()` and compact stored form `to_row()` |
| `wake/assemble.py` | `assemble(spans)` returns a `TraceTree`: dedupe, orphans, cycle breaking, clock-skew shifting, self time, all iterative so 5,000-deep traces work |
| `wake/store.py` | `TraceBuffer`, `TraceStore` and the `Collector` that combines them |
| `wake/flame.py` | `flame_chart()`, `lanes()`, `aggregate()`, `flame_graph()`, `folded()`, and the colour palette |
| `wake/pages.py` | the index page and the trace page |
| `wake/server.py` | request handling, the API, and the background flusher |
| `wake/encode.py`, `wake/demo.py` | synthetic traffic encoded as real OTLP protobuf |
| `wake/cli.py` | `serve`, `demo`, `flame` |

### Repairs the assembler makes
- **Duplicates.** Retried batches are counted, not stacked.
- **Orphans.** A span whose parent never arrived is kept at the top level and marked.
- **Loops.** A parent chain that points back at itself is broken.
- **Clock skew.** A child from a different service that seems to start before its parent is shifted,
  with its whole subtree, to start no earlier. Durations never change. Within one service it is left
  alone, because one clock cannot disagree with itself.

Every repair is listed on the trace page.

### Colours
Each service takes a fixed slot from a palette checked for colour-vision separation. After seven
services the rest are grey. Errors use a dashed outline and a label, never colour alone.

---

## Endpoints

| route | what it is |
|---|---|
| `POST /v1/traces` | OTLP/HTTP ingest, protobuf or JSON, gzip accepted |
| `GET /` | recent traces, merged flame graph, service and error filters |
| `GET /traces/{id}` | one trace: repairs, flame chart, span table |
| `GET /traces/{id}/flame.svg` | the flame chart alone |
| `GET /flame.svg`, `GET /flame.folded` | merged flame graph as SVG or folded stacks |
| `GET /api/traces?service=&min_ms=&errors=1` | search |
| `GET /api/traces/{id}` | the assembled tree as JSON |
| `GET /api/stats` | counters, buffer state, and which decoder is active |

Errors are specific: 400 for a malformed body, 415 naming the supported content types, and 413 for a
body over 16 MB, rejected before it is read.

---

## Sending Traces From Your Own Service

Any OpenTelemetry SDK with the OTLP/HTTP exporter works. For Python:

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_SERVICE_NAME=checkout
```

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

with trace.get_tracer(__name__).start_as_current_span("handle-order"):
    ...
```

To stitch across services, propagate the trace context in request headers. The SDK's
instrumentation libraries do this automatically.

Folded stacks for Brendan Gregg's flame graph tools:

```bash
.venv/bin/python -m wake.cli flame --database wake.db --service checkout > out.folded
```

---

## Testing Strategy

| file | what it covers |
|---|---|
| `tests/test_protowire.py` | the wire format, including field numbers checked against the official descriptors |
| `tests/test_otlp.py` | decoding real SDK output with both decoders and both encodings |
| `tests/test_assemble.py` | stitching, orphans, loops, skew, self time, very deep traces |
| `tests/test_store.py` | the buffer, idle and budget flushes, late spans, SQLite |
| `tests/test_flame.py` | charts, lanes for parallel calls, graphs, folded stacks |
| `tests/test_server.py` | HTTP, including the real OpenTelemetry exporter end to end |

The end-to-end test runs two services sharing one distributed trace through the official SDK and
asserts they come back as a single tree. CI runs the whole suite with each protobuf decoder.

---

## Benchmarks

```bash
make bench     # ingest rate, sustained storage rate, memory per span
```

Results are in the README. When quoting throughput, always say whether it is **accepted** over HTTP
or **stored** on disk. Stored is the real capacity. Accepted only fills the buffer faster.

---

## Extending Wake

Left out on purpose:
- **gRPC** transport. Needs HTTP/2 and a dependency.
- **Storage in its own process.** The next throughput ceiling.
- **Tail sampling and retention.** Every trace is kept until you delete the database.
- **Metrics and logs.** Traces only.
- **Authentication.** Bind to localhost or put a proxy in front.

---

## Troubleshooting

### No traces appear
Traces are stored only after they go quiet for `--idle` seconds. Wait a few seconds, or lower
`--idle`. Check `/api/stats` to see spans arriving in the buffer.

### My exporter gets 415
It sent a content type Wake does not accept. Use `application/x-protobuf` or `application/json`.
The response body names both.

### Spans from two services show as separate traces
The trace context is not being propagated between them, so they have different trace ids. Use the
SDK's HTTP client and server instrumentation.

### A trace is marked as flushed early
Traffic outran the idle timeout and the buffer hit `--max-spans`. Raise `--max-spans` if memory
allows. The README gives the memory cost per span.

### Port 4318 is already in use
Another collector is running. Start Wake with `--port`, and point `OTEL_EXPORTER_OTLP_ENDPOINT` at it.
