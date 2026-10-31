#!/usr/bin/env python3
"""Plan regional allocations, then apply/retry confirmed deployments with local receipts.

Connections file: [{region,url,token_env,weight}]. Tokens never appear in plans.
"""

import argparse
import fcntl
import tempfile
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xcorgen import regions, spec
from xcorgen.security import post, validate_headers


def apply(plan, state, confirm=False, dry=True, transport=post):
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _apply(plan, state, confirm, dry, transport)


def _apply(plan, state, confirm=False, dry=True, transport=post):
    if confirm is not True:
        raise ValueError(
            "Review the plan and provide --confirm to start regional jobs."
        )
    results = (
        json.loads(state.read_text())
        if state.exists()
        else {"plan_id": plan["id"], "runners": {}}
    )
    if results["plan_id"] != plan["id"]:
        raise ValueError("Receipt file belongs to another regional plan.")

    def save():
        state.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".regional-", dir=state.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(results, out, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, state)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    for row in plan["runners"]:
        result = results["runners"].setdefault(row["region"], {})
        if result.get("job"):
            if result.get("dry_run") != dry:
                raise ValueError(
                    "A regional dry run cannot be reused as live; create a separate plan receipt file."
                )
            continue
        token = os.environ.get(row["token_env"])
        if not token:
            raise ValueError(
                "Set " + row["token_env"] + " securely before applying this plan."
            )
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        validate_headers(headers)

        def call(path, body):
            return json.loads(
                transport(
                    row["url"].rstrip("/") + "/api/" + path,
                    json.dumps(body).encode(),
                    headers,
                )
            )

        if not result.get("spec_id"):
            result["spec_id"] = call("specs", row["spec"])["id"]
            save()
        live = row["spec"]["traffic"]["mode"] == "live"
        body = {
            "spec_id": result["spec_id"],
            "confirm": True,
            "dry_run": dry,
            "idempotency_key": row["idempotency_key"] + (":dry" if dry else ":live"),
        }
        if live:
            body["name"] = row["spec"]["name"]
        result["job"] = call("streams" if live else "runs", body)
        result["dry_run"] = dry
        save()
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("plan")
    a.add_argument("spec")
    a.add_argument("connections")
    a.add_argument("--out", required=True)
    a = sub.add_parser("apply")
    a.add_argument("plan")
    a.add_argument("--receipts", required=True)
    a.add_argument("--confirm", action="store_true")
    a.add_argument(
        "--live", action="store_true", help="Send telemetry; omission creates dry runs"
    )
    a = p.parse_args()
    if a.command == "plan":
        result = regions.plan(
            spec.load(a.spec), json.loads(Path(a.connections).read_text())
        )
        Path(a.out).write_text(json.dumps(result, indent=2) + "\n")
        print("Review regional plan:", a.out)
    else:
        result = apply(
            json.loads(Path(a.plan).read_text()),
            Path(a.receipts),
            a.confirm,
            not a.live,
        )
        print("Regional jobs recorded:", len(result["runners"]))


if __name__ == "__main__":
    main()
