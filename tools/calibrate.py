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
        gen = Generator([profile], cfg, persist=False, backend_logs=True)
        for pool in gen.pools.values():
            pool.personas = []
            pool.size = n + 1
        counts = collections.Counter()
        families = collections.Counter()
        examples = {}
        for index in range(n):
            s, _, deliveries = gen.session(1793300000000 + index * 20000)
            m = measure(s, deliveries)
            families.update(m.pop("crash_families"))
            counts.update({k: int(v) for k, v in m.items()})
            for r in s.plan.evidence:
                if r["incident"]:
                    examples.setdefault(r["incident"], {"session_id": s.user_session_id, "request": r})
        rates = {k: round(counts[k] * 100 / n, 3) for k in ("issue", "crash", "blockage", "handled", "mobile_failed",
                    "backend_degraded_200", "slow", "flow_eligible", "priority_started")}
        rates["failed_attempts"] = round(counts["failed_attempts"] * 100 / max(1, counts["attempts"]), 3)
        rates["canary_backend_error"] = round(counts["canary_backend_error"] * 100 / max(1, counts["canary_exposed"]), 3)
        output["profiles"][profile] = {"counts": dict(counts), "rates_percent": rates,
                                       "crash_families": dict(families), "incident_examples": examples}
        print(profile, json.dumps(rates), flush=True)
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
