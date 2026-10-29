#!/usr/bin/env python3
"""Generate a reproducible, in-memory corpus and measure emitted evidence.

No data is exported and no persona state is saved. This is a model/schema
check, not proof that backend spans were retained or that issue views exist.
"""
import argparse
import collections
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xcorgen.runner import Generator, load_config
from xcorgen.milestones import FLOWS


def attrs(record):
    return {a["key"]: next(iter(a["value"].values())) if isinstance(a["value"], dict) else a["value"]
            for a in record.get("attributes", [])}


def timeline_metrics(deliveries):
    """Count emitted Android timeline records, including tied timestamps.

    Span rows precede breadcrumb rows on a tie: this conservatively catches
    breadcrumbs clustered around an operation's completion and next start.
    """
    timeline, roots, failed_roots = [], 0, 0
    for d in deliveries:
        if d.get("fmt") != "envelope":
            continue
        for span in d["payload"]["data"].get("spans", []):
            a = attrs(span)
            if a.get("emb.type") in ("perf", "perf.network_request", "ux.view", "perf.thread_blockage"):
                timeline.append((span["start_time_unix_nano"], False))
            if a.get("app.operation") and span.get("parent_span_id") in (None, "", "0" * 16):
                roots += 1
                failed_roots += span["status"] == "Error"
            for event in span.get("events", []):
                if event["name"] in ("emb-breadcrumb", "emb-ui-tap"):
                    timeline.append((event["time_unix_nano"], event["name"] == "emb-breadcrumb"))
    longest = run = 0
    for _, crumb in sorted(timeline):
        run = run + 1 if crumb else 0
        longest = max(longest, run)
    return {"root_spans": roots, "failed_root_spans": failed_roots,
            "max_consecutive_breadcrumbs": longest}


def measure(s, deliveries):
    records = s.plan.evidence
    failed = [r for r in records if r["status"] is None or r["status"] >= 400]
    hidden = [r for r in records if r["status"] is not None and r["status"] < 400 and r["backend_errors"]]
    crash, handled, blockage = set(), False, False
    for d in deliveries:
        if d["target"] != "embrace" or d.get("fmt") != "envelope":
            continue
        for log in d["payload"]["data"].get("logs", []):
            a = attrs(log)
            if a.get("emb.type") == "sys.android.crash":
                crash.add(a["exception.type"] + ":" + json.loads(a["exception.stacktrace"])[0])
            handled |= a.get("emb.type") == "sys.exception" and a.get("emb.exception_handling") == "handled"
        for span in d["payload"]["data"].get("spans", []):
            blockage |= attrs(span).get("emb.type") == "perf.thread_blockage"
    # Endpoint-specific user-visible budgets, measured from physical timings.
    budgets = {"get_product": 1500, "get_recommendations": 1000, "accounts": 1400,
               "statement": 3000, "create_transfer": 2000, "flight_search": 3500,
               "place_order": 6000, "create_booking": 7000}
    slow = any(r["end"] - r["start"] >= budgets.get(r["key"], float("inf")) for r in records)
    marks = getattr(s, "milestones", [])
    names = {m["name"] for m in marks}
    starts = sum(start in names for _, start, _, _ in FLOWS[s.profile.key])
    priority_start, priority_end = FLOWS[s.profile.key][2][1:3]
    exposed = [r for r in records if r["context"].get("feature_exposed") == "true"]
    return {"issue": bool(failed or hidden or crash or handled or blockage or slow),
            "client_issue": bool(failed or crash or handled or blockage),
            "crash": bool(crash), "crash_families": sorted(crash), "blockage": blockage,
            "handled": handled, "mobile_failed": bool(failed), "attempts": len(records), "failed_attempts": len(failed),
            "business_conflicts": sum(r["status"] == 409 for r in records),
            "cancelled_attempts": sum(r["transport"] == "cancelled" for r in records),
            "backend_degraded_200": bool(hidden), "slow": slow,
            "flow_eligible": bool(starts), "flow_starts": starts, "priority_started": priority_start in names,
            "priority_completed": priority_end in names, "breadcrumbs": len(marks),
            "canary_exposed": s.plan.ring == "canary" and bool(exposed),
            "canary_backend_error": s.plan.ring == "canary" and any(r["backend_errors"] for r in exposed)}


def run(n, seed):
    output = {"provenance": "synthetic", "scope": "locally generated evidence; retention/dashboard unverified",
              "seed": seed, "sessions_per_profile": n, "profiles": {}}
    for i, profile in enumerate(("ecommerce", "banking", "travel")):
        random.seed(seed + i)
        cfg = load_config(None)
        cfg["embrace"]["apps"] = {"default": "dryrn"}
        cfg["generation"] = {"incident_phase": "incident"}
        gen = Generator([profile], cfg, persist=False, backend_logs=True)
        for pool in gen.pools.values():
            pool.personas = []
            pool.size = n + 1
        counts = collections.Counter()
        families = collections.Counter()
        examples = {}
        flows = {name: {"starts": 0, "success_endpoints": 0, "duplicate_starts": 0,
                       "success_after_timeout": 0, "incomplete_with_crash": 0, "durations_ms": []}
                 for name, _, _, _ in FLOWS[profile]}
        breadcrumb_counts = {"short": [], "engaged": [], "priority": [], "interrupted": []}
        largest_part = max_consecutive = 0
        root_counts, failed_root_counts = [], []
        assigned_users = {"stable": set(), "canary": set()}
        cohort_sessions = collections.Counter()
        exposed_sessions = collections.Counter()
        for index in range(n):
            s, _, deliveries = gen.session(1793300000000 + index * 20000)
            m = measure(s, deliveries)
            timeline = timeline_metrics(deliveries)
            max_consecutive = max(max_consecutive, timeline["max_consecutive_breadcrumbs"])
            root_counts.append(timeline["root_spans"])
            failed_root_counts.append(timeline["failed_root_spans"])
            assigned_users[s.plan.ring].add(s.persona["device_id"])
            cohort_sessions[s.plan.ring] += 1
            if s.properties.get("feature_exposed") == "true":
                exposed_sessions[s.plan.ring] += 1
            families.update(m.pop("crash_families"))
            counts.update({k: int(v) for k, v in m.items()})
            events = [e for d in deliveries if d.get("fmt") == "envelope"
                      for span in d["payload"]["data"].get("spans", [])
                      for e in span.get("events", []) if e["name"] == "emb-breadcrumb"]
            kind = "interrupted" if m["crash"] else "priority" if m["priority_started"] else "engaged" if m["flow_eligible"] else "short"
            breadcrumb_counts[kind].append(len(events))
            for d in deliveries:
                if d.get("fmt") == "envelope":
                    for span in d["payload"]["data"].get("spans", []):
                        largest_part = max(largest_part, sum(e["name"] == "emb-breadcrumb" for e in span.get("events", [])))
            for name, start, end, timeout in FLOWS[profile]:
                row = flows[name]
                starts = [e for e in events if attrs(e).get("message") == start]
                ends = [e for e in events if attrs(e).get("message") == end]
                row["starts"] += len(starts)
                row["duplicate_starts"] += max(0, len(starts) - 1)
                if starts and ends:
                    duration = (ends[-1]["time_unix_nano"] - starts[0]["time_unix_nano"]) / 1e6
                    row["success_endpoints"] += 1
                    row["success_after_timeout"] += duration > timeout * 1000
                    row["durations_ms"].append(duration)
                elif starts and m["crash"]:
                    row["incomplete_with_crash"] += 1
            for r in s.plan.evidence:
                if r["incident"]:
                    examples.setdefault(r["incident"], {"session_id": s.user_session_id, "request": r})
        rates = {k: round(counts[k] * 100 / n, 3) for k in ("issue", "client_issue", "crash", "blockage", "handled", "mobile_failed",
                    "backend_degraded_200", "slow", "flow_eligible", "priority_started")}
        rates["failed_attempts"] = round(counts["failed_attempts"] * 100 / max(1, counts["attempts"]), 3)
        rates["canary_backend_error"] = round(counts["canary_backend_error"] * 100 / max(1, counts["canary_exposed"]), 3)
        def distribution(values):
            values = sorted(values)
            return {"count": len(values), "min": min(values, default=0), "max": max(values, default=0),
                    "p50": round(values[len(values) // 2], 2) if values else 0,
                    "p95": round(values[min(len(values) - 1, int(len(values) * .95))], 2) if values else 0}
        for row in flows.values():
            row["durations_ms"] = distribution(row["durations_ms"])
            row["starts_per_hour_at_10_total_even_profiles"] = round(10 * 60 / 3 * row["starts"] / n, 2)
        output["profiles"][profile] = {"counts": dict(counts), "rates_percent": rates,
                                       "crash_families": dict(families), "incident_examples": examples,
                                       "flow_evidence": flows, "largest_part_breadcrumb_count": largest_part,
                                       "max_consecutive_breadcrumbs": max_consecutive,
                                       "root_span_distribution": distribution(root_counts),
                                       "failed_root_span_distribution": distribution(failed_root_counts),
                                       "cohorts": {"assigned_device_identities": {k: len(v) for k, v in assigned_users.items()},
                                                   "sessions": dict(cohort_sessions), "exposed_sessions": dict(exposed_sessions)},
                                       "breadcrumb_distributions": {k: distribution(v) for k, v in breadcrumb_counts.items()}}
        print(profile, json.dumps(rates), flush=True)
    aggregate = collections.Counter()
    for profile in output["profiles"].values():
        aggregate.update(profile["counts"])
    output["aggregate"] = {"sessions": 3 * n, "counts": dict(aggregate),
                           "issue_pct": round(aggregate["issue"] * 100 / (3 * n), 3),
                           "crash_free_pct": round(100 - aggregate["crash"] * 100 / (3 * n), 3),
                           "failed_attempt_pct": round(aggregate["failed_attempts"] * 100 / aggregate["attempts"], 3)}
    output["representative_phase"] = "incident; scheduled onset/recovery windows are controls, excluded from this calibration"
    output["flow_metrics_scope"] = "emitted breadcrumbs only; native completion/error/abandonment require definitions and fresh processed data"
    return output


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=7331)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    result = run(a.sessions, a.seed)
    with open(a.output, "w") as f:
        json.dump(result, f, indent=2)
