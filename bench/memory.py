"""What a buffered span costs in memory.

Measured with tracemalloc around filling the buffer with real demo spans, so the
figure includes everything a span actually holds: its strings, its attribute
dictionary, its event list, and the buffer's own bookkeeping per trace.

The span class uses `__slots__`. To show what that decision is worth rather than
assert it, the same spans are also built from an otherwise identical class
without slots, and both are measured.

    python -m bench.memory --spans 100000
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import tracemalloc

from wake.demo import generate
from wake.model import Span
from wake.store import TraceBuffer


@dataclasses.dataclass
class SpanWithoutSlots:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    service: str
    start_ns: int
    end_ns: int
    kind: int = 0
    status: int = 0
    status_message: str = ""
    attributes: dict = dataclasses.field(default_factory=dict)
    events: list = dataclasses.field(default_factory=list)


def source_spans(count: int) -> list[Span]:
    spans = [s for trace in generate(count // 6 + 1, seed=5) for s in trace]
    return spans[:count]


def fresh_copies(spans: list[Span], cls) -> list:
    """Rebuild every span, strings and dicts included, so nothing is shared with
    the source list and the measurement sees the full cost of each span."""
    out = []
    for s in spans:
        out.append(cls(
            "".join(s.trace_id), "".join(s.span_id), "".join(s.parent_span_id),
            "".join(s.name), s.service, s.start_ns + 0, s.end_ns + 0, s.kind, s.status,
            "".join(s.status_message), {("".join(k)): v for k, v in s.attributes.items()},
            list(s.events)))
    return out


def measure(spans: list[Span], cls) -> dict:
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    buffer = TraceBuffer(max_spans=10**9, idle_s=3600)
    buffer.add(fresh_copies(spans, cls))
    gc.collect()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    total = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    traces = len(buffer)
    return {
        "class": cls.__name__,
        "spans": buffer.span_count,
        "traces": traces,
        "total_mb": round(total / 1e6, 2),
        "bytes_per_span": round(total / buffer.span_count),
        "bytes_per_trace": round(total / traces),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.memory", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spans", type=int, default=100_000)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    spans = source_spans(args.spans)
    with_slots = measure(spans, Span)
    without = measure(spans, SpanWithoutSlots)
    budget = 200_000
    result = {
        "with_slots": with_slots,
        "without_slots": without,
        "saving_per_span": without["bytes_per_span"] - with_slots["bytes_per_span"],
        "default_budget_spans": budget,
        "default_budget_mb": round(budget * with_slots["bytes_per_span"] / 1e6, 1),
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    attrs = sum(len(s.attributes) for s in spans) / len(spans)
    print(f"{with_slots['spans']:,} demo spans in {with_slots['traces']:,} traces, "
          f"{attrs:.1f} attributes per span on average\n")
    for row in (with_slots, without):
        print(f"{row['class']:<18} {row['bytes_per_span']:>6} B/span   "
              f"{row['bytes_per_trace']:>6} B/trace   {row['total_mb']:>7} MB total")
    print(f"\nslots save {result['saving_per_span']} bytes per span; the default budget of "
          f"{budget:,} spans costs about {result['default_budget_mb']} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
