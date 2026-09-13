"""Where spans wait, and where traces end up.

Two tiers, because a trace is never finished in any way the collector can see.
Nothing marks the last span of a request. A trace is only considered done when
it has gone quiet.

**The buffer** holds traces that are still receiving spans, in memory, keyed by
trace id and ordered by when each last changed. A trace that has had no new span
for `idle_s` is flushed. So is the least recently touched trace whenever the
buffer passes its span budget, which is what keeps memory bounded when traffic
outruns the idle timeout; those early flushes are counted, since they are the
traces most likely to be missing a late span.

**The store** is SQLite. Each flushed trace is one row: summary columns for
searching, and its spans as a JSON array in the last column. A span that arrives after its trace was flushed
is not lost: it goes back through the buffer and is merged into the stored trace
on the next flush, and the summary is recomputed from everything held.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .assemble import assemble
from .model import Span

SCHEMA = """
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
    -- Every span of the trace as one JSON array. Last on purpose: SQLite keeps
    -- the columns before it on the row's first page, so searching the summary
    -- columns never touches the overflow pages the spans spill into.
    spans         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_by_start    ON traces (start_ns DESC);
CREATE INDEX IF NOT EXISTS traces_by_duration ON traces (duration_ns DESC);

CREATE TABLE IF NOT EXISTS trace_services (
    service  TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    PRIMARY KEY (service, trace_id)
);
"""


@dataclass
class BufferedTrace:
    spans: dict[str, Span] = field(default_factory=dict)   # span id -> span
    last_update: float = 0.0


class TraceBuffer:
    def __init__(self, *, max_spans: int = 200_000, idle_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_spans = max_spans
        self.idle_s = idle_s
        self.clock = clock
        self.traces: collections.OrderedDict[str, BufferedTrace] = collections.OrderedDict()
        self.span_count = 0
        self.duplicates = 0

    def add(self, spans: Iterable[Span]) -> list[tuple[str, list[Span]]]:
        """Buffer spans. Returns traces pushed out early to stay within budget."""
        now = self.clock()
        for span in spans:
            entry = self.traces.get(span.trace_id)
            if entry is None:
                entry = self.traces[span.trace_id] = BufferedTrace()
            if span.span_id in entry.spans:
                self.duplicates += 1
                continue
            entry.spans[span.span_id] = span
            entry.last_update = now
            self.traces.move_to_end(span.trace_id)
            self.span_count += 1

        evicted: list[tuple[str, list[Span]]] = []
        while self.span_count > self.max_spans and self.traces:
            trace_id, entry = self.traces.popitem(last=False)
            self.span_count -= len(entry.spans)
            evicted.append((trace_id, list(entry.spans.values())))
        return evicted

    def due(self) -> list[tuple[str, list[Span]]]:
        """Traces that have been quiet for idle_s. Oldest first, so this stops
        at the first trace that is still active."""
        cutoff = self.clock() - self.idle_s
        ready: list[tuple[str, list[Span]]] = []
        while self.traces:
            trace_id, entry = next(iter(self.traces.items()))
            if entry.last_update > cutoff:
                break
            self.traces.popitem(last=False)
            self.span_count -= len(entry.spans)
            ready.append((trace_id, list(entry.spans.values())))
        return ready

    def drain(self) -> list[tuple[str, list[Span]]]:
        everything = [(tid, list(e.spans.values())) for tid, e in self.traces.items()]
        self.traces.clear()
        self.span_count = 0
        return everything

    def get(self, trace_id: str) -> list[Span]:
        entry = self.traces.get(trace_id)
        return list(entry.spans.values()) if entry else []

    def __len__(self) -> int:
        return len(self.traces)


class TraceStore:
    def __init__(self, path: str = ":memory:"):
        # One connection shared under a lock. SQLite serialises writers anyway,
        # and a single connection keeps an in-memory database visible to every
        # thread that uses the store.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")

    def save(self, trace_id: str, spans: list[Span], *, early: bool = False) -> None:
        self.save_many([(trace_id, spans, early)])

    def save_many(self, items: list[tuple[str, list[Span], bool]]) -> None:
        """Store a batch of traces in one transaction, one row per trace.

        Profiling drove every choice here. The first version wrote a row per span
        and a transaction per trace, and flushed about 1,900 traces a second to
        disk while ingest accepted 34,000 spans a second, so storage, not HTTP,
        was the real ceiling. Committing dominated: each span row was another
        insert into a B-tree keyed on random ids. Now a whole trace is one row,
        a batch is one transaction, and a trace that was never stored before is
        summarised from the spans in hand without reading anything back.
        """
        if not items:
            return
        with self.lock, self.db:
            existing = self._existing([trace_id for trace_id, _, _ in items])
            rows = []
            memberships = []
            for trace_id, spans, early in items:
                if trace_id in existing:
                    # A late span: merge into what is stored. The stored copy of
                    # any span wins, since a retry resends what already landed.
                    merged = {s.span_id: s for s in spans}
                    merged.update({s.span_id: s for s in self._load(trace_id)})
                    spans = list(merged.values())
                else:
                    unique = {}
                    for s in spans:
                        unique.setdefault(s.span_id, s)
                    spans = list(unique.values())
                tree = assemble(spans, self_time=False)
                root = tree.root
                rows.append((
                    trace_id, root.span.service if root else None, root.span.name if root else None,
                    tree.start_ns, tree.duration_ns, len(tree.nodes), tree.errors,
                    json.dumps(tree.services), int(early),
                    json.dumps([s.to_row() for s in spans], separators=(",", ":")),
                ))
                memberships.extend((service, trace_id) for service in tree.services)

            self.db.executemany(
                """INSERT INTO traces (trace_id, root_service, root_name, start_ns, duration_ns,
                                       span_count, error_count, services, flushed_early, spans)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (trace_id) DO UPDATE SET
                       root_service = excluded.root_service, root_name = excluded.root_name,
                       start_ns = excluded.start_ns, duration_ns = excluded.duration_ns,
                       span_count = excluded.span_count, error_count = excluded.error_count,
                       services = excluded.services, spans = excluded.spans,
                       flushed_early = traces.flushed_early OR excluded.flushed_early""",
                rows)
            self.db.executemany(
                "INSERT OR IGNORE INTO trace_services (service, trace_id) VALUES (?, ?)",
                memberships)

    def _existing(self, trace_ids: list[str]) -> set[str]:
        found: set[str] = set()
        for offset in range(0, len(trace_ids), 500):   # well under SQLite's variable limit
            chunk = trace_ids[offset:offset + 500]
            rows = self.db.execute(
                f"SELECT trace_id FROM traces WHERE trace_id IN ({','.join('?' * len(chunk))})", chunk)
            found.update(row[0] for row in rows)
        return found

    def _load(self, trace_id: str) -> list[Span]:
        row = self.db.execute("SELECT spans FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
        return [Span.from_row(trace_id, raw) for raw in json.loads(row[0])] if row else []

    def get(self, trace_id: str) -> list[Span]:
        with self.lock:
            return self._load(trace_id)

    def search(self, *, service: str | None = None, min_duration_ns: int = 0,
               errors_only: bool = False, limit: int = 50) -> list[dict]:
        sql = ["SELECT t.trace_id, t.root_service, t.root_name, t.start_ns, t.duration_ns,"
               " t.span_count, t.error_count, t.services, t.flushed_early FROM traces t"]
        where, params = [], []
        if service:
            sql.append("JOIN trace_services s ON s.trace_id = t.trace_id AND s.service = ?")
            params.append(service)
        if min_duration_ns:
            where.append("t.duration_ns >= ?")
            params.append(min_duration_ns)
        if errors_only:
            where.append("t.error_count > 0")
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY t.start_ns DESC LIMIT ?")
        params.append(limit)
        with self.lock:
            rows = self.db.execute(" ".join(sql), params).fetchall()
        return [{
            "trace_id": r[0], "root_service": r[1], "root_name": r[2], "start_ns": r[3],
            "duration_ms": round(r[4] / 1e6, 3), "span_count": r[5], "errors": r[6],
            "services": json.loads(r[7]), "flushed_early": bool(r[8]), "in_flight": False,
        } for r in rows]

    def services(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT DISTINCT service FROM trace_services ORDER BY service")]

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT count(*) FROM traces").fetchone()[0]

    def close(self) -> None:
        with self.lock:
            self.db.close()


class Collector:
    """The buffer and the store behind one lock, plus the counters."""

    def __init__(self, store: TraceStore | None = None, *, max_spans: int = 200_000,
                 idle_s: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.store = store or TraceStore()
        self.buffer = TraceBuffer(max_spans=max_spans, idle_s=idle_s, clock=clock)
        self.lock = threading.Lock()
        self.batches = 0
        self.spans_received = 0
        self.rejected_batches = 0
        self.traces_flushed = 0
        self.flushed_early = 0

    def ingest(self, spans: list[Span]) -> int:
        with self.lock:
            self.batches += 1
            self.spans_received += len(spans)
            evicted = self.buffer.add(spans)
        self.store.save_many([(trace_id, trace_spans, True) for trace_id, trace_spans in evicted])
        with self.lock:
            self.flushed_early += len(evicted)
            self.traces_flushed += len(evicted)
        return len(spans)

    def flush_due(self, *, everything: bool = False) -> int:
        with self.lock:
            ready = self.buffer.drain() if everything else self.buffer.due()
        self.store.save_many([(trace_id, spans, False) for trace_id, spans in ready])
        with self.lock:
            self.traces_flushed += len(ready)
        return len(ready)

    def trace(self, trace_id: str) -> list[Span]:
        with self.lock:
            buffered = self.buffer.get(trace_id)
        stored = self.store.get(trace_id)
        merged = {s.span_id: s for s in stored}
        merged.update({s.span_id: s for s in buffered})
        return list(merged.values())

    def search(self, *, service: str | None = None, min_duration_ns: int = 0,
               errors_only: bool = False, limit: int = 50) -> list[dict]:
        """Stored traces plus any still in flight, newest first."""
        with self.lock:
            in_flight = [(tid, list(entry.spans.values()))
                         for tid, entry in reversed(self.buffer.traces.items())][:limit]
        results = []
        for trace_id, spans in in_flight:
            tree = assemble(spans)
            if service and service not in tree.services:
                continue
            if tree.duration_ns < min_duration_ns or (errors_only and not tree.errors):
                continue
            root = tree.root
            results.append({
                "trace_id": trace_id, "root_service": root.span.service,
                "root_name": root.span.name, "start_ns": tree.start_ns,
                "duration_ms": round(tree.duration_ns / 1e6, 3), "span_count": len(tree.nodes),
                "errors": tree.errors, "services": tree.services, "flushed_early": False,
                "in_flight": True,
            })
        stored_ids = {r["trace_id"] for r in results}
        results += [r for r in self.store.search(service=service, min_duration_ns=min_duration_ns,
                                                 errors_only=errors_only, limit=limit)
                    if r["trace_id"] not in stored_ids]
        results.sort(key=lambda r: r["start_ns"], reverse=True)
        return results[:limit]

    def stats(self) -> dict:
        with self.lock:
            return {
                "batches": self.batches,
                "spans_received": self.spans_received,
                "duplicate_spans": self.buffer.duplicates,
                "rejected_batches": self.rejected_batches,
                "buffered_traces": len(self.buffer),
                "buffered_spans": self.buffer.span_count,
                "buffer_span_budget": self.buffer.max_spans,
                "traces_flushed": self.traces_flushed,
                "flushed_early": self.flushed_early,
            }
