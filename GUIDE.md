# Wake — Complete Project Guide

A complete guide from zero to a working OpenTelemetry trace collector. Covers every feature, every
design decision and the reason behind it, with the real code. It is self-contained: you can paste it
into any AI chat and ask questions about the project without sharing the repository.

**Repository:** https://github.com/keerthishree20/wake
**All projects:** https://github.com/keerthishree20

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Tech Stack & Why](#2-tech-stack--why)
3. [Project Setup from Scratch](#3-project-setup-from-scratch)
4. [Core Ideas in Plain Words](#4-core-ideas-in-plain-words)
5. [Project Structure](#5-project-structure)
6. [Life of a Span](#6-life-of-a-span)
7. [The Span Model](#7-the-span-model)
8. [Reading Protobuf by Hand](#8-reading-protobuf-by-hand)
9. [OTLP Decoding: Protobuf and JSON](#9-otlp-decoding-protobuf-and-json)
10. [The Fast Decoder](#10-the-fast-decoder)
11. [Buffering Traces](#11-buffering-traces)
12. [Storing Traces in SQLite](#12-storing-traces-in-sqlite)
13. [Stitching a Trace Together](#13-stitching-a-trace-together)
14. [Clock Skew Repair](#14-clock-skew-repair)
15. [Self Time](#15-self-time)
16. [Flame Charts](#16-flame-charts)
17. [Flame Graphs and Folded Stacks](#17-flame-graphs-and-folded-stacks)
18. [Colours and Accessibility](#18-colours-and-accessibility)
19. [Pages and API](#19-pages-and-api)
20. [Demo Traffic](#20-demo-traffic)
21. [Sending Traces From Your Own Service](#21-sending-traces-from-your-own-service)
22. [Command Line & Configuration](#22-command-line--configuration)
23. [Testing](#23-testing)
24. [Benchmarks & Results](#24-benchmarks--results)
25. [Deliberately Not Built](#25-deliberately-not-built)
26. [Troubleshooting](#26-troubleshooting)
27. [Complete Feature Summary](#27-complete-feature-summary)

---

## 1. Project Overview

Wake is an **OpenTelemetry trace collector** with no runtime dependencies. When a request travels
through several services (checkout calls payments, which calls a database), each service reports
timed pieces of work called *spans*. Wake receives those spans over the standard OTLP protocol,
**stitches** spans from different services into one trace, **repairs** the problems real traffic has,
stores traces in SQLite, and draws them as **flame charts** and **flame graphs**.

```bash
wake serve                        # OTLP/HTTP on :4318, pages on the same port
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 your-instrumented-service
```

A ship's wake is the trail it leaves behind. A trace is the trail a request leaves through a system.

**Status:** complete. 108 tests pass, including the official OpenTelemetry SDK exporting to a live Wake.

---

## 2. Tech Stack & Why

| Technology | Role | Why We Chose It |
|---|---|---|
| **Python 3.10+** | Language | clear code for decoding, stitching and drawing |
| **Standard library only** | Runtime | protobuf decoded by hand; no dependency needed |
| **`http.server` (threading)** | Server | OTLP/HTTP and the pages from one port |
| **SQLite (WAL mode)** | Storage | one file, no server, fast enough for a local collector |
| **Hand-drawn SVG** | Charts | no JavaScript, native tooltips, prints well |
| **opentelemetry-proto** (optional) | Fast decoding | about three times faster, used automatically when installed |
| **OpenTelemetry SDK** | Tests only | proves real exporters work against Wake |

---

## 3. Project Setup from Scratch

```bash
git clone https://github.com/keerthishree20/wake.git
cd wake
make install     # .venv with test tools, including the OpenTelemetry SDK (python3.12)
make test        # all 108 tests
make serve       # collector and pages on http://127.0.0.1:4318
make demo        # in another terminal: a shop with seven services, errors, skew and orphans
```

Open http://127.0.0.1:4318 to see traces.

Optional faster decoding:
```bash
.venv/bin/pip install -e ".[fast]"   # installs opentelemetry-proto
WAKE_PURE_PROTOBUF=1 make serve      # force the hand-written decoder anyway
```

---

## 4. Core Ideas in Plain Words

| Idea | Meaning |
|---|---|
| **Span** | one timed operation: an HTTP handler, a database query. Has start, end, service, parent |
| **Trace** | every span sharing one trace id: the whole journey of one request |
| **Parent id** | links spans into a tree: who called whom |
| **OTLP** | the OpenTelemetry Protocol; Wake accepts it over HTTP |
| **Orphan** | a span whose parent never arrived |
| **Clock skew** | two services' clocks disagree, so a child seems to start before its parent |
| **Self time** | time a span spent on its own work, not waiting for children |
| **Flame chart** | one trace on a real time axis |
| **Flame graph** | many traces merged, showing where time goes in total |

---

## 5. Project Structure

```
wake/
  protowire.py  the protobuf wire format, read by hand
  otlp.py       OTLP protobuf and JSON to spans; picks the decoder
  otlp_fast.py  the same through the official library, when installed
  encode.py     spans to OTLP protobuf, for the demo and benchmarks
  model.py      Span and Event; API form and compact stored form
  assemble.py   stitching, orphans, loops, clock skew, self time
  store.py      TraceBuffer, TraceStore (SQLite), Collector over both
  flame.py      flame charts, lanes, flame graphs, folded stacks, colours
  pages.py      the server-rendered HTML pages
  server.py     OTLP ingest, the API, the background flusher
  demo.py       synthetic multi-service shop traffic
  cli.py        serve, demo, flame
bench/
  ingest.py     decode, buffer, flush, accepted and stored rates
  memory.py     bytes per buffered span
tests/          108 tests
docs/           screenshots
```

---

## 6. Life of a Span

1. An exporter posts a batch to `POST /v1/traces`. Wake checks the size (413 over 16 MB), the content
   type (415 if unsupported), decompresses gzip, and decodes the body into `Span` objects.
2. `Collector.ingest()` adds them to the `TraceBuffer`, grouped by trace id. Repeated spans are
   counted and ignored.
3. A trace that has been quiet for `--idle` seconds (5 by default) is due to be stored. So is the
   oldest trace whenever the buffer holds more than `--max-spans` spans.
4. A background thread writes due traces to SQLite, many per transaction.
5. A span that arrives after its trace was stored merges into the stored trace on the next flush.
6. Opening a trace page runs `assemble()` on the stored spans and draws a flame chart.

---

## 7. The Span Model

`wake/model.py`. A `Span` has: trace id, span id, parent id, service, name, kind, start and end in
nanoseconds, status, attributes and events. `__slots__` keeps it smaller.

- `to_dict()` is the API form.
- `to_row()` is a **compact positional list** for storage: no repeated trace id and no computed
  display fields. This halved the database size (section 24).

---

## 8. Reading Protobuf by Hand

Protobuf messages are a sequence of fields, each a *tag* (field number and type) followed by a value.
`wake/protowire.py` reads them.

```python
def read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise WireError("varint runs past the end of the message")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift     # 7 bits of value per byte
        if not byte & 0x80:                   # top bit clear: last byte
            return result, pos
        shift += 7
        if shift > 63:
            raise WireError("varint longer than ten bytes")
```

`fields(buf)` walks a message and yields `(field_number, wire_type, value)` for varints, fixed 64-bit,
fixed 32-bit and length-delimited values. **Unknown fields are skipped**, so newer exporters still
work.

### Guarding against upstream changes
The field numbers used are checked in the tests against the descriptors in the official generated
code. If OpenTelemetry ever renumbered a field, a test would fail instead of Wake silently misreading
spans.

---

## 9. OTLP Decoding: Protobuf and JSON

`wake/otlp.py` `decode(payload, content_type)`:

| Content type | Decoder |
|---|---|
| `application/x-protobuf` or `application/protobuf` | protobuf, fast library if available, else hand-written |
| `application/json` | JSON |
| anything else | `DecodeError` → HTTP 415 naming the right types |

### The JSON surprise
OTLP's JSON writes ids as **hex** and enums as **numbers**, unlike standard protobuf JSON. But testing
against JSON produced by the official protobuf library showed it writes **base64** ids and enum
**names** like `SPAN_KIND_CLIENT`. Wake accepts both forms.

Ids are validated: a trace id must be 32 hex characters, a span id 16.

---

## 10. The Fast Decoder

`wake/otlp_fast.py` decodes through the official `opentelemetry-proto` library, whose parser is in C.

```python
def protobuf_decoder():
    if otlp_fast.AVAILABLE and os.environ.get("WAKE_PURE_PROTOBUF") != "1":
        return otlp_fast.decode_protobuf, "opentelemetry-proto"
    return decode_protobuf, "hand-written"
```

A test decodes real SDK output and demo traffic both ways and requires **identical** spans. CI runs the
whole suite under each decoder. `/api/stats` reports which one is active.

---

## 11. Buffering Traces

Nothing marks the last span of a request, so a trace counts as finished once it has **gone quiet**.

```python
def add(self, spans):
    for span in spans:
        entry = self.traces.get(span.trace_id) or new BufferedTrace
        if span.span_id in entry.spans:
            self.duplicates += 1               # exporters retry whole batches
            continue
        entry.spans[span.span_id] = span
        entry.last_update = now
        self.traces.move_to_end(span.trace_id) # keep oldest-touched first
        self.span_count += 1
    evicted = []
    while self.span_count > self.max_spans and self.traces:
        trace_id, entry = self.traces.popitem(last=False)   # least recently touched
        evicted.append(...)                                 # flushed early, and flagged
    return evicted
```

- **`due()`** returns traces quiet for `idle_s`, oldest first, stopping at the first still-active one.
- **Memory is bounded:** past `max_spans` (200,000 by default, about 141 MB), the least recently
  touched trace is flushed early. Those are marked, because they are the traces most likely to be
  missing a late span.

---

## 12. Storing Traces in SQLite

```sql
CREATE TABLE IF NOT EXISTS traces (
    trace_id      TEXT PRIMARY KEY,
    root_service  TEXT,
    root_name     TEXT,
    start_ns      INTEGER NOT NULL,
    duration_ns   INTEGER NOT NULL,
    span_count    INTEGER NOT NULL,
    error_count   INTEGER NOT NULL,
    services      TEXT NOT NULL,
    flushed_early INTEGER NOT NULL DEFAULT 0,
    spans         TEXT NOT NULL          -- every span as one JSON array, LAST on purpose
);
CREATE INDEX traces_by_start    ON traces (start_ns DESC);
CREATE INDEX traces_by_duration ON traces (duration_ns DESC);
CREATE TABLE trace_services (service TEXT, trace_id TEXT, PRIMARY KEY (service, trace_id));
```

### Why one row per trace, spans last?
SQLite keeps the columns before a big value on the row's first page, so searching summary columns
never touches the pages the spans spill into. And one row per trace means one commit dirties few
pages. The first version used a row per span keyed on random ids, and commits took 35% of the run.

WAL mode and `synchronous=NORMAL` are set. One shared connection guarded by a lock serves all threads.

---

## 13. Stitching a Trace Together

`wake/assemble.py` `assemble(spans)` builds a `TraceTree` and **records every repair it makes**. The
trace page lists them.

| Problem | What Wake does |
|---|---|
| **Duplicates** | exporters retry batches; the first copy wins, later ones are counted |
| **Orphans** | a span whose parent never arrived is kept at the top level and marked. Dropping it would hide exactly the misbehaving service's spans |
| **Loops** | a parent chain that points back at itself is broken instead of followed forever |
| **Clock skew** | see section 14 |
| **Very deep traces** | the walk is iterative, so 5,000 levels still work (tested) |

---

## 14. Clock Skew Repair

Each service stamps times with its own clock. When a child from a **different** service seems to
start before its parent, the parent's clock is trusted, and the child's whole subtree is shifted to
start no earlier:

```python
for child in current.children:
    child.shift_ns += current.shift_ns         # inherit the parent's correction first
    if (child.span.service != current.span.service
            and child.start_ns < current.start_ns):
        child.shift_ns += current.start_ns - child.start_ns
        adjusted += 1
```

- **Durations never change**, only positions.
- A child starting early **within the same service** is left alone, because one clock cannot disagree
  with itself; that is a real bug worth seeing.

---

## 15. Self Time

A span's own time is its duration minus the time its children cover. Parallel children overlap, so
summing their durations would give negative self time. Wake uses the **union** of the children's
intervals, clipped to the parent:

```python
def _self_time(node):
    start, end = node.start_ns, node.end_ns
    intervals = sorted((max(c.start_ns, start), min(c.end_ns, end))
                       for c in node.children if c.end_ns > start and c.start_ns < end)
    covered, cursor = 0, start
    for lo, hi in intervals:
        lo = max(lo, cursor)
        if hi > lo:
            covered += hi - lo
            cursor = hi
    return max((end - start) - covered, 0)
```

---

## 16. Flame Charts

A **flame chart** shows **one trace** on a real time axis: position is when a span ran, width is how
long, and depth is who called whom.

### Lanes for parallel calls
Parallel calls sit at the same depth and overlap in time. At first they shared one row and one bar hid
the other, which only looking at the rendered chart revealed. Now each span goes on the first row
below its parent that is free for its whole interval:

```python
def lanes(tree):
    occupied = []                      # per row, the intervals already drawn
    def free(row, start, end):
        return all(end <= lo or start >= hi for lo, hi in occupied[row])
    ...
    row = parent_row + 1
    while not free(row, start, end):
        row += 1
```

A test covers it. Axis ticks use "nice" steps and readable millisecond labels.

---

## 17. Flame Graphs and Folded Stacks

A **flame graph** merges **many traces**. Identical call paths collapse into one bar whose width is
total time. Children are sorted **by name**, not time, in the format Brendan Gregg introduced.

- `aggregate(trees)` merges trees into `Frame`s.
- `flame_graph(root)` draws the SVG.
- `folded(root)` produces **folded stacks**, the text format his tools read:
  `checkout;payments;db.query 1234`.

---

## 18. Colours and Accessibility

- Each **service** gets a fixed colour slot from a palette checked for colour-vision separation, in
  order of first appearance.
- After **seven** services, the rest are grey, rather than adding hues nobody can tell apart.
- The palette's **red** slot is unused, because it is too close to the error colour.
- **Errors** have a dashed outline and a label, so colour is never the only signal.
- Every bar has a native tooltip: duration, self time, offset, errors and any repair applied.
- Both charts are plain SVG with no script.

---

## 19. Pages and API

| Route | What it is |
|---|---|
| `POST /v1/traces` | OTLP/HTTP ingest, protobuf or JSON, gzip accepted |
| `GET /` | recent traces, merged flame graph, service and error filters |
| `GET /traces/{id}` | one trace: repairs, flame chart, span table |
| `GET /traces/{id}/flame.svg` | the flame chart alone |
| `GET /flame.svg`, `GET /flame.folded` | merged flame graph as SVG or folded stacks |
| `GET /api/traces?service=&min_ms=&errors=1` | search |
| `GET /api/traces/{id}` | the assembled tree as JSON |
| `GET /api/stats` | counters, buffer state, active decoder |

Errors are specific: **400** malformed body (saying what was wrong), **415** unsupported content type
(naming the right ones), **413** over 16 MB (rejected before reading).

---

## 20. Demo Traffic

`wake/demo.py` generates a synthetic shop with seven services: checkout traces with parallel calls,
browse traces, errors, clock skew and orphans, encoded as real OTLP protobuf by `encode.py`.

```bash
make demo        # or: wake demo --count 300 --batch 256 --seed 1
```

---

## 21. Sending Traces From Your Own Service

Any OpenTelemetry SDK with the OTLP/HTTP exporter works. Python:

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

To stitch across services, the trace context must travel in request headers; the SDK's HTTP
instrumentation libraries do this automatically.

---

## 22. Command Line & Configuration

```
wake serve [--host 127.0.0.1] [--port 4318] [--database wake.db] [--max-spans 200000] [--idle 5]
wake demo  [--host] [--port] [--count 300] [--batch 256] [--seed N]
wake flame [--database wake.db] [--service NAME] [--limit 500]      # folded stacks to stdout
```

| Setting | Default | Meaning |
|---|---|---|
| `--port` | 4318 | the standard OTLP/HTTP port |
| `--max-spans` | 200,000 | in-memory span budget |
| `--idle` | 5 s | quiet time before a trace is stored |
| `WAKE_PURE_PROTOBUF=1` | off | force the hand-written decoder |

---

## 23. Testing

| Area | Tests |
|---|---:|
| OTLP decoding against the official SDK, both decoders, both encodings | 26 |
| Protobuf wire format | 20 |
| Stitching and repairs | 19 |
| Buffer and storage | 17 |
| Flame charts, flame graphs, folded stacks | 16 |
| HTTP, including the real exporter end to end | 10 |

**The compatibility claim:** a test runs two services sharing one distributed trace through the
official OpenTelemetry SDK and HTTP exporter against a live Wake, and asserts they come back stitched
into a single tree.

```bash
make test
```

---

## 24. Benchmarks & Results

Intel Core i5-11320H, Python 3.12, laptop SSD. 200,000 spans of synthetic shop traffic in batches of
512. Reproduce with `make bench`.

### Always say accepted or stored
| Protobuf decoder | Accepted over HTTP | **Stored on disk, sustained** |
|---|---:|---:|
| Official library | 59,911 spans/s | **14,646 spans/s** |
| Hand-written | 34,881 spans/s | **12,516 spans/s** |

**Stored is the real capacity.** Accepting faster than storing only fills the buffer until traces are
forced out early. The two differ because decoding and storage share one Python interpreter.

### Decoder speed alone
| Decoder | Spans/s |
|---|---:|
| Official library, parsing only | 4,286,249 |
| Official library, parsing plus building spans | 122,267 |
| JSON, standard library | 80,637 |
| **Protobuf, hand-written** | **47,931** |

Protobuf is 3.6× smaller on the wire, but in pure Python it is slower to decode than JSON, because
Python's JSON parser is in C.

### Storage, one profile at a time
| Change | In memory | On disk |
|---|---:|---:|
| a transaction and a row per span | 1,941 traces/s | not measured |
| one transaction per batch; no read-back for new traces | 3,725 | 2,000 |
| one row per trace, spans in the last column | 4,765 | 2,901 |
| compact span rows; summaries skip self time | **7,485** | **5,000** |

The database shrank from 72.7 MB to 34.9 MB with the compact form.

### Memory
About 707 bytes per buffered span with `__slots__`, 747 without. Slots save only about 5%; what a span
really costs is its id strings and attribute dictionary.

---

## 25. Deliberately Not Built

| Feature | Why not |
|---|---|
| gRPC transport | needs HTTP/2 and a dependency |
| storage in its own process | the next throughput ceiling (section 24) |
| tail sampling and retention | every trace kept until the database is deleted |
| metrics and logs | traces only |
| authentication | bind to localhost or put a proxy in front |

---

## 26. Troubleshooting

### No traces appear
Traces are stored only after going quiet for `--idle` seconds. Wait, or lower `--idle`. Check
`/api/stats` for spans in the buffer.

### My exporter gets 415
Use `application/x-protobuf` or `application/json`.

### Two services show as separate traces
The trace context is not propagated, so their trace ids differ. Use the SDK's HTTP instrumentation.

### A trace is marked flushed early
The buffer hit `--max-spans`. Raise it if memory allows.

### Port 4318 is in use
Another collector is running. Use `--port` and point the exporter at it.

---

## 27. Complete Feature Summary

### All Features Built

| # | Feature | Type | Key Files |
|---|---|---|---|
| 1 | Hand-written protobuf reader | Protocol | `protowire.py` |
| 2 | OTLP protobuf and JSON decoding | Protocol | `otlp.py` |
| 3 | Automatic fast decoder | Performance | `otlp_fast.py` |
| 4 | Duplicate-aware trace buffer with budget | Storage | `store.py` |
| 5 | SQLite storage, one row per trace | Storage | `store.py` |
| 6 | Late spans merged into stored traces | Storage | `store.py` |
| 7 | Stitching with orphans and loop breaking | Analysis | `assemble.py` |
| 8 | Cross-service clock skew repair | Analysis | `assemble.py` |
| 9 | Overlap-aware self time | Analysis | `assemble.py` |
| 10 | Flame charts with lanes | Visuals | `flame.py` |
| 11 | Flame graphs and folded stacks | Visuals | `flame.py` |
| 12 | Accessible service palette | Visuals | `flame.py` |
| 13 | Server-rendered pages and JSON API | Web | `pages.py`, `server.py` |
| 14 | Synthetic multi-service demo | Tooling | `demo.py`, `encode.py` |
| 15 | Real-SDK compatibility tests | Testing | `tests/` |
| 16 | Ingest and memory benchmarks | Tooling | `bench/` |

### Data Flow Architecture

```
Instrumented services (OpenTelemetry SDK)
  └── POST /v1/traces (protobuf or JSON, gzip) ──► server.py
        ├── 413 / 415 / 400 checks
        └── otlp.decode() ──► protowire or otlp_fast or JSON ──► Span[]
              └── Collector.ingest() ──► TraceBuffer (dedupe, LRU budget)

Background flusher
  └── due() (idle traces) + evicted (over budget) ──► TraceStore.save_many()
        └── SQLite: traces row (summary + spans JSON last) + trace_services

Browser
  ├── GET /                ──► search + aggregate() ──► flame_graph SVG
  └── GET /traces/{id}     ──► assemble() (dedupe, orphans, skew, self time)
                                └── lanes() ──► flame_chart SVG + repairs list
```

### Tech Stack at a Glance

```
Language:   Python 3.10+ (no runtime dependencies)
Protocol:   OTLP/HTTP, protobuf (hand-written or official library), JSON
Storage:    SQLite in WAL mode
Visuals:    server-rendered HTML, hand-drawn SVG flame charts and graphs
Testing:    pytest, official OpenTelemetry SDK and exporter end to end
```
