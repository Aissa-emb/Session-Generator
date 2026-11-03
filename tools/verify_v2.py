#!/usr/bin/env python3
"""Read-only verification of every known V2 trace ID after forwarding settles."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xcorgen import spec, secrets
from xcorgen.runner import load_config
from server.verification import manifest_sessions, traces


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("spec")
    p.add_argument("manifest")
    p.add_argument("--out", required=True)
    p.add_argument("--settle-seconds", type=int, default=300)
    p.add_argument("--config")
    a = p.parse_args()
    s = spec.normalized(spec.load(a.spec))
    cfg = load_config(a.config)
    token = (
        secrets.resolve(s["chronosphere"]["token_ref"])
        if (s.get("chronosphere") or {}).get("token_ref")
        else cfg["chronosphere"].get("api_token")
    )
    records = manifest_sessions(a.manifest)
    mature = [
        r
        for r in records
        if r["end_ms"] <= time.time() * 1000 - a.settle_seconds * 1000
    ]
    result = traces(s, mature, token)
    result["settling_sessions"] = len(records) - len(mature)
    Path(a.out).write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "state",
                    "expected",
                    "retained",
                    "complete",
                    "groups",
                    "settling_sessions",
                )
                if k in result
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
