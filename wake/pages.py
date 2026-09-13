"""Server-rendered pages. No script: every chart is SVG with native tooltips."""

from __future__ import annotations

import datetime
from html import escape
from urllib.parse import urlencode

from .assemble import TraceTree
from .flame import flame_chart

STYLE = """
:root{--page:#f3f3f1;--surface:#fcfcfb;--line:#dedddb;--ink:#0b0b0b;--ink2:#52514e;--muted:#7a7975;
--accent:#2a78d6;--bar:rgba(42,120,214,.28);--critical:#d03b3b;--warn:#fab219}
@media (prefers-color-scheme:dark){:root{--page:#131312;--surface:#1a1a19;--line:#33332f;--ink:#fff;
--ink2:#c3c2b7;--muted:#8d8c83;--accent:#3987e5;--bar:rgba(57,135,229,.34)}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
padding-block:20px 48px;padding-left:18px;padding-right:18px}
.wrap{max-width:1140px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px}
h1{margin:0;font-size:20px;font-weight:650;letter-spacing:-.01em;margin-right:auto}
h1 a{color:inherit;text-decoration:none}
h2{margin:0 0 8px;font-size:14px;font-weight:650}
.sub{color:var(--ink2);font-size:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.stats{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:13px;color:var(--ink2)}
.stats b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
form{display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:13px}
select,button{font:inherit;font-size:13px;color:var(--ink);background:var(--surface);border:1px solid var(--line);
border-radius:6px;padding:4px 10px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 12px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
tbody tr:last-child td{border-bottom:none}
td.n{font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,monospace}
a{color:var(--accent)}
.dur{display:flex;align-items:center;gap:8px;min-width:170px}
.dur span.track{flex:1;height:8px;background:transparent;position:relative}
.dur span.fill{position:absolute;left:0;top:0;bottom:0;background:var(--bar);border-radius:2px;border-left:2px solid var(--accent)}
.pill{display:inline-block;font-size:11px;font-weight:650;padding:1px 7px;border-radius:999px}
.pill.err{background:var(--critical);color:#fff}
.pill.note{border:1px solid var(--line);color:var(--ink2)}
.chart{overflow-x:auto}
.chart svg{display:block;max-width:none}
.empty{color:var(--muted)}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><div class=wrap>{body}</div></body></html>")


def _when(ns: int) -> str:
    return datetime.datetime.fromtimestamp(ns / 1e9).strftime("%H:%M:%S.%f")[:-3]


def index(traces: list[dict], services: list[str], stats: dict, *, service: str | None,
          errors_only: bool) -> str:
    longest = max((t["duration_ms"] for t in traces), default=1) or 1
    options = "".join(
        f"<option value='{escape(s)}'{' selected' if s == service else ''}>{escape(s)}</option>"
        for s in services)
    rows = []
    for t in traces:
        share = t["duration_ms"] / longest * 100
        flags = []
        if t["errors"]:
            flags.append(f"<span class='pill err'>{t['errors']} error{'s' if t['errors'] > 1 else ''}</span>")
        if t["in_flight"]:
            flags.append("<span class='pill note'>receiving</span>")
        if t["flushed_early"]:
            flags.append("<span class='pill note' title='flushed to stay within the span budget; "
                         "a late span may be missing'>flushed early</span>")
        rows.append(
            f"<tr><td class=n>{_when(t['start_ns'])}</td>"
            f"<td><a href='/traces/{t['trace_id']}'>{escape(t['root_service'] or '?')} · "
            f"{escape(t['root_name'] or '?')}</a></td>"
            f"<td><div class=dur><span class=track><span class=fill style='width:{share:.1f}%'></span></span>"
            f"<span class=n>{t['duration_ms']:.1f} ms</span></div></td>"
            f"<td class=n>{t['span_count']}</td><td class=n>{len(t['services'])}</td>"
            f"<td>{' '.join(flags)}</td></tr>")

    table = ("<div class=scroll><table><thead><tr><th>Started</th><th>Root span</th><th>Duration</th>"
             "<th>Spans</th><th>Services</th><th></th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table></div>") if rows else \
        "<p class=empty>No traces yet. Point an OpenTelemetry exporter at <code>POST /v1/traces</code>, " \
        "or run <code>wake demo</code>.</p>"

    flame_query = urlencode({"service": service} if service else {})
    return _page("Wake", f"""
<header><h1><a href='/'>Wake</a></h1>
<span class=sub>OpenTelemetry traces, stitched across services</span></header>
<div class='card stats'>
<span><b>{stats['spans_received']:,}</b> spans received</span>
<span><b>{stats['traces_flushed']:,}</b> traces stored</span>
<span><b>{stats['buffered_traces']:,}</b> receiving now</span>
<span><b>{stats['duplicate_spans']:,}</b> duplicates dropped</span>
<span><b>{stats['flushed_early']:,}</b> flushed early</span>
</div>
<div class=card><h2>Where the time goes</h2>
<p class=sub style='margin:0 0 8px'>Merged from the recent traces below. Width is total time; hover a bar
for its self time. Also as <a href='/flame.folded?{flame_query}'>folded stacks</a>.</p>
<div class=chart><img src='/flame.svg?{flame_query}' alt='Aggregated flame graph' style='max-width:100%'></div></div>
<div class=card><form method=get action='/'>
<label>Service <select name=service onchange='this.form.submit()'><option value=''>all</option>{options}</select></label>
<label><input type=checkbox name=errors value=1{' checked' if errors_only else ''} onchange='this.form.submit()'> errors only</label>
<noscript><button>Filter</button></noscript></form></div>
<div class=card><h2>Recent traces</h2>{table}</div>""")


def trace(tree: TraceTree) -> str:
    root = tree.root
    repairs = tree.to_dict()["repairs"]
    notes = []
    if repairs["orphans"]:
        notes.append(f"{repairs['orphans']} span{'s' if repairs['orphans'] > 1 else ''} arrived "
                     "without their parent and are shown at the top level")
    if repairs["skew_adjusted"]:
        notes.append(f"{repairs['skew_adjusted']} span{'s' if repairs['skew_adjusted'] > 1 else ''} "
                     "shifted to correct clock skew between services")
    if repairs["duplicates"]:
        notes.append(f"{repairs['duplicates']} duplicate spans ignored")

    rows = []
    for node in tree.nodes:
        span = node.span
        flags = []
        if span.is_error:
            flags.append(f"<span class='pill err'>{escape(span.status_message or 'error')}</span>")
        if node.orphan:
            flags.append("<span class='pill note'>no parent</span>")
        if node.shift_ns:
            flags.append(f"<span class='pill note'>skew +{node.shift_ns / 1e6:.1f} ms</span>")
        indent = "&nbsp;&nbsp;" * node.depth
        rows.append(
            f"<tr><td>{indent}{escape(span.name)}</td><td>{escape(span.service)}</td>"
            f"<td class=n>{span.duration_ns / 1e6:.2f} ms</td><td class=n>{node.self_ns / 1e6:.2f} ms</td>"
            f"<td class=n>+{(node.start_ns - tree.start_ns) / 1e6:.2f} ms</td><td>{' '.join(flags)}</td></tr>")

    heading = f"{escape(root.span.service)} · {escape(root.span.name)}" if root else tree.trace_id
    note_html = "".join(f"<li>{escape(n)}</li>" for n in notes)
    return _page(f"Trace {tree.trace_id[:8]}", f"""
<header><h1><a href='/'>Wake</a> · {heading}</h1>
<span class=sub><code>{tree.trace_id}</code></span></header>
<div class='card stats'>
<span><b>{tree.duration_ns / 1e6:.2f} ms</b> total</span>
<span><b>{len(tree.nodes)}</b> spans</span>
<span><b>{len(tree.services)}</b> services</span>
<span><b>{tree.depth}</b> deep</span>
<span><b>{tree.errors}</b> errors</span>
<span><a href='/api/traces/{tree.trace_id}'>JSON</a> · <a href='/traces/{tree.trace_id}/flame.svg'>SVG</a></span>
</div>
{f"<div class=card><h2>Repairs made while assembling</h2><ul style='margin:0;padding-left:18px'>{note_html}</ul></div>" if notes else ""}
<div class='card chart'>{flame_chart(tree)}</div>
<div class=card><h2>Spans</h2><div class=scroll><table><thead><tr><th>Span</th><th>Service</th>
<th>Duration</th><th>Self</th><th>Starts</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>""")
