"""Versioned stdlib distribution contract. No wire-payload mutation or global RNG."""
import hashlib
import json
import math
import random
import statistics
from functools import lru_cache

VERSION = "1.0"
# Param label, default. Bounds are semantic and validated below, not slider promises.
FAMILIES = {
    "constant": ("Constant", "A fixed value.", {"value": ("Value", 100)}),
    "normal": ("Normal", "Symmetric spread around the mean.", {"mean": ("Mean", 100), "stddev": ("Standard deviation", 20)}),
    "right_skew": ("Right-skew normal", "A longer right tail. Location is not the mean.", {"location": ("Location", 50), "scale": ("Scale", 80), "skew": ("Skew", 6)}),
    "left_skew": ("Left-skew normal", "A longer left tail. Location is not the mean.", {"location": ("Location", 200), "scale": ("Scale", 50), "skew": ("Skew", 6)}),
    "lognormal": ("Lognormal", "Positive durations with occasional slow observations.", {"mu": ("Log-space mean", math.log(100)), "sigma": ("Log-space standard deviation", .7)}),
    "bimodal": ("Two populations", "Two Gaussian components; overlap may produce a single peak.", {"mean1": ("First mean", 50), "stddev1": ("First spread", 10), "mean2": ("Second mean", 250), "stddev2": ("Second spread", 30), "weight": ("First population share", .7)}),
    "bounded_left_skew": ("Bounded left-skew", "Beta distribution concentrated toward the upper limit.", {"alpha": ("Alpha", 5), "beta": ("Beta", 2), "low": ("Lower limit", 0), "high": ("Upper limit", 300)}),
    "bounded_right_skew": ("Bounded right-skew", "Beta distribution concentrated toward the lower limit.", {"alpha": ("Alpha", 2), "beta": ("Beta", 5), "low": ("Lower limit", 0), "high": ("Upper limit", 300), "upper_mass": ("Share at upper limit", 0)}),
}
QUANTITIES = {
    "reading_ms": {"name": "Reading pause", "unit": "ms", "max": 120000, "median": 6000, "p95": 15000, "description": "Time reading between eligible user actions."},
    "typing_ms": {"name": "Typing per character", "unit": "ms", "max": 5000, "median": 300, "p95": 700, "description": "Per-character input time; does not change network deadlines."},
    "network_rtt_ms": {"name": "API round-trip transport", "unit": "ms", "max": 5000, "median": 45, "p95": 180, "description": "Request and response transit, separate from server work and transfer size."},
    "backend_work_ms": {"name": "Gateway processing", "unit": "ms", "max": 30000, "median": 2, "p95": 8, "description": "Gateway's own work. Child service work is retained and parents are laid out again."},
}


def rng_for(*parts):
    key = json.dumps([VERSION, *parts], sort_keys=True, separators=(",", ":"))
    return random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest(), "big"))


def lognormal(median, p95):
    if not 0 < median <= p95 or not all(math.isfinite(n) for n in (median, p95)):
        raise ValueError("Typical duration must be positive and no greater than p95.")
    return {"family": "lognormal", "parameters": {"mu": math.log(median), "sigma": math.log(p95 / median) / statistics.NormalDist().inv_cdf(.95)}}


def default(quantity):
    q = QUANTITIES[quantity]
    return dict(lognormal(q["median"], q["p95"]), bounds={"low": 0, "high": q["max"], "policy": "truncate"})


def metadata():
    return {"version": VERSION, "quantities": QUANTITIES, "families": {
        key: {"name": name, "description": desc, "parameters": {k: {"label": v[0], "default": v[1]} for k, v in params.items()}}
        for key, (name, desc, params) in FAMILIES.items()}, "defaults": {q: default(q) for q in QUANTITIES}}


def validate(config, maximum=None):
    """Strict names, finite numbers, feasible support; returns human-readable errors."""
    if not isinstance(config, dict): return ["Choose a distribution."]
    errors = []
    if set(config) - {"family", "parameters", "bounds"}: errors.append("Unknown distribution setting.")
    family = config.get("family")
    if not isinstance(family, str) or family not in FAMILIES: return errors + ["Unknown distribution family."]
    p = config.get("parameters", {})
    required = set(FAMILIES[family][2])
    if not isinstance(p, dict) or set(p) != required:
        return errors + ["Parameters must be exactly: " + ", ".join(sorted(required)) + "."]
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1e9 for x in p.values()):
        return errors + ["Use finite parameters within ±1 billion."]
    for key in ("stddev", "stddev1", "stddev2", "sigma", "scale"):
        if key in p and p[key] < 0: errors.append(key + " must be nonnegative.")
    for key in ("alpha", "beta", "skew"):
        if key in p and not 0 < p[key] <= 1000: errors.append(key + " must be above zero and at most 1,000.")
    for key in ("weight", "upper_mass"):
        if key in p and not 0 <= p[key] <= 1: errors.append(key + " must be between 0 and 1.")
    if family.startswith("bounded"):
        if p["high"] <= p["low"]: errors.append("Upper limit must exceed lower limit.")
        if (family == "bounded_left_skew" and p["alpha"] <= p["beta"]) or (family == "bounded_right_skew" and p["alpha"] >= p["beta"]):
            errors.append("Alpha/beta must match the selected skew direction.")
    if family == "lognormal" and (abs(p["mu"]) > 50 or p["sigma"] > 10):
        errors.append("Use a log-space mean within ±50 and spread at most 10.")
    b = config.get("bounds", {})
    if not isinstance(b, dict) or set(b) - {"low", "high", "policy"}: return errors + ["Invalid bounds."]
    low, high = b.get("low", 0), b.get("high", maximum if maximum is not None else 1e9)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (low, high)):
        return errors + ["Bounds must be finite numbers."]
    if not 0 <= low < high <= (maximum if maximum is not None else 1e9): errors.append("Bounds must be nonnegative, ordered and within the quantity's limit.")
    if b.get("policy", "truncate") not in ("truncate", "clip"): errors.append("Choose truncation or clipping.")
    if not errors:
        # Probe feasibility deterministically; also rejects effectively impossible truncation.
        try:
            rng = rng_for("validation", config)
            for _ in range(8): draw(config, rng, maximum)
        except ValueError as exc: errors.append(str(exc))
    return errors


def raw(config, rng):
    f, p = config["family"], config["parameters"]
    if f == "constant": return p["value"]
    if f == "normal": return rng.gauss(p["mean"], p["stddev"])
    if f in ("left_skew", "right_skew"):
        delta = p["skew"] / math.sqrt(1 + p["skew"] ** 2)
        z = delta * abs(rng.gauss(0, 1)) + math.sqrt(1 - delta * delta) * rng.gauss(0, 1)
        return p["location"] + p["scale"] * z * (-1 if f == "left_skew" else 1)
    if f == "lognormal": return math.exp(p["mu"] + p["sigma"] * rng.gauss(0, 1))
    if f == "bimodal":
        k = "1" if rng.random() < p["weight"] else "2"
        return rng.gauss(p["mean" + k], p["stddev" + k])
    if f == "bounded_right_skew" and rng.random() < p["upper_mass"]: return p["high"]
    return p["low"] + (p["high"] - p["low"]) * rng.betavariate(p["alpha"], p["beta"])


def draw(config, rng, maximum=None):
    b = config.get("bounds", {})
    low, high = b.get("low", 0), b.get("high", maximum if maximum is not None else 1e9)
    for attempt in range(256):
        value = raw(config, rng)
        if low <= value <= high: return value, attempt, False
        if b.get("policy", "truncate") == "clip": return min(high, max(low, value)), 0, True
    raise ValueError("Distribution has negligible probability within its bounds; widen them or choose clipping.")


def quantile(values, q):
    if not values: return None
    xs = sorted(values); p = (len(xs) - 1) * q; i = int(p)
    return xs[i] + (xs[min(i + 1, len(xs) - 1)] - xs[i]) * (p - i)


def stats(values):
    if not values: return {"count": 0}
    return {"count": len(values), "mean": statistics.fmean(values), "min": min(values), "max": max(values),
            **{f"p{int(q * 100)}": quantile(values, q) for q in (.5, .75, .9, .95, .99)}}


@lru_cache(maxsize=128)
def knee_value(encoded, knee, maximum):
    cfg = json.loads(encoded); rng = rng_for("tail-threshold", cfg)
    return quantile([draw(cfg, rng, maximum)[0] for _ in range(4096)], knee)


def transform(value, config, effect, maximum):
    if not effect: return value, False
    factor = effect.get("factor", 1)
    if effect["kind"] == "scale": result = value * factor
    else:
        threshold = knee_value(json.dumps(config, sort_keys=True), effect.get("knee", .8), maximum)
        result = value + max(0, value - threshold) * (factor - 1)
    high = config.get("bounds", {}).get("high", maximum)
    low = config.get("bounds", {}).get("low", 0)
    bounded = min(high, max(low, result))
    return bounded, bounded != result


def preview(config, quantity, effect=None, count=2048, seed=7331):
    if not isinstance(quantity, str) or quantity not in QUANTITIES: raise ValueError("Unknown quantity.")
    maximum = QUANTITIES[quantity]["max"]
    errors = validate(config, maximum)
    if errors: raise ValueError(" ".join(errors))
    if effect is not None and (not isinstance(effect, dict) or set(effect) - {"kind", "factor", "knee"} or effect.get("kind") not in ("scale", "tail")
                   or any(isinstance(effect.get(k, v), bool) or not isinstance(effect.get(k, v), (int, float)) or not math.isfinite(effect.get(k, v)) for k, v in (("factor", 1), ("knee", .8)))
                   or not 1 <= effect.get("factor", 1) <= 20 or not .5 <= effect.get("knee", .8) <= .99):
        raise ValueError("Invalid effect: factor 1–20, tail knee 0.5–0.99.")
    rng = rng_for("numerical-preview", seed); before, after = [], []; rejected = clipped = 0
    for _ in range(min(4096, max(1, count))):
        x, attempts, clip = draw(config, rng, maximum); y, constrained = transform(x, config, effect, maximum)
        before.append(x); after.append(y); rejected += attempts; clipped += clip or constrained
    lo, hi = min(before + after), max(before + after)
    width = (hi - lo) / 24 if hi > lo else 1
    bins = [{"low": lo + i * width, "high": lo + (i + 1) * width, "baseline": 0, "affected": 0} for i in range(24)]
    for label, values in (("baseline", before), ("affected", after)):
        for x in values: bins[min(23, int((x - lo) / width))][label] += 1
    return {"version": VERSION, "quantity": quantity, "unit": "ms", "baseline": stats(before), "affected": stats(after),
            "histogram": bins, "resampled": rejected, "bounded": clipped, "count": len(before),
            "scope": "raw_model_draws", "seed": seed}
