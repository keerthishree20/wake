"""Flame charts and flame graphs, as SVG with no script.

Two different pictures that often share a name, kept distinct here:

* A **flame chart** is one trace on a real time axis. Position is when each span
  ran, width is how long it took, depth is who called whom. It answers "what
  happened in this request".
* A **flame graph** merges many traces. Identical call paths collapse into one
  bar whose width is total time spent across all of them, children sorted by
  name rather than time. It answers "where does the time go in general", and it
  is what Brendan Gregg's format describes. The same data is also available as
  folded stacks, the text format his tools read.

Colour is identity: each service gets a fixed slot from a palette checked for
colour-vision separation, assigned in order of first appearance and never
reused. Past seven services the rest fold into neutral grey rather than
generating new hues that nobody can tell apart. Colour is never the only
signal: every bar that fits carries its name, every bar has a hover tooltip, and
the legend names the services. Errors are marked with an outline and a label,
in a status colour kept distinct from the service slots.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .assemble import Node, TraceTree

#: Seven categorical slots, light then dark, in a fixed order validated for
#: adjacent colour-vision separation. The palette's eighth slot, red, is left out
#: on purpose: it sits too close to the error status colour.
SERVICE_SLOTS = [
    ("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300"),
    ("#4a3aa7", "#9085e9"),
]
OTHER = ("#8b8a86", "#7a7975")

ROW = 22
GAP = 2
CHAR_W = 6.6
PAD = 12
AXIS = 28
LEGEND = 26


def _service_colours(services: list[str]) -> dict[str, int]:
    return {service: (i if i < len(SERVICE_SLOTS) else -1) for i, service in enumerate(services)}


def _style(slots_used: int) -> str:
    light = [f"--s{i}:{SERVICE_SLOTS[i][0]};" for i in range(min(slots_used, len(SERVICE_SLOTS)))]
    dark = [f"--s{i}:{SERVICE_SLOTS[i][1]};" for i in range(min(slots_used, len(SERVICE_SLOTS)))]
    return f"""<style>
svg{{--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#7a7975;--grid:rgba(11,11,11,.09);
--other:{OTHER[0]};--critical:#d03b3b;{''.join(light)}}}
@media (prefers-color-scheme:dark){{svg{{--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--muted:#8d8c83;--grid:rgba(255,255,255,.1);--other:{OTHER[1]};{''.join(dark)}}}}}
text{{font:11px ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--ink)}}
.muted{{fill:var(--muted)}} .ink2{{fill:var(--ink2)}} .title{{font-size:13px;font-weight:600}}
.bar{{fill-opacity:.24;stroke-width:0}} .edge{{fill-opacity:1}}
.err{{fill:none;stroke:var(--critical);stroke-width:1.6;stroke-dasharray:3 2}}
.hit:hover .bar{{fill-opacity:.42}}
</style>"""


def _fill(slot: int) -> str:
    return f"var(--s{slot})" if slot >= 0 else "var(--other)"


def _label(text: str, width: float) -> str:
    fits = int((width - 8) // CHAR_W)
    if fits < 3:
        return ""
    return text if len(text) <= fits else text[:max(fits - 1, 1)] + "…"


def _nice_step(span_ms: float, target_ticks: int = 6) -> float:
    if span_ms <= 0:
        return 1.0
    raw = span_ms / target_ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s".rstrip("0").rstrip(".") if ms % 1000 else f"{ms / 1000:.0f}s"
    if ms >= 10 or ms == int(ms):
        return f"{ms:.0f}ms"
    return f"{ms:.1f}ms"


def _legend(services: list[str], colours: dict[str, int], y: float, width: float) -> list[str]:
    parts = []
    x = PAD
    shown = [s for s in services if colours[s] >= 0]
    folded = len(services) - len(shown)
    for service in shown:
        parts.append(f'<rect x="{x}" y="{y}" width="12" height="12" rx="2" '
                     f'fill="{_fill(colours[service])}"/>')
        parts.append(f'<text x="{x + 17}" y="{y + 10}">{escape(service)}</text>')
        x += 17 + len(service) * CHAR_W + 16
    if folded:
        parts.append(f'<rect x="{x}" y="{y}" width="12" height="12" rx="2" fill="var(--other)"/>')
        parts.append(f'<text x="{x + 17}" y="{y + 10}" class="ink2">{folded} more</text>')
    return parts


# ---------------------------------------------------------------- flame chart

def lanes(tree: TraceTree) -> dict[int, int]:
    """Assign each span a row so that no two bars on one row overlap in time.

    Depth alone is not enough. Calls made in parallel sit at the same depth and
    overlap in time, and drawing them on one row puts one bar on top of the
    other. Each span goes on the first row below its parent that is free for its
    whole interval, which keeps parents above children and pushes parallel
    siblings onto their own lanes.
    """
    occupied: list[list[tuple[int, int]]] = []
    row_of: dict[int, int] = {}

    def free(row: int, start: int, end: int) -> bool:
        return all(end <= lo or start >= hi for lo, hi in occupied[row])

    stack = [(node, -1) for node in reversed(tree.roots)]
    while stack:
        node, parent_row = stack.pop()
        start, end = node.start_ns, max(node.end_ns, node.start_ns + 1)
        row = parent_row + 1
        while True:
            while len(occupied) <= row:
                occupied.append([])
            if free(row, start, end):
                break
            row += 1
        occupied[row].append((start, end))
        row_of[id(node)] = row
        stack.extend((child, row) for child in reversed(node.children))
    return row_of


def flame_chart(tree: TraceTree, *, width: int = 1100) -> str:
    """One trace on a time axis."""
    services = tree.services
    colours = _service_colours(services)
    duration = max(tree.duration_ns, 1)
    inner = width - 2 * PAD
    top = PAD + 20 + LEGEND + AXIS
    row_of = lanes(tree)
    rows = max(row_of.values(), default=0) + 1
    height = top + rows * (ROW + GAP) + PAD

    def x_of(ns: int) -> float:
        return PAD + (ns - tree.start_ns) / duration * inner

    root = tree.root
    heading = (f"{escape(root.span.service)} · {escape(root.span.name)}" if root else tree.trace_id)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="Flame chart of trace {tree.trace_id}: {len(tree.nodes)} spans across '
        f'{len(services)} services, {_fmt_ms(tree.duration_ns / 1e6)}">',
        _style(len(services)),
        f'<rect width="{width}" height="{height}" fill="var(--surface)"/>',
        f'<text x="{PAD}" y="{PAD + 12}" class="title">{heading}</text>',
        f'<text x="{width - PAD}" y="{PAD + 12}" text-anchor="end" class="ink2">'
        f'{_fmt_ms(tree.duration_ns / 1e6)} · {len(tree.nodes)} spans · '
        f'{len(services)} services{" · " + str(tree.errors) + " errors" if tree.errors else ""}</text>',
    ]
    parts += _legend(services, colours, PAD + 22, width)

    span_ms = duration / 1e6
    step = _nice_step(span_ms)
    tick = 0.0
    axis_y = top - 8
    while tick <= span_ms + 1e-9:
        x = PAD + tick / span_ms * inner
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{axis_y}" y2="{height - PAD}" '
                     f'stroke="var(--grid)"/>')
        anchor = "end" if x > width - PAD - 20 else "middle" if tick else "start"
        parts.append(f'<text x="{x:.1f}" y="{axis_y - 6}" class="muted" '
                     f'text-anchor="{anchor}">{_fmt_ms(tick)}</text>')
        tick += step

    for node in tree.nodes:
        parts.append(_bar(node, tree, colours, x_of(node.start_ns),
                          max(x_of(node.end_ns) - x_of(node.start_ns), 1.0),
                          top + row_of[id(node)] * (ROW + GAP)))
    parts.append("</svg>")
    return "\n".join(parts)


def _bar(node: Node, tree: TraceTree, colours: dict[str, int], x: float, w: float,
         y: float) -> str:
    span = node.span
    fill = _fill(colours[span.service])
    tooltip = [
        f"{span.service} · {span.name}",
        f"duration {_fmt_ms(span.duration_ns / 1e6)}, self {_fmt_ms(node.self_ns / 1e6)}",
        f"starts at +{_fmt_ms((node.start_ns - tree.start_ns) / 1e6)}",
    ]
    if span.is_error:
        tooltip.append(f"error{': ' + span.status_message if span.status_message else ''}")
    if node.orphan:
        tooltip.append("parent span never arrived")
    if node.shift_ns:
        tooltip.append(f"clock skew corrected by {_fmt_ms(node.shift_ns / 1e6)}")

    text = _label(f"{'! ' if span.is_error else ''}{span.name}", w)
    pieces = [
        f'<g class="hit"><title>{escape(chr(10).join(tooltip))}</title>',
        f'<rect class="bar" x="{x:.1f}" y="{y}" width="{w:.1f}" height="{ROW}" rx="3" fill="{fill}"/>',
        f'<rect class="edge" x="{x:.1f}" y="{y}" width="{w:.1f}" height="3" rx="1.5" fill="{fill}"/>',
    ]
    if span.is_error:
        pieces.append(f'<rect class="err" x="{x + 0.8:.1f}" y="{y + 0.8}" '
                      f'width="{max(w - 1.6, 0.5):.1f}" height="{ROW - 1.6}" rx="3"/>')
    if text:
        pieces.append(f'<text x="{x + 5:.1f}" y="{y + 15}">{escape(text)}</text>')
    pieces.append("</g>")
    return "".join(pieces)


# ---------------------------------------------------------------- flame graph

@dataclass
class Frame:
    name: str
    service: str
    total_ns: int = 0
    self_ns: int = 0
    samples: int = 0
    children: dict[tuple[str, str], "Frame"] = field(default_factory=dict)


def aggregate(trees: list[TraceTree]) -> Frame:
    """Merge identical call paths across traces."""
    root = Frame("all", "")
    for tree in trees:
        for top in tree.roots:
            _merge(root, top)
        root.total_ns += sum(n.end_ns - n.start_ns for n in tree.roots)
        root.samples += 1
    return root


def _merge(parent: Frame, node: Node) -> None:
    stack = [(parent, node)]
    while stack:
        into, current = stack.pop()
        key = (current.span.service, current.span.name)
        frame = into.children.get(key)
        if frame is None:
            frame = into.children[key] = Frame(current.span.name, current.span.service)
        frame.total_ns += current.span.duration_ns
        frame.self_ns += current.self_ns
        frame.samples += 1
        stack.extend((frame, child) for child in current.children)


def folded(root: Frame) -> str:
    """Brendan Gregg's folded stacks: one line per path, self time in microseconds."""
    lines: list[str] = []
    stack = [(root, [])]
    while stack:
        frame, path = stack.pop()
        for child in frame.children.values():
            # Semicolons separate frames and the space separates the count, so
            # neither may appear inside a frame's own label.
            here = path + [f"{child.service}:{child.name}".replace(";", ",").replace(" ", "_")]
            micros = child.self_ns // 1000
            if micros:
                lines.append(f"{';'.join(here)} {micros}")
            stack.append((child, here))
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def flame_graph(root: Frame, *, width: int = 1100, title: str = "Where the time goes") -> str:
    services: list[str] = []
    depth = 0
    levels = [(root, 0)]
    while levels:
        frame, level = levels.pop()
        depth = max(depth, level)
        for child in frame.children.values():
            if child.service not in services:
                services.append(child.service)
            levels.append((child, level + 1))
    colours = _service_colours(services)

    inner = width - 2 * PAD
    top = PAD + 20 + LEGEND + 6
    height = top + depth * (ROW + GAP) + PAD
    total = max(sum(c.total_ns for c in root.children.values()), 1)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="Flame graph merged from {root.samples} traces">',
        _style(len(services)),
        f'<rect width="{width}" height="{height}" fill="var(--surface)"/>',
        f'<text x="{PAD}" y="{PAD + 12}" class="title">{escape(title)}</text>',
        f'<text x="{width - PAD}" y="{PAD + 12}" text-anchor="end" class="ink2">'
        f'merged from {root.samples} traces · width is total time · children sorted by name</text>',
    ]
    parts += _legend(services, colours, PAD + 22, width)

    def draw(frame: Frame, x: float, level: int) -> None:
        cursor = x
        for key in sorted(frame.children):
            child = frame.children[key]
            w = child.total_ns / total * inner
            if w >= 0.5:
                y = top + level * (ROW + GAP)
                share = child.total_ns / total * 100
                tooltip = (f"{child.service} · {child.name}\n"
                           f"total {_fmt_ms(child.total_ns / 1e6)} ({share:.1f}%), "
                           f"self {_fmt_ms(child.self_ns / 1e6)}\n"
                           f"{child.samples} spans")
                fill = _fill(colours[child.service])
                parts.append(
                    f'<g class="hit"><title>{escape(tooltip)}</title>'
                    f'<rect class="bar" x="{cursor:.1f}" y="{y}" width="{max(w, 1):.1f}" '
                    f'height="{ROW}" rx="3" fill="{fill}"/>'
                    f'<rect class="edge" x="{cursor:.1f}" y="{y}" width="{max(w, 1):.1f}" '
                    f'height="3" rx="1.5" fill="{fill}"/>'
                    + (f'<text x="{cursor + 5:.1f}" y="{y + 15}">{escape(lab)}</text>'
                       if (lab := _label(child.name, w)) else "")
                    + "</g>")
                draw(child, cursor, level + 1)
            cursor += w

    draw(root, PAD, 0)
    parts.append("</svg>")
    return "\n".join(parts)
