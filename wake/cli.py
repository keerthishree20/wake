"""Command line interface.

    wake serve                      OTLP on :4318, pages on the same port
    wake demo --count 300           send synthetic multi-service traffic
    wake flame --service frontend   print folded stacks from a stored database
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request

from .demo import generate
from .encode import encode_request


def cmd_serve(args) -> int:
    from .server import serve
    server = serve(args.host, args.port, database=args.database,
                   max_spans=args.max_spans, idle_s=args.idle)
    print(f"wake listening on http://{args.host}:{server.server_address[1]}\n"
          f"  OTLP traces  POST /v1/traces  (protobuf or JSON)\n"
          f"  database     {args.database}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


def cmd_demo(args) -> int:
    """Send demo traces as real OTLP protobuf, in batches, the way an exporter would."""
    url = f"http://{args.host}:{args.port}/v1/traces"
    traces = generate(args.count, seed=args.seed)
    batch: list = []
    sent = 0
    for trace in traces:
        batch.extend(trace)
        if len(batch) >= args.batch:
            _post(url, encode_request(batch))
            sent += len(batch)
            batch = []
    if batch:
        _post(url, encode_request(batch))
        sent += len(batch)
    print(f"sent {sent} spans in {len(traces)} traces to {url}")
    return 0


def _post(url: str, body: bytes) -> None:
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-protobuf"})
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def cmd_flame(args) -> int:
    from .assemble import assemble
    from .flame import aggregate, folded
    from .store import TraceStore
    store = TraceStore(args.database)
    trees = [assemble(spans) for row in store.search(service=args.service, limit=args.limit)
             if (spans := store.get(row["trace_id"]))]
    sys.stdout.write(folded(aggregate(trees)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wake", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the collector")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4318, help="4318 is the standard OTLP/HTTP port")
    p.add_argument("--database", default="wake.db")
    p.add_argument("--max-spans", type=int, default=200_000, help="in-memory span budget")
    p.add_argument("--idle", type=float, default=5.0, help="seconds of quiet before a trace is stored")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("demo", help="send synthetic traffic")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4318)
    p.add_argument("--count", type=int, default=300)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--seed", type=int, default=None)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("flame", help="folded stacks from a database")
    p.add_argument("--database", default="wake.db")
    p.add_argument("--service", default=None)
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(fn=cmd_flame)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
