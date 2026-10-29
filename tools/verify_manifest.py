#!/usr/bin/env python3
"""Check every expected request ID against retained traces, including missing IDs.

Transport failures that never reached a server are counted separately. A
successful export receipt does not imply retention or dashboard visibility.
"""
import argparse
import collections
import datetime
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xcorgen.runner import load_config
from chronosphere_traces import query, hexid


def attrs(items):
    return {x["key"]: next(iter(x["value"].values())) for x in items}


def unpack(trace):
    records = []
    for resource in trace.get("resource_spans", []):
        common = attrs(resource.get("resource", {}).get("attributes", []))
        for scope in resource.get("scope_spans", []):
            for span in scope.get("spans", []):
                a = dict(common, **attrs(span.get("attributes", [])))
                records.append({"trace_id": hexid(span["trace_id"]), "span_id": hexid(span["span_id"]),
                    "parent_id": hexid(span.get("parent_span_id")), "name": span["name"], "attributes": a,
                    "kind": span.get("kind"), "status": span.get("status", {}),
                    "start_ns": span["start_time_unix_nano"], "end_ns": span["end_time_unix_nano"],
                    "events": span.get("events", [])})
    return records


def verify(manifest, cfg, settle_seconds=240, previous=None):
    now = time.time() * 1000
    sessions = [json.loads(line) for line in open(manifest) if line.strip()]
    sessions = [s for s in sessions if s["end_ms"] < now - settle_seconds * 1000]
    expected = {r["trace_id"]: r for s in sessions for r in s["requests"] if r["status"] is not None}
    companion_ids = {tid for r in expected.values() for tid in r.get("companion_trace_ids", [])}
    previous = previous or {}
    found = {tid: record["spans"] for tid, record in previous.get("requests", {}).items()
             if record.get("linked") and record.get("spans")}
    found.update({tid: record["spans"] for tid, record in previous.get("companion_traces", {}).items()
                  if record.get("retained") and record.get("spans")})
    cached = set(found)
    query_errors = []
    ids = sorted((set(expected) | companion_ids) - cached)
    for offset in range(0, len(ids), 8):
        try:
            response = query(cfg, {"query_type": "TRACE_IDS", "trace_ids": ids[offset:offset + 8]})
        except SystemExit as error:
            query_errors.append({"trace_ids": ids[offset:offset + 8], "error": str(error)})
            continue
        for trace in response.get("traces", []):
            records = unpack(trace)
            if records:
                found[records[0]["trace_id"]] = records
        if offset % 80 == 0:
            print(f"Checked {min(offset + 8, len(ids))}/{len(ids)} known IDs", flush=True)
    checks = {}
    for tid, request in expected.items():
        records = found.get(tid, [])
        app = next((s for s in records if s["span_id"] == request["client_span_id"]), None)
        first = next((s for s in records if s["span_id"] == request["first_backend_span_id"]), None)
        all_ids = {s["span_id"] for s in records}
        missing = [s["span_id"] for s in records if s["parent_id"] and s["parent_id"] != "0" * 16 and s["parent_id"] not in all_ids]
        backend = [s for s in records if s["span_id"] != request["client_span_id"]]
        errors = [s for s in backend if s["status"].get("code") in (2, "STATUS_CODE_ERROR")]
        redis = [s for s in backend if s["attributes"].get("db.system.name") == "redis"]
        mismatches = [s["span_id"] for s in backend if any(str(s["attributes"].get(k)) != str(v) for k, v in request["context"].items())]
        complete = bool(app and first and first["parent_id"] == request["client_span_id"] and not missing)
        checks[tid] = {"retained": bool(records), "linked": complete, "missing_parents": missing,
            "observed_at": previous.get("requests", {}).get(tid, {}).get("observed_at", previous.get("queried_at"))
                if tid in cached else datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "span_count": len(records), "error_count": len(errors), "context_mismatches": mismatches,
            "redis_count": len(redis), "failed_spans": errors, "redis_spans": redis,
            "spans": records,
            "session_url": app["attributes"].get("emb.dashboard_session") if app else None,
            "trace_url": f"https://{cfg['tenant']}.chronosphere.io/traces/trace-detail/by-id/{tid}",
            "first_backend_parent": first["parent_id"] if first else None,
            "sampling": {k: v for s in records for k, v in s["attributes"].items() if "sampling" in k or "dataset" in k}}
    counts = collections.Counter({"sessions": len(sessions), "expected_requests": len(expected),
        "retained": sum(x["retained"] for x in checks.values()), "linked": sum(x["linked"] for x in checks.values()),
        "context_mismatch_requests": sum(bool(x["context_mismatches"]) for x in checks.values()),
        "pre_server_transport_failures": sum(r["status"] is None for s in sessions for r in s["requests"])})
    # Random, bounded, reproducible sessions from the normal mix, selected
    # without inspecting retention or failures. Unknowns stay in denominators.
    rng = random.Random(7331)
    samples = {}
    for profile in ("ecommerce", "banking", "travel"):
        candidates = [s for s in sessions if s["profile"] == profile and not s.get("verification_case") and
                      s["properties"].get("release_ring") == "canary" and s["properties"].get("feature_exposed") == "true"]
        selected = rng.sample(candidates, min(20, len(candidates)))
        rows = []
        for session in selected:
            relevant = [r for r in session["requests"] if r["context"].get("feature_exposed") == "true"]
            verified = [checks[r["trace_id"]] for r in relevant if r["trace_id"] in checks]
            linked = len(verified) == len(relevant) and bool(relevant) and all(x["linked"] for x in verified)
            has_error = any(x["linked"] and x["error_count"] for x in verified)
            rows.append({"session_id": session["session_id"], "known_requests": len(relevant),
                "all_linked": linked, "unverified": not linked, "backend_error": has_error,
                "http_failure": any(r["status"] is None or r["status"] >= 400 for r in relevant),
                "backend_caused_http_failure": any(r["status"] is not None and r["status"] >= 500 and
                    checks.get(r["trace_id"], {}).get("linked") and checks[r["trace_id"]]["error_count"] for r in relevant),
                "redis_context_verified": linked and any(x["redis_count"] for x in verified) and
                    not any(x["context_mismatches"] for x in verified),
                "latency_only": linked and not has_error and session["primary_scenario"] == "slow",
                "http_200_backend_error": any(r["status"] == 200 and checks.get(r["trace_id"], {}).get("linked") and
                    checks[r["trace_id"]]["error_count"] for r in relevant)})
        samples[profile] = {"seed": 7331, "eligible": len(candidates), "denominator": len(rows),
                            "backend_error_numerator": sum(bool(r["backend_error"]) for r in rows),
                            "numerators": {k: sum(bool(r[k]) for r in rows) for k in
                                ("all_linked", "unverified", "backend_error", "backend_caused_http_failure",
                                 "http_200_backend_error", "latency_only", "redis_context_verified")}, "sessions": rows}
    return {"queried_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "stage": "retained_API_verified",
            "dashboard_ui_verified": False, "query_errors": query_errors,
            "reused_previously_verified_traces": len(cached & (set(expected) | companion_ids)),
            "counts": dict(counts), "sessions": sessions, "requests": checks,
            "companion_traces": {tid: {"retained": tid in found, "spans": found.get(tid, [])} for tid in companion_ids},
            "random_canary_samples": samples}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest")
    ap.add_argument("--config", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--settle-seconds", type=int, default=240)
    ap.add_argument("--previous", help="Reuse retained records from an earlier report; observed_at is preserved, missing IDs are queried again")
    a = ap.parse_args()
    prior = json.load(open(a.previous)) if a.previous else None
    result = verify(a.manifest, load_config(a.config)["chronosphere"], a.settle_seconds, prior)
    with open(a.output, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result["counts"], indent=2))
