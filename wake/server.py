"""The HTTP side: OTLP ingest, the query API, and the pages.

Standard library only. `ThreadingHTTPServer` gives one thread per request, which
suits a collector well enough: ingest is mostly decoding, the store serialises
its own writes, and a background thread flushes quiet traces to SQLite.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import otlp, pages
from .assemble import assemble
from .flame import aggregate, flame_chart, flame_graph, folded
from .store import Collector

log = logging.getLogger("wake")

MAX_BODY = 16 * 1024 * 1024


class WakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, collector: Collector, *, flush_interval_s: float = 1.0):
        super().__init__(address, Handler)
        self.collector = collector
        self.flush_interval_s = flush_interval_s
        self.ingest_errors = 0
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, name="wake-flush", daemon=True)

    def start_background(self) -> None:
        self._flusher.start()

    def _flush_loop(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            try:
                self.collector.flush_due()
            except Exception:  # noqa: BLE001 - the flusher must survive a bad trace
                log.exception("flush failed")

    def shutdown(self) -> None:
        self._stop.set()
        super().shutdown()
        self.collector.flush_due(everything=True)


class Handler(BaseHTTPRequestHandler):
    server: WakeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    # ------------------------------------------------------------------ ingest

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path != "/v1/traces":
            self._error(404, "the OTLP traces endpoint is POST /v1/traces")
            return

        length = self.headers.get("Content-Length")
        if length is None:
            self._error(411, "Content-Length is required")
            return
        size = int(length)
        if size > MAX_BODY:
            self._error(413, f"request of {size} bytes exceeds the {MAX_BODY} byte limit")
            self.close_connection = True
            return
        payload = self.rfile.read(size)

        if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                payload = gzip.decompress(payload)
            except OSError:
                self._error(400, "Content-Encoding says gzip but the body is not gzip")
                return

        content_type = self.headers.get("Content-Type", "")
        try:
            spans = otlp.decode(payload, content_type)
        except otlp.DecodeError as exc:
            self.server.ingest_errors += 1
            self.server.collector.rejected_batches += 1
            status = 415 if "unsupported content type" in str(exc) else 400
            self._error(status, str(exc))
            return

        self.server.collector.ingest(spans)
        # An empty ExportTraceServiceResponse means full success, in either encoding.
        if content_type.lower().startswith("application/json"):
            self._send(200, b"{}", "application/json")
        else:
            self._send(200, b"", "application/x-protobuf")

    # ------------------------------------------------------------------- reads

    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parts.query)
        collector = self.server.collector

        def arg(name: str, default=None):
            return query.get(name, [default])[0]

        try:
            if path == "/":
                service = arg("service") or None
                traces = collector.search(service=service, limit=int(arg("limit", 60)),
                                          min_duration_ns=int(float(arg("min_ms", 0)) * 1e6),
                                          errors_only=arg("errors") == "1")
                body = pages.index(traces, collector.store.services(), collector.stats(),
                                   service=service, errors_only=arg("errors") == "1")
                self._send(200, body.encode(), "text/html; charset=utf-8")

            elif path == "/api/traces":
                traces = collector.search(
                    service=arg("service") or None, limit=min(int(arg("limit", 50)), 1000),
                    min_duration_ns=int(float(arg("min_ms", 0)) * 1e6),
                    errors_only=arg("errors") == "1")
                self._json({"traces": traces})

            elif path.startswith("/api/traces/"):
                tree = self._tree(path.rsplit("/", 1)[1])
                if tree:
                    self._json(tree.to_dict())

            elif path.startswith("/traces/") and path.endswith("/flame.svg"):
                tree = self._tree(path.split("/")[2])
                if tree:
                    self._send(200, flame_chart(tree).encode(), "image/svg+xml")

            elif path.startswith("/traces/"):
                tree = self._tree(path.split("/")[2])
                if tree:
                    self._send(200, pages.trace(tree).encode(), "text/html; charset=utf-8")

            elif path in ("/flame.svg", "/flame.folded"):
                service = arg("service") or None
                summaries = collector.search(service=service, limit=min(int(arg("limit", 200)), 2000))
                trees = [assemble(spans) for s in summaries if (spans := collector.trace(s["trace_id"]))]
                merged = aggregate(trees)
                if path.endswith(".svg"):
                    title = f"Where the time goes{' in ' + service if service else ''}"
                    self._send(200, flame_graph(merged, title=title).encode(), "image/svg+xml")
                else:
                    self._send(200, folded(merged).encode(), "text/plain; charset=utf-8")

            elif path == "/api/services":
                self._json({"services": collector.store.services()})

            elif path == "/api/stats":
                stats = collector.stats()
                stats["stored_traces"] = collector.store.count()
                stats["ingest_errors"] = self.server.ingest_errors
                stats["protobuf_decoder"] = otlp.protobuf_decoder()[1]
                self._json(stats)

            elif path == "/health":
                self._json({"status": "ok"})

            else:
                self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))

    def _tree(self, trace_id: str):
        trace_id = trace_id.lower()
        if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
            self._error(400, "a trace id is 32 hex characters")
            return None
        spans = self.server.collector.trace(trace_id)
        if not spans:
            self._error(404, f"no trace {trace_id}")
            return None
        return assemble(spans)

    # ---------------------------------------------------------------- replies

    def _json(self, value) -> None:
        self._send(200, json.dumps(value, separators=(",", ":")).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 4318, *, database: str = "wake.db",
          max_spans: int = 200_000, idle_s: float = 5.0) -> WakeServer:
    from .store import TraceStore
    collector = Collector(TraceStore(database), max_spans=max_spans, idle_s=idle_s)
    server = WakeServer((host, port), collector)
    server.start_background()
    return server


def now_ns() -> int:
    return time.time_ns()
