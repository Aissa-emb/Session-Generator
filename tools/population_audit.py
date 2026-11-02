#!/usr/bin/env python3
"""Measure synthetic visit distributions offline, without credentials or sending."""
import argparse
import copy
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from xcorgen.emitted import percentile
from xcorgen.linter import lint_payloads
from xcorgen.population import INTENTS, VERSION
from xcorgen.runner import load_config
from xcorgen.spec import default_spec, ANDROID_TARGET, IOS_TARGET, WEB_TARGET
from xcorgen.spec_runner import SpecRun
from xcorgen.web_contract import check


def audit(count, seed):
    # Fixed clock; twenty hours lets the returning pool mature without overlaps.
    now = 1793638800000
    rows, platforms = [], []
    for platform, target in (("android", ANDROID_TARGET), ("ios", IOS_TARGET), ("web", WEB_TARGET)):
        s = default_spec()
        s["targets"] = [copy.deepcopy(target)]
        s["population"].update(model="varied", platform_mix={target["id"]: 1},
                               line_of_business_mix={k: 1 for k in INTENTS})
        s["traffic"] = {"mode": "burst", "count": count, "window": {"last_minutes": 1200}}
        random.seed(seed)
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(s, load_config(None), persist=False, now_ms=now, state_dir=state, rng=random.Random(seed))
            for i in range(count):
                start = now - 72000000 + i * 71000000 / count
                t, session, _, deliveries, faults = run.session(start)
                issues = lint_payloads(deliveries)
                if platform == "web":
                    for d in deliveries:
                        if d["target"] == "embrace":
                            check(d["payload"], issues.append)
                if issues:
                    raise ValueError(f"{platform} visit {i}: {issues[:5]}")
                run.record(t, session, faults, deliveries)
            report = run.emitted.summary()
            rows.extend(report["population"])
            observed = run.emitted.observations
            warmed = observed[count // 3:]
            cold = [o for o in observed if o.get("cold_start_ms") is not None]
            platforms.append({"platform": platform, "visits": len(observed), "sdk_sessions": report["sdk_sessions"],
                "returning_share_after_warmup": round(sum(o.get("visitor_type") == "returning" for o in warmed) / len(warmed), 4),
                "cold_start_p75_ms_by_tier": {tier: round(percentile([o["cold_start_ms"] for o in cold if o.get("hardware_tier") == tier], .75) or 0, 1)
                                              for tier in ("flagship", "mid", "budget")},
                "payload_validation": "passed"})
    return {"model_version": VERSION, "seed": seed, "visits": count * 3,
            "evidence": "Serialized local payloads; no remote sends or dashboard verification.",
            "intent_weights": INTENTS, "platforms": platforms, "cohorts": rows}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("--count", type=int, default=1200, help="Visits per platform (at least 300)")
    p.add_argument("--seed", type=int, default=8219)
    a = p.parse_args()
    if not 300 <= a.count <= 10000:
        p.error("Use 300–10000 visits per platform.")
    result = audit(a.count, a.seed)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["platforms"], indent=2))
