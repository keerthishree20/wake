"""Spans to a trace tree.

Spans from different services arrive separately, in any order, possibly
duplicated, sometimes with their parent missing entirely. This file turns
whatever arrived into one tree and records every repair it made, because a
tracing view that silently rearranges data is harder to trust than one that
shows its working.

Three repairs:

* **Orphans.** A span whose parent never arrived is kept, attached at the top
  level, and marked. Dropping it would hide exactly the spans from the service
  whose exporter is misbehaving.
* **Clock skew.** Each service stamps times with its own clock. When a child
  from a different service appears to start before its parent, the parent's
  clock is taken as the reference and the whole child subtree is shifted to
  begin no earlier than the parent. Skew within one service is left alone: that
  is a real bug worth seeing, not a clock disagreement.
* **Self time from the union of children.** A parent's own time is its duration
  minus the time covered by its children. Children that run in parallel overlap,
  and subtracting their summed durations would give negative self time, so the
  covered time is the union of their intervals, clipped to the parent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Span


@dataclass
class Node:
    span: Span
    depth: int = 0
    children: list["Node"] = field(default_factory=list)
    self_ns: int = 0
    shift_ns: int = 0
    orphan: bool = False

    @property
    def start_ns(self) -> int:
        return self.span.start_ns + self.shift_ns

    @property
    def end_ns(self) -> int:
        return self.span.end_ns + self.shift_ns


@dataclass
class TraceTree:
    trace_id: str
    roots: list[Node]
    nodes: list[Node]
    start_ns: int
    end_ns: int
    duplicates: int
    orphans: int
    skew_adjusted: int

    @property
    def duration_ns(self) -> int:
        return max(self.end_ns - self.start_ns, 0)

    @property
    def services(self) -> list[str]:
        seen: dict[str, None] = {}
        for node in self.nodes:
            seen.setdefault(node.span.service, None)
        return list(seen)

    @property
    def depth(self) -> int:
        return max((n.depth for n in self.nodes), default=-1) + 1

    @property
    def errors(self) -> int:
        return sum(1 for n in self.nodes if n.span.is_error)

    @property
    def root(self) -> Node | None:
        """The real root if there is exactly one, else the earliest top-level span."""
        real = [n for n in self.roots if not n.orphan]
        if len(real) == 1:
            return real[0]
        return min(self.roots, key=lambda n: n.start_ns, default=None)

    def to_dict(self) -> dict:
        def encode(node: Node) -> dict:
            body = node.span.to_dict()
            body.update({
                "depth": node.depth,
                "offset_ms": round((node.start_ns - self.start_ns) / 1e6, 3),
                "self_ms": round(node.self_ns / 1e6, 3),
                "clock_shift_ms": round(node.shift_ns / 1e6, 3) if node.shift_ns else 0,
                "orphan": node.orphan,
                "children": [encode(child) for child in node.children],
            })
            return body

        root = self.root
        return {
            "trace_id": self.trace_id,
            "root_service": root.span.service if root else None,
            "root_name": root.span.name if root else None,
            "start_ns": self.start_ns,
            "duration_ms": round(self.duration_ns / 1e6, 3),
            "span_count": len(self.nodes),
            "services": self.services,
            "depth": self.depth,
            "errors": self.errors,
            "repairs": {"duplicates": self.duplicates, "orphans": self.orphans,
                        "skew_adjusted": self.skew_adjusted},
            "roots": [encode(root) for root in self.roots],
        }


def assemble(spans: list[Span], *, self_time: bool = True) -> TraceTree:
    """Build the tree. `self_time=False` skips the one part a stored summary does
    not need; it was an eighth of the time spent flushing to disk."""
    if not spans:
        raise ValueError("cannot assemble a trace from no spans")
    trace_id = spans[0].trace_id

    by_id: dict[str, Node] = {}
    duplicates = 0
    for span in spans:
        if span.trace_id != trace_id:
            raise ValueError("spans from more than one trace")
        if span.span_id in by_id:
            # Exporters retry whole batches. The first copy wins; later ones are
            # counted, not stacked on top of it.
            duplicates += 1
            continue
        by_id[span.span_id] = Node(span)

    roots: list[Node] = []
    orphans = 0
    for node in by_id.values():
        parent_id = node.span.parent_span_id
        if not parent_id:
            roots.append(node)
        elif parent_id in by_id and parent_id != node.span.span_id:
            by_id[parent_id].children.append(node)
        else:
            node.orphan = True
            orphans += 1
            roots.append(node)

    # A parent chain that loops back on itself has no root to hang from. Break
    # it at an arbitrary member so every span is still shown exactly once.
    reachable = _reachable(roots)
    for node in by_id.values():
        if id(node) not in reachable:
            parent = by_id.get(node.span.parent_span_id)
            if parent is not None and node in parent.children:
                parent.children.remove(node)
            node.orphan = True
            orphans += 1
            roots.append(node)
            reachable |= _reachable([node])

    roots.sort(key=lambda n: (n.span.start_ns, n.span.span_id))
    skew_adjusted = 0
    for root in roots:
        skew_adjusted += _layout(root, 0, self_time=self_time)

    nodes = [n for n in _walk(roots)]
    return TraceTree(
        trace_id=trace_id, roots=roots, nodes=nodes,
        start_ns=min(n.start_ns for n in nodes),
        end_ns=max(n.end_ns for n in nodes),
        duplicates=duplicates, orphans=orphans, skew_adjusted=skew_adjusted,
    )


def _reachable(roots: list[Node]) -> set[int]:
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        stack.extend(node.children)
    return seen


def _layout(node: Node, depth: int, *, self_time: bool = True) -> int:
    """Set depth, fix skew, and compute self time, top down. Iterative, because
    a pathological trace can nest deeper than Python's recursion limit."""
    adjusted = 0
    stack = [(node, depth)]
    order: list[Node] = []
    while stack:
        current, level = stack.pop()
        current.depth = level
        order.append(current)
        current.children.sort(key=lambda c: (c.span.start_ns, c.span.span_id))
        for child in current.children:
            child.shift_ns += current.shift_ns   # inherit the parent's correction first
            if (child.span.service != current.span.service
                    and child.start_ns < current.start_ns):
                child.shift_ns += current.start_ns - child.start_ns
                adjusted += 1
            stack.append((child, level + 1))

    if self_time:
        for current in order:
            current.self_ns = _self_time(current)
    return adjusted


def _self_time(node: Node) -> int:
    start, end = node.start_ns, node.end_ns
    intervals = sorted(
        (max(c.start_ns, start), min(c.end_ns, end))
        for c in node.children if c.end_ns > start and c.start_ns < end
    )
    covered = 0
    cursor = start
    for lo, hi in intervals:
        lo = max(lo, cursor)
        if hi > lo:
            covered += hi - lo
            cursor = hi
    return max((end - start) - covered, 0)


def _walk(roots: list[Node]):
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))
