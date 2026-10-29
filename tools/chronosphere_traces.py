#!/usr/bin/env python3
"""Inspect traces in Chronosphere through its Data API (uses chronosphere.tenant / api_token from config.json).

  python3 tools/chronosphere_traces.py trace <trace_id> [<trace_id> ...]
      List every span Chronosphere holds for these traces, with service and parent.
  python3 tools/chronosphere_traces.py completeness <start RFC3339> [--settle-min 4]
      For traces whose gateway span started after <start>, count how many have the forwarded
      app span (vantra-android-production) as their root. Traces younger than --settle-min
      minutes are skipped because their app span may not have been forwarded yet.

API notes (POST /api/v1/data/traces, header API-Token): query_type TRACE_IDS must not carry a time range;
SERVICE_OPERATION needs one and allows at most 15 minutes. IDs come back base64-encoded.
"""

import argparse
import base64
import datetime
import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_SERVICE = "vantra-android-production"


def query(cfg, body):
    req = urllib.request.Request(f"https://{cfg['tenant']}.chronosphere.io/api/v1/data/traces",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"API-Token": cfg["api_token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"traces": []}
        raise SystemExit(f"HTTP {e.code}: {e.read()[:300].decode()}")


def hexid(v):
    return base64.b64decode(v).hex() if v else ""


def spans_of(trace):
    return [s for rs in trace["resource_spans"] for ss in rs["scope_spans"] for s in ss["spans"]]


def service(span):
    return {a["key"]: list(a["value"].values())[0] for a in span.get("attributes", [])}.get("service.name", "?")


def iso(t):
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def cmd_trace(cfg, ids):
    d = query(cfg, {"query_type": "TRACE_IDS", "trace_ids": ids})
    for tr in d.get("traces", []):
        spans = spans_of(tr)
        print(f"trace {hexid(spans[0]['trace_id'])}: {len(spans)} spans")
        for s in sorted(spans, key=lambda s: int(s["start_time_unix_nano"])):
            print(f"  {service(s):32} {s['name'][:56]:56} span={hexid(s['span_id'])} parent={hexid(s.get('parent_span_id'))}")


def cmd_completeness(cfg, start, settle_min):
    since = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
    until = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=settle_min)
    seen, rows, t = set(), [], since
    while t < until:
        end = min(t + datetime.timedelta(minutes=2), until)
        for op in ("", "POST /api/v3/bookings", "POST /api/v2/checkout/orders", "POST /api/v1/transfers"):
            for tr in query(cfg, {"query_type": "SERVICE_OPERATION", "service": "api-gateway", "operation": op,
                                  "start_time": iso(t), "end_time": iso(end)}).get("traces", []):
                spans = spans_of(tr)
                tid = spans[0]["trace_id"]
                if tid in seen:
                    continue
                seen.add(tid)
                gw = next((s for s in spans if service(s) == "api-gateway" and " " in s["name"]), spans[0])
                errors = sum(1 for s in spans if s.get("status", {}).get("code") == "STATUS_CODE_ERROR")
                rows.append((APP_SERVICE in {service(s) for s in spans}, len(spans), errors, gw["name"]))
        t = end
    ok = sum(1 for r in rows if r[0])
    print(f"{len(rows)} traces {iso(since)}..{iso(until)}: {ok} complete, {len(rows) - ok} missing the app span")
    for r in sorted(rows, key=lambda r: -r[1])[:15]:
        print(f"  {'complete' if r[0] else 'MISSING '}  {r[1]:3d} spans  errors={r[2]:2d}  {r[3]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("trace")
    t.add_argument("ids", nargs="+")
    c = sub.add_parser("completeness")
    c.add_argument("start")
    c.add_argument("--settle-min", type=float, default=4)
    a = ap.parse_args()
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)["chronosphere"]
    if a.cmd == "trace":
        cmd_trace(cfg, a.ids)
    else:
        cmd_completeness(cfg, a.start, a.settle_min)


if __name__ == "__main__":
    main()
