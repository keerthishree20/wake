"""A span, as Wake keeps it."""

from __future__ import annotations

from dataclasses import dataclass, field

KINDS = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
STATUSES = {0: "unset", 1: "ok", 2: "error"}


@dataclass(slots=True)
class Event:
    name: str
    time_ns: int
    attributes: dict = field(default_factory=dict)


@dataclass(slots=True)
class Span:
    """One unit of work in one service.

    Slots, because the trace buffer holds every span of every recent trace in
    memory. The saving is smaller than folklore suggests, and the benchmark says
    so: about 40 bytes of roughly 700 per span, since Python 3.12 already stores
    plain instance attributes compactly. Most of a span's memory is its id
    strings and its attribute dictionary, not the object that holds them.
    """

    trace_id: str          # 32 lowercase hex characters
    span_id: str           # 16 lowercase hex characters
    parent_span_id: str    # empty for a root span
    name: str
    service: str
    start_ns: int
    end_ns: int
    kind: int = 0
    status: int = 0
    status_message: str = ""
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    @property
    def duration_ns(self) -> int:
        return max(self.end_ns - self.start_ns, 0)

    @property
    def is_root(self) -> bool:
        return not self.parent_span_id

    @property
    def is_error(self) -> bool:
        return self.status == 2

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id or None,
            "name": self.name,
            "service": self.service,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_ms": round(self.duration_ns / 1e6, 3),
            "kind": KINDS.get(self.kind, str(self.kind)),
            "status": STATUSES.get(self.status, str(self.status)),
            "status_message": self.status_message or None,
            "attributes": self.attributes,
            "events": [{"name": e.name, "time_ns": e.time_ns, "attributes": e.attributes}
                       for e in self.events],
        }

    def to_row(self) -> list:
        """The compact stored form: positional, no computed fields, and no trace
        id, which the row that holds the spans already carries. About half the
        size of the API form, and cheaper to encode."""
        return [self.span_id, self.parent_span_id, self.name, self.service, self.start_ns,
                self.end_ns, self.kind, self.status, self.status_message, self.attributes,
                [[e.name, e.time_ns, e.attributes] for e in self.events]]

    @classmethod
    def from_row(cls, trace_id: str, row: list) -> "Span":
        return cls(trace_id, row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                   row[8], row[9], [Event(e[0], e[1], e[2]) for e in row[10]])

    @classmethod
    def from_dict(cls, raw: dict) -> "Span":
        kinds = {v: k for k, v in KINDS.items()}
        statuses = {v: k for k, v in STATUSES.items()}
        return cls(
            trace_id=raw["trace_id"], span_id=raw["span_id"],
            parent_span_id=raw.get("parent_span_id") or "",
            name=raw["name"], service=raw["service"],
            start_ns=int(raw["start_ns"]), end_ns=int(raw["end_ns"]),
            kind=kinds.get(raw.get("kind"), 0), status=statuses.get(raw.get("status"), 0),
            status_message=raw.get("status_message") or "",
            attributes=raw.get("attributes") or {},
            events=[Event(e["name"], int(e["time_ns"]), e.get("attributes") or {})
                    for e in raw.get("events") or []],
        )
