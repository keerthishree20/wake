"""Stitching spans into a tree, and the repairs made along the way."""

from __future__ import annotations

import random

import pytest

from wake.assemble import assemble

from .conftest import span

MS = 1_000_000


def test_out_of_order_spans_become_one_tree():
    spans = [span("c", "b", start=2 * MS, end=3 * MS), span("a", start=0, end=10 * MS),
             span("b", "a", start=1 * MS, end=5 * MS)]
    random.Random(1).shuffle(spans)
    tree = assemble(spans)
    assert len(tree.roots) == 1
    root = tree.roots[0]
    assert root.span.span_id.endswith("a")
    assert root.children[0].span.span_id.endswith("b")
    assert root.children[0].children[0].span.span_id.endswith("c")
    assert tree.depth == 3
    assert tree.duration_ns == 10 * MS


def test_children_are_ordered_by_start_time():
    tree = assemble([span("root", end=10 * MS), span("late", "root", start=6 * MS, end=7 * MS),
                     span("early", "root", start=1 * MS, end=2 * MS)])
    assert [c.span.name for c in tree.roots[0].children] == ["op-early", "op-late"]


def test_duplicates_are_counted_and_ignored():
    """Exporters retry whole batches."""
    first = span("a", end=5 * MS, name="first copy")
    tree = assemble([first, span("a", end=9 * MS, name="retry"), span("b", "a")])
    assert len(tree.nodes) == 2
    assert tree.duplicates == 1
    assert tree.roots[0].span.name == "first copy"


def test_a_span_whose_parent_never_arrived_is_kept_and_marked():
    """Dropping it would hide exactly the spans from the service whose exporter
    is misbehaving."""
    tree = assemble([span("root", end=10 * MS), span("lost-child", "missing", start=MS, end=2 * MS)])
    assert tree.orphans == 1
    orphan = next(n for n in tree.roots if n.orphan)
    assert orphan.span.span_id.endswith("lost-child")
    assert tree.root.span.span_id.endswith("root"), "the real root is still preferred"


def test_a_parent_cycle_does_not_lose_spans_or_loop_forever():
    tree = assemble([span("a", "b"), span("b", "a")])
    assert len(tree.nodes) == 2
    assert tree.orphans >= 1


def test_a_span_that_is_its_own_parent_is_an_orphan():
    tree = assemble([span("a", "a")])
    assert tree.orphans == 1 and len(tree.nodes) == 1


def test_spans_from_two_traces_are_refused():
    with pytest.raises(ValueError, match="more than one trace"):
        assemble([span("a"), span("b", trace="f" * 32)])


def test_no_spans_is_an_error():
    with pytest.raises(ValueError):
        assemble([])


# ---------------------------------------------------------------- clock skew

def test_a_child_from_another_service_that_starts_early_is_shifted():
    """frontend calls payments; payments' clock runs behind, so its span appears
    to start before the call that caused it."""
    tree = assemble([
        span("front", service="frontend", start=10 * MS, end=40 * MS),
        span("pay", "front", service="payments", start=7 * MS, end=17 * MS),
    ])
    pay = tree.roots[0].children[0]
    assert pay.shift_ns == 3 * MS
    assert pay.start_ns == 10 * MS
    assert pay.end_ns - pay.start_ns == 10 * MS, "shifting must not change the duration"
    assert tree.skew_adjusted == 1


def test_the_shift_carries_down_to_the_whole_subtree():
    tree = assemble([
        span("front", service="frontend", start=10 * MS, end=40 * MS),
        span("pay", "front", service="payments", start=7 * MS, end=17 * MS),
        span("db", "pay", service="payments", start=8 * MS, end=9 * MS),
    ])
    db = tree.roots[0].children[0].children[0]
    assert db.shift_ns == 3 * MS
    assert db.start_ns == 11 * MS


def test_skew_within_one_service_is_left_visible():
    """One clock cannot disagree with itself. A child starting before its parent
    in the same service is a real bug and should look like one."""
    tree = assemble([span("p", service="api", start=10 * MS, end=20 * MS),
                     span("c", "p", service="api", start=8 * MS, end=12 * MS)])
    assert tree.roots[0].children[0].shift_ns == 0
    assert tree.skew_adjusted == 0


def test_a_child_that_starts_inside_its_parent_is_not_touched():
    tree = assemble([span("p", service="a", start=0, end=10 * MS),
                     span("c", "p", service="b", start=2 * MS, end=4 * MS)])
    assert tree.roots[0].children[0].shift_ns == 0


# ------------------------------------------------------------------ self time

def test_self_time_subtracts_children():
    tree = assemble([span("p", end=10 * MS), span("c1", "p", start=1 * MS, end=3 * MS),
                     span("c2", "p", start=5 * MS, end=9 * MS)])
    assert tree.roots[0].self_ns == 4 * MS


def test_parallel_children_are_not_double_subtracted():
    """Two children running at the same time cover 4 ms of the parent, not 8.
    Summing their durations would give the parent negative self time."""
    tree = assemble([span("p", end=10 * MS), span("c1", "p", start=2 * MS, end=6 * MS),
                     span("c2", "p", start=2 * MS, end=6 * MS)])
    assert tree.roots[0].self_ns == 6 * MS


def test_partly_overlapping_children_use_their_union():
    tree = assemble([span("p", end=10 * MS), span("c1", "p", start=1 * MS, end=5 * MS),
                     span("c2", "p", start=3 * MS, end=8 * MS)])
    assert tree.roots[0].self_ns == 3 * MS      # 10 - union[1, 8]


def test_a_child_overrunning_its_parent_is_clipped_and_self_time_is_never_negative():
    tree = assemble([span("p", end=5 * MS), span("c", "p", start=1 * MS, end=50 * MS)])
    assert tree.roots[0].self_ns == 1 * MS


def test_a_leaf_owns_all_of_its_time():
    tree = assemble([span("p", end=10 * MS), span("leaf", "p", start=MS, end=4 * MS)])
    assert tree.roots[0].children[0].self_ns == 3 * MS


# --------------------------------------------------------------------- shape

def test_a_very_deep_trace_does_not_hit_the_recursion_limit():
    spans = [span("s0", end=10**9)]
    for i in range(1, 5000):
        spans.append(span(f"s{i}", f"s{i - 1}", start=i, end=10**9 - i))
    tree = assemble(spans)
    assert tree.depth == 5000
    assert len(tree.nodes) == 5000


def test_the_json_form_nests_children_and_reports_repairs():
    body = assemble([span("a", end=10 * MS), span("b", "a", start=MS, end=2 * MS),
                     span("x", "gone")]).to_dict()
    assert body["span_count"] == 3
    assert body["repairs"]["orphans"] == 1
    top = next(r for r in body["roots"] if not r["orphan"])
    assert top["children"][0]["name"] == "op-b"
    assert top["children"][0]["offset_ms"] == 1.0
