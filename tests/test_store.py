"""The buffer, the SQLite store, and the collector over both."""

from __future__ import annotations

from wake.store import Collector, TraceBuffer, TraceStore

from .conftest import span

MS = 1_000_000
T1 = "1" * 32
T2 = "2" * 32
T3 = "3" * 32


def test_the_buffer_drops_duplicate_spans(clock):
    buffer = TraceBuffer(clock=clock)
    buffer.add([span("a"), span("a"), span("b", "a")])
    assert buffer.span_count == 2
    assert buffer.duplicates == 1


def test_a_trace_is_only_due_once_it_has_gone_quiet(clock):
    buffer = TraceBuffer(idle_s=5, clock=clock)
    buffer.add([span("a", trace=T1)])
    clock.now += 4
    assert buffer.due() == []
    buffer.add([span("b", "a", trace=T1)])       # new activity resets the wait
    clock.now += 4
    assert buffer.due() == []
    clock.now += 2
    (trace_id, spans), = buffer.due()
    assert trace_id == T1 and len(spans) == 2
    assert buffer.span_count == 0


def test_the_span_budget_evicts_the_least_recently_touched_trace(clock):
    buffer = TraceBuffer(max_spans=4, clock=clock)
    buffer.add([span("a", trace=T1), span("b", trace=T1)])
    assert buffer.add([span("c", trace=T2), span("d", trace=T2)]) == []   # exactly at budget
    # A new span for T1 makes T1 the most recently touched and pushes the buffer
    # over budget, so T2, now the least recent, is the one evicted even though T1
    # arrived first.
    evicted = buffer.add([span("e", trace=T1)])
    assert [trace_id for trace_id, _ in evicted] == [T2]
    assert buffer.span_count == 3
    assert list(buffer.traces) == [T1]


def test_flushed_traces_are_searchable():
    store = TraceStore()
    store.save(T1, [span("root", trace=T1, service="frontend", name="GET /", end=30 * MS),
                    span("db", "root", trace=T1, service="postgres", start=MS, end=5 * MS)])
    (row,) = store.search()
    assert row["root_service"] == "frontend" and row["root_name"] == "GET /"
    assert row["duration_ms"] == 30.0 and row["span_count"] == 2
    assert set(row["services"]) == {"frontend", "postgres"}


def test_search_filters_by_any_service_in_the_trace():
    store = TraceStore()
    store.save(T1, [span("a", trace=T1, service="frontend"), span("b", "a", trace=T1, service="payments")])
    store.save(T2, [span("c", trace=T2, service="frontend")])
    assert [r["trace_id"] for r in store.search(service="payments")] == [T1]
    assert len(store.search(service="frontend")) == 2


def test_search_filters_by_duration_and_errors():
    store = TraceStore()
    store.save(T1, [span("a", trace=T1, end=100 * MS)])
    store.save(T2, [span("b", trace=T2, end=5 * MS, status=2)])
    assert [r["trace_id"] for r in store.search(min_duration_ns=50 * MS)] == [T1]
    assert [r["trace_id"] for r in store.search(errors_only=True)] == [T2]


def test_a_late_span_merges_into_the_stored_trace():
    """Nothing marks the last span of a trace. One that arrives after its trace
    was flushed must extend the stored trace, not replace it."""
    store = TraceStore()
    store.save(T1, [span("root", trace=T1, end=10 * MS)])
    store.save(T1, [span("late", "root", trace=T1, start=8 * MS, end=25 * MS)])
    (row,) = store.search()
    assert row["span_count"] == 2
    assert len(store.get(T1)) == 2


def test_an_early_flush_stays_flagged_after_a_merge():
    store = TraceStore()
    store.save(T1, [span("a", trace=T1)], early=True)
    store.save(T1, [span("b", "a", trace=T1)])
    assert store.search()[0]["flushed_early"] is True


def test_the_store_survives_reopening(tmp_path):
    path = str(tmp_path / "wake.db")
    first = TraceStore(path)
    first.save(T1, [span("a", trace=T1, service="frontend")])
    first.close()
    second = TraceStore(path)
    assert second.count() == 1
    assert second.services() == ["frontend"]


def test_the_collector_serves_in_flight_and_stored_traces_together(clock):
    collector = Collector(TraceStore(), idle_s=5, clock=clock)
    collector.ingest([span("a", trace=T1, start=1, end=MS)])
    clock.now += 10
    collector.flush_due()
    collector.ingest([span("b", trace=T2, start=2, end=MS)])

    found = {r["trace_id"]: r for r in collector.search()}
    assert found[T1]["in_flight"] is False
    assert found[T2]["in_flight"] is True


def test_a_trace_is_readable_while_split_across_buffer_and_store(clock):
    collector = Collector(TraceStore(), idle_s=5, clock=clock)
    collector.ingest([span("root", trace=T1, end=10 * MS)])
    clock.now += 10
    collector.flush_due()
    collector.ingest([span("late", "root", trace=T1, start=MS, end=2 * MS)])
    assert {s.span_id[-4:] for s in collector.trace(T1)} == {"root", "late"}


def test_budget_evictions_are_stored_and_counted(clock):
    collector = Collector(TraceStore(), max_spans=2, clock=clock)
    collector.ingest([span("a", trace=T1), span("b", trace=T1)])
    collector.ingest([span("c", trace=T2)])
    stats = collector.stats()
    assert stats["flushed_early"] == 1
    assert collector.store.count() == 1


def test_draining_on_shutdown_stores_everything(clock):
    collector = Collector(TraceStore(), idle_s=600, clock=clock)
    collector.ingest([span("a", trace=T1), span("b", trace=T2)])
    assert collector.flush_due(everything=True) == 2
    assert collector.store.count() == 2


def test_a_batch_stores_new_traces_and_merges_existing_ones_together():
    store = TraceStore()
    store.save(T1, [span("root", trace=T1, end=10 * MS)])
    store.save_many([
        (T1, [span("late", "root", trace=T1, start=MS, end=30 * MS)], False),
        (T2, [span("fresh", trace=T2, end=5 * MS)], False),
    ])
    rows = {r["trace_id"]: r for r in store.search()}
    assert rows[T1]["span_count"] == 2, "the late span did not merge into the stored trace"
    assert rows[T2]["span_count"] == 1


def test_a_batch_is_one_transaction(monkeypatch):
    """Either every trace in a flush lands or none does."""
    store = TraceStore()
    real = store._existing

    def fail_after_insert(ids):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(store, "_existing", fail_after_insert)
    try:
        store.save_many([(T1, [span("a", trace=T1)], False), (T2, [span("b", trace=T2)], False)])
    except RuntimeError:
        pass
    monkeypatch.setattr(store, "_existing", real)
    assert store.count() == 0


def test_the_compact_stored_form_round_trips_every_field():
    from wake.model import Event
    original = span("a", "b", service="svc", name="n", start=5, end=9, status=2, k=[1, {"x": True}])
    original.status_message = "boom"
    original.kind = 3
    original.events.append(Event("e", 7, {"n": -1}))
    from wake.model import Span
    back = Span.from_row(original.trace_id, original.to_row())
    assert back.to_dict() == original.to_dict()


def test_stored_traces_come_back_with_their_self_times():
    """The store skips self time when summarising. Reading a trace back must still
    produce it, or every flame chart of stored data would show zero self time."""
    from wake.assemble import assemble
    store = TraceStore()
    store.save(T1, [span("p", trace=T1, end=10 * MS), span("c", "p", trace=T1, start=MS, end=4 * MS)])
    tree = assemble(store.get(T1))
    assert tree.roots[0].self_ns == 7 * MS
