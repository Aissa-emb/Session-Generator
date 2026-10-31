"""Partition one scenario over independently deployed geographic runners."""

import copy
import hashlib
import math
import re
from .spec import validate, errors
from .security import validate_endpoint
from .journal import digest


def plan(spec, runners):
    if not isinstance(runners, list) or not 1 <= len(runners) <= 16:
        raise ValueError("Provide 1–16 regional runners.")
    names = set()
    total = 0
    for runner in runners:
        region = runner["region"]
        weight = runner.get("weight", 1)
        if not re.fullmatch("[a-z][a-z0-9-]{1,40}", region) or region in names:
            raise ValueError("Regions must have unique lowercase deployment labels.")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight <= 0
        ):
            raise ValueError("Runner weights must be positive finite numbers.")
        validate_endpoint(runner["url"], {"Authorization": "Bearer protected"})
        if not re.fullmatch("[A-Z][A-Z0-9_]{1,80}", runner["token_env"]):
            raise ValueError(
                "Each runner needs a token environment variable name, not a token."
            )
        names.add(region)
        total += weight
    key = digest({"spec": spec, "runners": runners})[:24]
    live = spec["traffic"]["mode"] == "live"
    field = "rate_per_minute" if live else "count"
    size = spec["traffic"][field]
    values = [size * r.get("weight", 1) / total for r in runners]
    if not live:
        whole = [math.floor(v) for v in values]
        for i in sorted(
            range(len(values)), key=lambda i: values[i] - whole[i], reverse=True
        )[: size - sum(whole)]:
            whole[i] += 1
        values = whole
    shards = []
    for runner, value in zip(runners, values):
        if value == 0:
            continue
        s = copy.deepcopy(spec)
        s["traffic"][field] = value
        s["seed"] = int(
            hashlib.sha256((key + runner["region"]).encode()).hexdigest()[:8], 16
        )
        s["name"] = (s.get("name", "Regional traffic") + " / " + runner["region"])[:120]
        issues = errors(validate(s))
        if issues:
            raise ValueError("Regional allocation is invalid: " + str(issues))
        shards.append(
            dict(runner, spec=s, idempotency_key=key + ":" + runner["region"])
        )
    return {
        "id": key,
        "total": size,
        "unit": field,
        "runners": shards,
        "geography": "Deploy each runner in its named region with independent storage and egress. Cortex XCOR RUM derives location from the actual sender IP; labels do not change location.",
    }
