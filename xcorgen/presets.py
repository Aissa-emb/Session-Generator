"""Preset library: spec fragments a sales engineer can apply, combine and adjust.

Each preset is a partial ScenarioSpec plus a one-line outcome. apply() merges
a fragment into a spec (lists are appended, IDs stay unique), so presets
compose into stories. "baseline" re-expresses V1's default session policy as
explicit fault rates, computed from the V1 constants.
"""
import copy
import time

from . import catalog, scenarios
from .spec import iso

ANDROID = "android"


def _story_rates():
    """V1 primary-scenario shares per release ring, with canary allocation and transport failures."""
    weights, share = scenarios.DEFAULT_WEIGHTS, .25
    canary = {"clean": .20, "mobile": .20, "backend": .60}
    stable = {k: (v - share * canary.get(k, 0)) / (1 - share) for k, v in weights.items()}
    return {"stable": stable, "canary": {k: canary.get(k, 0) for k in weights}}


def baseline():
    """V1's default distribution as fault rates (seeded policy, canary cohort, hourly pool incident)."""
    rings = _story_rates()
    transport = .4
    faults, client = [], []
    per_profile = {}
    for profile, stories in scenarios.INCIDENTS.items():
        lob = catalog.LOB_FOR[profile]
        for story, incidents in stories.items():
            for incident in incidents:
                for ring, mix in rings.items():
                    rate = mix[story] / len(incidents)
                    if story == "mobile" and ring == "stable":
                        rate *= 1 - transport
                    if rate:
                        per_profile.setdefault((incident, story), {}).setdefault(ring, {})[lob] = rate
    variant_for = {(k, v["story"]): name for name, v in catalog.BACKEND_VARIANTS.items() for k in [v["v1_incident"]]}
    for (incident, story), by_ring in sorted(per_profile.items()):
        if incident in catalog.HANDLED_ONLY_INCIDENTS.values():
            continue
        variant = variant_for[(incident, story)]
        v = catalog.BACKEND_VARIANTS[variant]
        same = len(by_ring) == 2 and all(abs(by_ring["stable"].get(l, 0) - by_ring["canary"].get(l, 0)) < 1e-9
                                         for l in set(by_ring["stable"]) | set(by_ring["canary"]))
        groups = [(None, by_ring["stable"])] if same else sorted(by_ring.items())
        for ring, lobs in groups:
            for rate, lob_set in _by_rate(lobs):
                entry = {"id": f"baseline-{variant}" + (f"-{ring}" if ring else "") + (f"-{'-'.join(lob_set)}" if len(_by_rate(lobs)) > 1 else ""),
                         "type": v["type"], "variant": variant, "rate": round(rate, 6), "exclusive_group": "story",
                         "lines_of_business": lob_set}
                if ring:
                    entry["filters"] = {"release_rings": [ring]}
                if story == "mobile":
                    entry["details"] = {"handled_followup": .6}
                if variant == "payments-db-pool":
                    entry["schedule"] = "payments-pool-hourly"
                faults.append(entry)
    stable = rings["stable"]
    client.append({"id": "baseline-network-failure", "type": "network_failure", "rate": round(stable["mobile"] * transport, 6),
                   "exclusive_group": "story", "filters": {"release_rings": ["stable"]},
                   "details": {"kinds": ["dns", "offline", "reset", "timeout", "cancelled"], "story": "mobile",
                               "handled_followup": .6}})
    client.append({"id": "baseline-handled-story", "type": "handled_exception", "rate": round(stable["handled"], 6),
                   "exclusive_group": "story", "filters": {"release_rings": ["stable"], "lines_of_business": ["banking", "travel"]},
                   "details": {"story": "handled"}})
    client.append({"id": "baseline-crash", "type": "jvm_crash", "rate": .10, "exclusive_group": "client", "steer_journey": True})
    client.append({"id": "baseline-blockage", "type": "main_thread_blockage", "rate": .30, "exclusive_group": "client"})
    return {
        "client_faults": client, "backend_faults": faults,
        "incidents": [{"id": "payments-pool-hourly", "fault_ref": "baseline-payments-db-pool", "start": "2026-01-01T00:00:00Z",
                       "onset_minutes": 2, "peak_minutes": 52, "recovery_minutes": 6, "repeat_every_minutes": 60}],
        "rollouts": [{"id": "payment-orchestration-v2", "kind": "feature_cohort", "subject": "payment_orchestration",
                      "canary_share": .25, "cohort_seed": "vantra-2026-10"}],
    }


def _by_rate(lobs):
    groups = {}
    for lob, rate in sorted(lobs.items()):
        groups.setdefault(round(rate, 9), []).append(lob)
    return sorted(groups.items())


def _incident_start(now_ms, minutes_from_now):
    return iso((now_ms // 60000 + minutes_from_now) * 60000)


def _backend(variant, rate=.3, **extra):
    v = catalog.BACKEND_VARIANTS[variant]
    entry = {"id": variant, "type": v["type"], "variant": variant, "rate": rate}
    if v["story"] == "mobile":
        entry["details"] = {"handled_followup": .6}
    entry.update(extra)
    return {"backend_faults": [entry]}


def catalog_presets(now_ms=None):
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    p = {
        "baseline": ("Baseline", "Stories", "Reproduces V1's default mix: about 85% of sessions carry a backend or client story, "
                     "10% crash and 30% show a main-thread freeze.", baseline()),
        "release-regression": ("Release regression", "Android",
            "10.28.0 crashes with a NullPointerException in BookingConfirmationMapper for a third of sessions that reach "
            "booking confirmation; earlier versions are clean. Set rollback_at on the rollout to stop it.",
            {"client_faults": [{"id": "booking-confirmation-npe", "type": "jvm_crash", "rate": 1 / 3, "steer_journey": False,
                                "filters": {"lines_of_business": ["travel"], "app_versions": ["10.28.0"], "endpoints": ["create_booking"]},
                                "details": {"family": 0}}],
             "rollouts": [{"id": "release-10-28-0", "kind": "app_version", "subject": "10.28.0"}]}),
        "anr-results-lists": ("Freezes on results lists", "Android",
            "Budget devices freeze for 6–12 seconds while rendering product and hotel results.",
            {"client_faults": [{"id": "results-list-freeze", "type": "main_thread_blockage", "rate": .45,
                                "filters": {"lines_of_business": ["retail", "travel"], "device_tiers": ["budget"]},
                                "details": {"cause": "layout", "min_ms": 6000, "max_ms": 12000}}]}),
        "handled-exception-storm": ("Handled exception storm", "Android",
            "A backend response change triggers handled parsing exceptions in a third of sessions, with no crashes.",
            {"client_faults": [{"id": "amount-parse-errors", "type": "handled_exception", "rate": .35}]}),
        "slow-cold-start": ("Slow cold start", "Android",
            "After a dependency update, cold starts on budget devices take 4–8 s, spent in application init.",
            {"client_faults": [{"id": "budget-cold-start", "type": "slow_startup", "rate": .6,
                                "filters": {"device_tiers": ["budget"]},
                                "details": {"phase": "application_init", "min_ms": 4200, "max_ms": 7800}}]}),
        "flaky-network": ("Flaky network", "Android",
            "Client timeouts and connection resets without an HTTP response in 15% of sessions; retries succeed.",
            {"client_faults": [{"id": "client-timeouts", "type": "network_failure", "rate": .15,
                                "details": {"kinds": ["timeout", "reset", "dns", "offline"]}}]}),
        "slow-critical-flow": ("Slow critical flow", "Android",
            "Payment authorization root spans for checkout and booking double, to a p95 of about 5 s.",
            {"client_faults": [
                {"id": "slow-checkout-authorization", "type": "slow_root_span", "rate": .5,
                 "filters": {"lines_of_business": ["retail"]}, "details": {"span": "authorize_payment", "p50_ms": 2600, "p95_ms": 5200}},
                {"id": "slow-booking-authorization", "type": "slow_root_span", "rate": .5,
                 "filters": {"lines_of_business": ["travel"]}, "details": {"span": "authorize_booking", "p50_ms": 2800, "p95_ms": 5600}}]}),
    }
    names = {"payments-db-pool": "Payments pool exhaustion", "payment-timeouts": "Card processor timeouts",
             "reviews-n-plus-one": "Review media N+1", "recs-latency": "Recommendation latency",
             "recs-fallback": "Recommendation fallback", "inventory-sync": "Inventory sync conflicts",
             "inventory-deadlock": "Stock reservation deadlocks", "fraud-latency": "Fraud model timeouts",
             "fraud-slow": "Fraud model latency", "rails-outage": "Instant-payments outage",
             "core-banking-latency": "Core banking latency", "account-read-fallback": "Account read fallback",
             "statements-slow": "Statement rendering latency", "supplier-timeouts": "Supplier timeouts",
             "gds-pnr-timeouts": "PNR creation timeouts", "hotel-supplier-outage": "Hotel supplier outage",
             "search-complexity": "Complex search latency"}
    for variant, title in names.items():
        p[variant] = (title, "Backend", catalog.BACKEND_VARIANTS[variant]["summary"], _backend(variant))
    outage = _backend("payments-db-pool", rate=.4)
    outage["incidents"] = [{"id": "payments-outage", "fault_ref": "payments-db-pool", "start": _incident_start(now_ms, 3),
                            "onset_minutes": 3, "peak_minutes": 20, "recovery_minutes": 5}]
    p["shared-payments-outage"] = ("Shared payments outage", "Stories",
        "The payments database pool is exhausted for 20 minutes starting in 3 minutes; shop checkouts and travel bookings "
        "fail together, then recover.", outage)
    regression = copy.deepcopy(p["release-regression"][3])
    regression["rollouts"][0]["rollback_at"] = _incident_start(now_ms, 45)
    p["bad-release-and-rollback"] = ("Bad release and rollback", "Stories",
        "10.28.0 crashes at booking confirmation until it is rolled back 45 minutes from now.", regression)
    peak = merge(merge(_backend("payment-timeouts", rate=.2), _backend("inventory-deadlock", rate=.1)),
                 p["slow-critical-flow"][3])
    p["peak-traffic-checkout-degradation"] = ("Peak traffic checkout degradation", "Stories",
        "Under peak load, card processor timeouts and stock reservation deadlocks fail checkouts while authorization slows.",
        peak)
    p["search-degradation"] = ("Search degradation from a slow supplier", "Stories",
        "An airline adapter times out and search returns partial results; complex searches slow down.",
        merge(_backend("supplier-timeouts", rate=.35), _backend("search-complexity", rate=.2)))
    return p


def merge(base, fragment):
    out = copy.deepcopy(base)
    for key, value in fragment.items():
        if isinstance(value, list):
            have = {x.get("id") for x in out.get(key, []) if isinstance(x, dict)}
            out.setdefault(key, []).extend(copy.deepcopy(x) for x in value if not (isinstance(x, dict) and x.get("id") in have))
        elif isinstance(value, dict):
            out.setdefault(key, {}).update(copy.deepcopy(value))
        else:
            out[key] = value
    return out


def apply(spec, name, now_ms=None):
    presets = catalog_presets(now_ms)
    if name not in presets:
        raise KeyError(f"Unknown preset {name}. Options: {', '.join(sorted(presets))}")
    out = merge(spec, presets[name][3])
    out.setdefault("stories", [])
    if name not in out["stories"]:
        out["stories"].append(name)
    return out


def listing(now_ms=None):
    return [{"id": k, "title": v[0], "group": v[1], "description": v[2]} for k, v in catalog_presets(now_ms).items()]
