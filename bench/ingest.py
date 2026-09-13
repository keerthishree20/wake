"""Ingest rate: how many spans per second Wake takes in.

Three numbers, measured separately so each can be read on its own:

* **decode**, in-process, protobuf and JSON. The ceiling set by parsing alone.
* **buffer**, decode plus stitching into the in-memory buffer.
* **end to end over HTTP**, with sender threads posting protobuf batches the
  way exporters do. Senders run in a separate process pinned to other cores, so
  the load generator is not stealing the collector's CPU.

    python -m bench.ingest --spans 200000
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time

from wake import otlp
from wake.demo import generate
from wake.encode import encode_request
from wake.store import Collector, TraceStore

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def batches(total_spans: int, batch: int, seed: int = 7) -> list[bytes]:
    spans = [s for trace in generate(max(total_spans // 6, 1), seed=seed) for s in trace]
    while len(spans) < total_spans:
        spans += [s for trace in generate(total_spans // 6, seed=seed + len(spans)) for s in trace]
    spans = spans[:total_spans]
    return [encode_request(spans[i:i + batch]) for i in range(0, len(spans), batch)]


def json_batches(total_spans: int, batch: int) -> list[bytes]:
    """OTLP JSON for the same spans, built from the protobuf decode."""
    out = []
    for payload in batches(total_spans, batch):
        spans = otlp.decode_protobuf(payload)
        by_service: dict[str, list] = {}
        for s in spans:
            by_service.setdefault(s.service, []).append({
                "traceId": s.trace_id, "spanId": s.span_id, "parentSpanId": s.parent_span_id,
                "name": s.name, "kind": s.kind, "startTimeUnixNano": str(s.start_ns),
                "endTimeUnixNano": str(s.end_ns),
                "status": {"code": s.status, "message": s.status_message},
                "attributes": [{"key": k, "value": _any(v)} for k, v in s.attributes.items()],
            })
        document = {"resourceSpans": [
            {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
             "scopeSpans": [{"spans": items}]} for svc, items in by_service.items()]}
        out.append(json.dumps(document).encode())
    return out


def _any(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def rate(fn, payloads: list[bytes]) -> tuple[float, int]:
    started = time.perf_counter()
    count = sum(len(fn(p)) for p in payloads)
    return count / (time.perf_counter() - started), count


def http_rate(total_spans: int, batch: int, senders: int, server_cpus: str, client_cpus: str,
              *, pure: bool = False) -> dict:
    """Run a Wake server pinned to some cores and a sender process pinned to others."""
    port = 43180 + random.randint(0, 500)
    db = os.path.join(HERE, ".run", "ingest.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    env = dict(os.environ)
    if pure:
        env["WAKE_PURE_PROTOBUF"] = "1"
    server = subprocess.Popen(
        ["taskset", "-c", server_cpus, sys.executable, "-m", "wake.cli", "serve",
         "--port", str(port), "--database", db, "--idle", "0.5"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        sender = subprocess.run(
            ["taskset", "-c", client_cpus, sys.executable, "-m", "bench.ingest", "--send-only",
             "--port", str(port), "--spans", str(total_spans), "--batch", str(batch),
             "--senders", str(senders)],
            cwd=REPO, capture_output=True, text=True, timeout=900)
        result = json.loads(sender.stdout.strip().splitlines()[-1])
        sent_at_end = time.perf_counter()

        # Sustained: keep going until every trace is on disk, not just accepted.
        # Accepting faster than storage can keep up only fills the buffer.
        expected = len({s.trace_id for p in batches(total_spans, batch) for s in otlp.decode_protobuf(p)})
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            stats = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats").read())
            if stats["stored_traces"] >= expected:
                break
            time.sleep(0.05)
        stored_after = result["seconds"] + (time.perf_counter() - sent_at_end)
        result["server_received"] = stats["spans_received"]
        result["rejected_batches"] = stats["rejected_batches"]
        result["stored_traces"] = stats["stored_traces"]
        result["expected_traces"] = expected
        result["flushed_early"] = stats["flushed_early"]
        result["decoder"] = stats["protobuf_decoder"]
        result["until_stored_seconds"] = round(stored_after, 2)
        result["sustained_spans_per_s"] = round(total_spans / stored_after)
        return result
    finally:
        server.terminate()
        server.wait(timeout=30)


def send_only(port: int, total_spans: int, batch: int, senders: int) -> None:
    import http.client
    payloads = batches(total_spans, batch)
    per_sender = [payloads[i::senders] for i in range(senders)]
    counts = [0] * senders
    errors = [0] * senders

    def run(n: int) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        for body in per_sender[n]:
            connection.request("POST", "/v1/traces", body=body,
                               headers={"Content-Type": "application/x-protobuf"})
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                counts[n] += 1
            else:
                errors[n] += 1

    threads = [threading.Thread(target=run, args=(n,)) for n in range(senders)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    sent = min(total_spans, sum(counts) * batch)
    print(json.dumps({"spans": sent, "seconds": round(elapsed, 3),
                      "spans_per_s": round(sent / elapsed), "batches": sum(counts),
                      "http_errors": sum(errors), "senders": senders, "batch": batch}))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spans", type=int, default=200_000)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--senders", type=int, default=4)
    p.add_argument("--server-cpus", default="0,1")
    p.add_argument("--client-cpus", default="4,5,6,7")
    p.add_argument("--send-only", action="store_true")
    p.add_argument("--port", type=int, default=4318)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.send_only:
        send_only(args.port, args.spans, args.batch, args.senders)
        return 0

    from wake import otlp_fast
    proto = batches(args.spans, args.batch)
    as_json = json_batches(args.spans, args.batch)
    proto_rate, count = rate(otlp.decode_protobuf, proto)
    fast_rate, _ = rate(otlp_fast.decode_protobuf, proto) if otlp_fast.AVAILABLE else (0, 0)
    json_rate, _ = rate(otlp.decode_json, as_json)

    collector = Collector(TraceStore(":memory:"), max_spans=10**9, idle_s=3600)
    decoder = otlp.protobuf_decoder()[0]
    buffer_rate, _ = rate(lambda payload: (collector.ingest(s := decoder(payload)), s)[1], proto)

    started = time.perf_counter()
    stored = collector.flush_due(everything=True)
    flush_s = time.perf_counter() - started

    http = http_rate(args.spans, args.batch, args.senders, args.server_cpus, args.client_cpus)
    http_pure = http_rate(args.spans, args.batch, args.senders, args.server_cpus, args.client_cpus,
                          pure=True)

    result = {
        "spans": count,
        "protobuf_bytes_per_span": round(sum(map(len, proto)) / count, 1),
        "json_bytes_per_span": round(sum(map(len, as_json)) / count, 1),
        "decode_protobuf_hand_written_spans_per_s": round(proto_rate),
        "decode_protobuf_library_spans_per_s": round(fast_rate),
        "decode_json_spans_per_s": round(json_rate),
        "decode_and_buffer_spans_per_s": round(buffer_rate),
        "flush_traces": stored,
        "flush_traces_per_s_in_memory": round(stored / flush_s),
        "http": http,
        "http_hand_written_decoder": http_pure,
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"{count:,} spans from the demo shop, batches of {args.batch}\n")
    print(f"wire size        protobuf {result['protobuf_bytes_per_span']} B/span, "
          f"JSON {result['json_bytes_per_span']} B/span")
    print(f"decode           protobuf, hand-written   {result['decode_protobuf_hand_written_spans_per_s']:>9,}/s")
    print(f"                 protobuf, library        {result['decode_protobuf_library_spans_per_s']:>9,}/s")
    print(f"                 JSON                     {result['decode_json_spans_per_s']:>9,}/s")
    print(f"decode + buffer                           {result['decode_and_buffer_spans_per_s']:>9,}/s")
    print(f"flush, in memory                          {result['flush_traces_per_s_in_memory']:>9,} traces/s")
    for label, h in (("HTTP, library decoder", http), ("HTTP, hand-written", http_pure)):
        print(f"{label:<24} accepted {h['spans_per_s']:>7,}/s, stored on disk {h['sustained_spans_per_s']:>7,}/s "
              f"({h['stored_traces']:,}/{h['expected_traces']:,} traces, {h['http_errors']} errors, "
              f"{h['flushed_early']} flushed early)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
