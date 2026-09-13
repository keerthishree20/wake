from __future__ import annotations

import threading

import pytest

from wake.model import Span
from wake.server import WakeServer
from wake.store import Collector, TraceStore

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def span(span_id: str, parent: str = "", *, service: str = "svc", name: str | None = None,
         start: int = 0, end: int = 1_000_000, trace: str = TRACE, status: int = 0, **attrs) -> Span:
    """Ids are padded to their real length so tests can use short readable ones."""
    return Span(trace_id=trace, span_id=span_id.rjust(16, "0"),
                parent_span_id=parent.rjust(16, "0") if parent else "",
                name=name or f"op-{span_id}", service=service, start_ns=start, end_ns=end,
                status=status, attributes=dict(attrs))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def server():
    collector = Collector(TraceStore(":memory:"), idle_s=0.2)
    srv = WakeServer(("127.0.0.1", 0), collector, flush_interval_s=0.05)
    srv.start_background()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
