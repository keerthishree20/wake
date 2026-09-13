"""Flame charts, flame graphs and folded stacks."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from wake.assemble import assemble
from wake.flame import SERVICE_SLOTS, aggregate, flame_chart, flame_graph, folded

from .conftest import span

MS = 1_000_000
SVG = "{http://www.w3.org/2000/svg}"


def bars(svg: str) -> list[ET.Element]:
    root = ET.fromstring(svg)
    return [g for g in root.iter(f"{SVG}g") if g.get("class") == "hit"]


def main_rect(group: ET.Element) -> ET.Element:
    return next(r for r in group.iter(f"{SVG}rect") if r.get("class") == "bar")


def sample_tree():
    return assemble([
        span("root", service="frontend", name="GET /", start=0, end=100 * MS),
        span("a", "root", service="catalog", name="list", start=10 * MS, end=60 * MS),
        span("b", "a", service="postgres", name="SELECT", start=20 * MS, end=30 * MS),
        span("c", "root", service="payments", name="charge", start=70 * MS, end=90 * MS, status=2),
    ])


def test_the_flame_chart_is_valid_svg_with_one_bar_per_span():
    svg = flame_chart(sample_tree())
    assert len(bars(svg)) == 4


def test_bar_position_and_width_follow_time():
    """One scale places every bar: offset and width are proportional to time."""
    svg = flame_chart(sample_tree(), width=1024)
    by_title = {g.find(f"{SVG}title").text.split("\n")[0]: main_rect(g) for g in bars(svg)}
    root = by_title["frontend · GET /"]
    list_bar = by_title["catalog · list"]
    inner = float(root.get("width"))
    left = float(root.get("x"))
    assert abs(float(list_bar.get("x")) - (left + 0.10 * inner)) < 1
    assert abs(float(list_bar.get("width")) - 0.50 * inner) < 1


def test_depth_sets_the_row():
    svg = flame_chart(sample_tree())
    rows = {g.find(f"{SVG}title").text.split("\n")[0]: float(main_rect(g).get("y")) for g in bars(svg)}
    assert rows["frontend · GET /"] < rows["catalog · list"] < rows["postgres · SELECT"]
    assert rows["catalog · list"] == rows["payments · charge"]


def test_error_spans_are_outlined_and_say_so():
    svg = flame_chart(sample_tree())
    errored = [g for g in bars(svg) if "error" in g.find(f"{SVG}title").text]
    assert len(errored) == 1
    assert any(r.get("class") == "err" for r in errored[0].iter(f"{SVG}rect"))


def test_tooltips_carry_duration_and_self_time():
    svg = flame_chart(sample_tree())
    title = next(g.find(f"{SVG}title").text for g in bars(svg)
                 if g.find(f"{SVG}title").text.startswith("catalog"))
    assert "duration 50ms" in title and "self 40ms" in title


def test_repairs_show_up_in_the_tooltip():
    tree = assemble([span("p", service="a", start=10 * MS, end=20 * MS),
                     span("c", "p", service="b", start=8 * MS, end=12 * MS)])
    titles = " ".join(g.find(f"{SVG}title").text for g in bars(flame_chart(tree)))
    assert "clock skew corrected" in titles


def test_services_beyond_the_palette_fold_into_one_grey():
    spans = [span("root", service="s0", end=100 * MS)]
    for i in range(1, 10):
        spans.append(span(f"x{i}", "root", service=f"s{i}", start=i * MS, end=(i + 1) * MS))
    svg = flame_chart(assemble(spans))
    fills = {main_rect(g).get("fill") for g in bars(svg)}
    assert "var(--other)" in fills
    assert len([f for f in fills if f.startswith("var(--s")]) == len(SERVICE_SLOTS)
    assert "3 more" in svg


def test_names_are_escaped():
    tree = assemble([span("r", service="<svc>", name="GET /a?b=1&c=<2>", end=MS)])
    ET.fromstring(flame_chart(tree))          # would raise on unescaped markup


def test_the_chart_adapts_to_dark_mode():
    assert "prefers-color-scheme:dark" in flame_chart(sample_tree())


# --------------------------------------------------------------- flame graph

def test_identical_paths_merge_across_traces():
    trees = []
    for n in range(3):
        trace = str(n + 1) * 32
        trees.append(assemble([
            span("r", service="frontend", name="GET /", end=10 * MS, trace=trace),
            span("q", "r", service="db", name="query", start=MS, end=5 * MS, trace=trace),
        ]))
    merged = aggregate(trees)
    assert merged.samples == 3
    (front,) = merged.children.values()
    assert front.total_ns == 30 * MS and front.samples == 3
    (db,) = front.children.values()
    assert db.total_ns == 12 * MS
    assert front.self_ns == 18 * MS


def test_folded_stacks_use_self_time_in_microseconds():
    merged = aggregate([sample_tree()])
    lines = dict(line.rsplit(" ", 1) for line in folded(merged).strip().splitlines())
    assert lines["frontend:GET_/"] == str(30 * 1000)            # 100 - (50 + 20) ms
    assert lines["frontend:GET_/;catalog:list"] == str(40 * 1000)
    assert lines["frontend:GET_/;catalog:list;postgres:SELECT"] == str(10 * 1000)


def test_folded_labels_cannot_break_the_format():
    tree = assemble([span("r", service="a;b", name="x y;z", end=MS)])
    (line,) = folded(aggregate([tree])).strip().splitlines()
    stack, count = line.rsplit(" ", 1)
    assert ";" not in stack and " " not in stack and count.isdigit()


def test_the_flame_graph_is_valid_svg_sized_by_total_time():
    svg = flame_graph(aggregate([sample_tree()]), width=1024)
    widths = {g.find(f"{SVG}title").text.split("\n")[0]: float(main_rect(g).get("width"))
              for g in bars(svg)}
    assert abs(widths["catalog · list"] / widths["frontend · GET /"] - 0.5) < 0.01


def test_an_empty_aggregate_still_renders():
    ET.fromstring(flame_graph(aggregate([])))
    assert folded(aggregate([])) == ""


def test_parallel_siblings_get_their_own_lanes():
    """Found by looking at the rendered chart: two calls made in parallel sat on
    one row and one bar covered the other."""
    tree = assemble([
        span("root", service="frontend", end=100 * MS),
        span("inv", "root", service="inventory", start=20 * MS, end=40 * MS),
        span("price", "root", service="pricing", start=20 * MS, end=35 * MS),
        span("later", "root", service="payments", start=50 * MS, end=60 * MS),
    ])
    rects = {}
    for g in bars(flame_chart(tree)):
        name = g.find(f"{SVG}title").text.split("\n")[0]
        r = main_rect(g)
        rects[name] = (float(r.get("x")), float(r.get("x")) + float(r.get("width")), float(r.get("y")))

    def overlaps(a, b):
        return a[2] == b[2] and a[0] < b[1] and b[0] < a[1]

    names = list(rects)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not overlaps(rects[a], rects[b]), f"{a} and {b} overlap on one row"
    assert rects["payments · op-later"][2] == rects["inventory · op-inv"][2], (
        "a sibling that does not overlap should reuse the first lane")


def test_children_always_sit_below_their_parent():
    tree = sample_tree()
    rows = {}
    for g in bars(flame_chart(tree)):
        rows[g.find(f"{SVG}title").text.split("\n")[0]] = float(main_rect(g).get("y"))
    assert rows["frontend · GET /"] < rows["catalog · list"] < rows["postgres · SELECT"]
