"""Synthetic traffic: a small shop of services calling each other.

Realistic enough to exercise every repair the assembler makes. Requests fan out
in parallel, some calls fail, one service's clock runs behind the others, and a
small share of spans from one service is dropped on the floor so orphans appear.
"""

from __future__ import annotations

import os
import random
import time

from .model import Event, Span

SKEWED_SERVICE = "inventory"
#: inventory's clock runs 4 ms ahead of everyone else's, so the database call it
#: makes appears to start before inventory itself does. That is the case the
#: assembler's skew repair exists for.
SKEW_NS = 4_000_000
#: The span whose loss is interesting: dropping a parent orphans its child.
DROPPED_PARENT = "reserve stock"
DROP_RATE = 0.05


class TraceBuilder:
    def __init__(self, rng: random.Random, start_ns: int):
        self.rng = rng
        self.trace_id = rng.getrandbits(128).to_bytes(16, "big").hex()
        self.start_ns = start_ns
        self.spans: list[Span] = []

    def span(self, service: str, name: str, parent: Span | None, start_ns: int,
             duration_ms: float, *, kind: int = 1, error: str = "", **attributes) -> Span:
        skew = SKEW_NS if service == SKEWED_SERVICE else 0
        span = Span(
            trace_id=self.trace_id,
            span_id=self.rng.getrandbits(64).to_bytes(8, "big").hex(),
            parent_span_id=parent.span_id if parent else "",
            name=name, service=service,
            start_ns=start_ns + skew,
            end_ns=start_ns + skew + int(duration_ms * 1e6),
            kind=kind, status=2 if error else 0, status_message=error,
            attributes=dict(attributes),
        )
        if error:
            span.events.append(Event("exception", span.end_ns, {"exception.message": error}))
        self.spans.append(span)
        return span


def checkout_trace(rng: random.Random, now_ns: int) -> list[Span]:
    t = TraceBuilder(rng, now_ns)
    ms = lambda lo, hi: rng.uniform(lo, hi)
    cursor = now_ns

    total = ms(40, 90)
    root = t.span("frontend", "POST /checkout", None, cursor, total, kind=2,
                  **{"http.method": "POST", "http.route": "/checkout", "http.status_code": 200})

    auth = ms(2, 6)
    t.span("auth", "verify token", root, cursor + int(1e6), auth, kind=2)
    cursor += int((1 + auth) * 1e6)

    cart_ms = ms(8, 18)
    cart = t.span("cart", "load cart", root, cursor, cart_ms, kind=2, items=rng.randint(1, 6))
    t.span("postgres", "SELECT cart_items", cart, cursor + int(1e6), cart_ms * 0.6, kind=3,
           **{"db.system": "postgresql"})
    cursor += int(cart_ms * 1e6)

    # Parallel fan-out: inventory and pricing run at the same time.
    fan_start = cursor
    inventory_ms = ms(6, 20)
    inventory = t.span("inventory", "reserve stock", root, fan_start, inventory_ms, kind=2)
    t.span("postgres", "UPDATE stock", inventory, fan_start + int(0.5e6), inventory_ms * 0.3,
           kind=3, **{"db.system": "postgresql"})
    pricing_ms = ms(5, 14)
    t.span("cart", "quote basket", root, fan_start, pricing_ms, kind=2)
    cursor += int(max(inventory_ms, pricing_ms) * 1e6)

    failed = rng.random() < 0.08
    pay_ms = ms(10, 30)
    payment = t.span("payments", "charge card", root, cursor, pay_ms, kind=2,
                     error="card declined" if failed else "", **{"payment.provider": "stripe"})
    t.span("payments", "call provider", payment, cursor + int(1e6), pay_ms * 0.8, kind=3)

    if not failed:
        t.span("recommendations", "suggest next", root, cursor + int(pay_ms * 1e6),
               ms(3, 8), kind=2)

    root.end_ns = max(root.end_ns, max(s.end_ns for s in t.spans if s.service != SKEWED_SERVICE))
    if failed:
        root.status, root.status_message = 2, "payment failed"
        root.attributes["http.status_code"] = 402

    # Now and then inventory's exporter loses its span but the database span it
    # parented still arrives, which is how orphans turn up in real traces.
    drop = rng.random() < DROP_RATE
    return [s for s in t.spans if not (drop and s.name == DROPPED_PARENT)]


def browse_trace(rng: random.Random, now_ns: int) -> list[Span]:
    t = TraceBuilder(rng, now_ns)
    total = rng.uniform(10, 30)
    root = t.span("frontend", "GET /products", None, now_ns, total, kind=2,
                  **{"http.method": "GET", "http.route": "/products", "http.status_code": 200})
    catalog_ms = total * 0.7
    catalog = t.span("catalog", "list products", root, now_ns + int(1e6), catalog_ms, kind=2)
    for i in range(rng.randint(1, 3)):
        t.span("postgres", "SELECT products", catalog, now_ns + int((2 + i * 3) * 1e6), 2.5, kind=3)
    return t.spans


def generate(count: int, *, seed: int | None = None) -> list[list[Span]]:
    rng = random.Random(seed if seed is not None else int.from_bytes(os.urandom(4), "big"))
    now = time.time_ns()
    traces = []
    for i in range(count):
        start = now - (count - i) * 7_000_000
        maker = checkout_trace if rng.random() < 0.4 else browse_trace
        traces.append(maker(rng, start))
    return traces
