"""ScenarioSpec: defaults, validation, JSON Patch and plain-language explanation.

A spec is the single source of truth for a run (spec/scenario.schema.json).
validate() returns actionable issues; any issue with severity "error"
blocks a run. The same function serves the CLI, the server, the copilot and
MCP clients, so every surface applies identical rules.
"""
import copy
import itertools
import json
import math
from datetime import datetime, timezone

from . import catalog, limits, schema
from .personas import ANDROID_DEVICES, DEVICE_TIERS

SPEC_VERSION = "2.0"
ROOT_FIELDS_DEFAULT_EMPTY = ("client_faults", "web_performance", "backend_faults", "incidents", "rollouts",
                             "verification_cases", "stories")

ANDROID_TARGET = {
    "id": "vantra-android", "platform": "android", "embrace_app_id": "5nrc2", "app_name": "Vantra",
    "environment": "production", "region": "us",
    "api_domains": {"retail": "api.vantrashop.com", "banking": "api.vantramoney.com", "travel": "api.vantratravel.com"},
    "android": {"package": "com.vantra.android", "ui_toolkit": "compose", "sdk_version": "9.3.1", "versions": [
        {"version": "10.28.0", "version_code": "1028004", "sdk_version": "9.3.1", "released": "2026-10-20", "share": .61},
        {"version": "10.27.3", "version_code": "1027032", "sdk_version": "9.3.1", "released": "2026-10-06", "share": .30},
        {"version": "10.25.1", "version_code": "1025011", "sdk_version": "9.3.0", "released": "2026-09-08", "share": .09}]},
}
WEB_TARGET = {
    "id": "vantra-web", "platform": "web", "embrace_app_id": "kfs9f", "app_name": "Vantra Web",
    "environment": "production", "region": "us",
    "api_domains": dict(ANDROID_TARGET["api_domains"]),
    "web": {"sdk_version": "2.31.0", "cdn": "https://static.vantra.com", "router_integration": "route_templates",
            "sdk_upload_spans": True, "origin_header": {"policy": "none"},
            "estate": {"retail": {"origin": "https://www.vantra.com", "archetype": "multi_page"},
                       "banking": {"origin": "https://app.vantra.com", "archetype": "single_page"},
                       "travel": {"origin": "https://www.vantra.com", "archetype": "hybrid"}},
            "propagation_allow_list": ["api.vantrashop.com", "api.vantramoney.com", "api.vantratravel.com"]},
}
DEFAULT_POPULATION = {
    "line_of_business_mix": {"retail": 1, "banking": 1, "travel": 1},
    "signed_in_share": .82, "returning_share": .45,
    "journey_depth": {"retail": .23, "banking": .18, "travel": .23},
    "session_properties": {"market": {"US": 1},
                           "membership_tier": {"standard": 62, "plus": 29, "premium": 9},
                           "entry_point": {"home_tab": 55, "push_notification": 15, "deep_link": 12, "widget": 6,
                                           "search": 12}},
}
CRASH_FAMILY_WEIGHTS = {0: 40, 1: 25, 2: 15, 3: 12, 4: 8}
# Journey order per line of business: startup, endpoints and the mid-journey "content" stage where
# freezes, handled errors and content-stage crashes happen. Used to keep faults out of sessions that a
# fatal crash ends before the fault's stage.
JOURNEY_ORDER = {
    "retail": ["startup", "home", "get_recommendations", "search", "list_products", "get_product", "content",
               "add_to_cart", "login", "get_cart", "shipping_options", "place_order", "operation_status", "get_order"],
    "banking": ["startup", "login", "mfa_verify", "accounts", "transactions", "content", "payees", "create_transfer",
                "operation_status", "pay_bill", "statement", "lock_card"],
    "travel": ["startup", "locations", "hotel_search", "hotel_revalidate", "flight_search", "content", "offer",
               "create_booking", "operation_status", "trips"],
}
# Where each V1 crash family terminates the process (journeys.client_issue stages).
CRASH_STAGE_ENDPOINT = {
    "retail": ("place_order", "place_order", "get_cart", "content", "content"),
    "banking": ("content", "create_transfer", "content", "content", "content"),
    "travel": ("create_booking", "create_booking", "content", "content", "content"),
}
ANDROID_SCREENS = {
    "retail": ["home", "category", "search", "product", "cart", "signin", "checkout", "confirmation"],
    "banking": ["signin", "mfa", "dashboard", "account", "transfer", "transfer_review", "transfer_done", "billpay",
                "statements", "cards"],
    "travel": ["home", "flights", "offer", "checkout", "confirmation", "hotels", "trips"],
}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_spec():
    return {"spec_version": SPEC_VERSION, "name": "Vantra Android", "seed": None,
            "targets": [copy.deepcopy(ANDROID_TARGET)],
            "chronosphere": None,
            "destinations": {"mode": "sessions_only", "backend_logs": False},
            "population": copy.deepcopy(DEFAULT_POPULATION),
            "traffic": {"mode": "live", "rate_per_minute": 10, "duration_minutes": 0,
                        "daily_curve": {"kind": "flat"}},
            "client_faults": [], "backend_faults": [], "incidents": [], "rollouts": []}


def load(path):
    with open(path) as f:
        return json.load(f)


# --- normalization ------------------------------------------------------------------------------------

def normalized(spec):
    """Deep copy with defaults filled; never mutates the input."""
    s = copy.deepcopy(spec)
    for key in ROOT_FIELDS_DEFAULT_EMPTY:
        s.setdefault(key, [])
    pop = s.setdefault("population", {})
    for key, value in DEFAULT_POPULATION.items():
        if isinstance(value, dict):
            merged = copy.deepcopy(value)
            merged.update(pop.get(key, {}))
            pop[key] = merged
        else:
            pop.setdefault(key, value)
    for target in s.get("targets", []):
        base = ANDROID_TARGET if target.get("platform") == "android" else WEB_TARGET if target.get("platform") == "web" else {}
        for key, value in base.items():
            if key in ("id", "embrace_app_id"):
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merged = copy.deepcopy(value)
                merged.update(target[key])
                target[key] = merged
            else:
                target.setdefault(key, copy.deepcopy(value))
    pop.setdefault("platform_mix", {t["id"]: 1 for t in s.get("targets", [])})
    s.setdefault("destinations", {"mode": "sessions_only"})
    for fault in s["client_faults"]:
        meta = catalog.CLIENT_FAULTS.get(fault.get("type"), {})
        fault.setdefault("platform", meta.get("platforms", ("any",))[0] if len(meta.get("platforms", ())) == 1 else "any")
        fault.setdefault("quota", "auto")
        fault.setdefault("filters", {})
        fault.setdefault("details", {})
        if meta.get("fatal"):
            fault.setdefault("exclusive_group", "fatal")
    groups = {f.get("exclusive_group") for f in s["backend_faults"] if f.get("exclusive_group")}
    for fault in s["backend_faults"]:
        fault.setdefault("quota", "auto")
        fault.setdefault("filters", {})
        fault.setdefault("details", {})
        fault.setdefault("rate", 0.0)
        # One backend story per session: every backend fault shares one exclusive group.
        fault.setdefault("exclusive_group", next(iter(groups)) if len(groups) == 1 else "backend")
    return s


def lines_of_business(spec):
    mix = spec["population"]["line_of_business_mix"]
    return [lob for lob in catalog.LINES_OF_BUSINESS if mix.get(lob, 0) > 0]


def fault_lines(fault, spec):
    """Lines of business a fault can affect."""
    lobs = set(lines_of_business(spec))
    if fault.get("filters", {}).get("lines_of_business"):
        lobs &= set(fault["filters"]["lines_of_business"])
    if fault.get("lines_of_business"):
        lobs &= set(fault["lines_of_business"])
    if fault.get("type") not in catalog.CLIENT_FAULTS:
        variant = catalog.resolve_variant(fault)
        if variant:
            lobs &= set(catalog.BACKEND_VARIANTS[variant]["endpoints"])
    return lobs


def incident_for(fault, spec):
    return next((i for i in spec.get("incidents", []) if i.get("fault_ref") == fault.get("id")
                 or i.get("id") == fault.get("schedule")), None)


def incident_window(incident):
    start = parse_time(incident["start"])
    onset = incident.get("onset_minutes", 0) * limits.MINUTE_MS
    peak = incident.get("peak_minutes", 30) * limits.MINUTE_MS
    recovery = incident.get("recovery_minutes", 0) * limits.MINUTE_MS
    repeat = incident.get("repeat_every_minutes")
    return {"start_ms": start, "onset_ms": onset, "peak_ms": peak, "recovery_ms": recovery,
            "repeat_ms": repeat * limits.MINUTE_MS if repeat else None}


def phase_at(t_ms, window):
    """onset, incident (peak), recovery or None at time t for an incident window."""
    if window is None:
        return "incident"
    offset = t_ms - window["start_ms"]
    if window["repeat_ms"]:
        offset %= window["repeat_ms"]
    if offset < 0:
        return None
    if offset < window["onset_ms"]:
        return "onset"
    if offset < window["onset_ms"] + window["peak_ms"]:
        return "incident"
    if offset < window["onset_ms"] + window["peak_ms"] + window["recovery_ms"]:
        return "recovery"
    return None


def ramp(t_ms, window):
    """Share of the peak rate active at time t: linear onset and recovery."""
    phase = phase_at(t_ms, window)
    if phase is None:
        return 0.0
    if phase == "incident" or window is None:
        return 1.0
    offset = (t_ms - window["start_ms"]) % window["repeat_ms"] if window["repeat_ms"] else t_ms - window["start_ms"]
    if phase == "onset":
        return offset / window["onset_ms"] if window["onset_ms"] else 1.0
    into = offset - window["onset_ms"] - window["peak_ms"]
    return max(0.0, 1 - into / window["recovery_ms"]) if window["recovery_ms"] else 0.0


def traffic_window(spec, now_ms):
    """(start_ms, end_ms) the run's sessions are dated in; end None for open-ended live streams."""
    t = spec["traffic"]
    if t["mode"] == "live":
        duration = t.get("duration_minutes", 0)
        return now_ms, now_ms + duration * limits.MINUTE_MS if duration else None
    w = t.get("window") or {"last_minutes": 8 if t["mode"] == "burst" else 24 * 60}
    if "last_minutes" in w:
        return now_ms - w["last_minutes"] * limits.MINUTE_MS, now_ms
    return parse_time(w["start"]), parse_time(w["end"])


# --- validation ---------------------------------------------------------------------------------------

def _issue(severity, path, message, code):
    return {"severity": severity, "path": path, "message": message, "code": code}


SESSION_DIMS = ("lines_of_business", "app_versions", "os_versions", "device_models", "device_tiers", "signed_in",
                "release_rings", "browser_families", "device_types")


def _accepts(filters, dim, value):
    allowed = filters.get(dim)
    if allowed is None or allowed == []:
        return True
    if dim == "signed_in":
        return value == allowed
    return value in allowed


def _max_group_sum(faults, spec, rate_of):
    """Largest summed rate any single session can see across faults of one exclusive group."""
    values = {}
    for dim in SESSION_DIMS:
        mentioned = set()
        for f in faults:
            v = f.get("filters", {}).get(dim)
            if dim == "signed_in" and v is not None:
                mentioned.add(v)
            elif isinstance(v, list):
                mentioned.update(v)
        if dim == "lines_of_business":
            mentioned |= set(lines_of_business(spec))
        if dim == "signed_in":
            values[dim] = sorted(mentioned | {True, False}, key=str)
        else:
            values[dim] = sorted(mentioned, key=str) + ["<other>"]
    best, best_stratum = 0.0, None
    dims = [d for d in SESSION_DIMS if len(values[d]) > 1]
    for combo in itertools.product(*(values[d] for d in dims)):
        stratum = dict(zip(dims, combo))
        lob = stratum.get("lines_of_business")
        total = 0.0
        for f in faults:
            if lob and lob not in fault_lines(f, spec):
                continue
            if all(_accepts(f.get("filters", {}), d, stratum[d]) for d in dims if d != "lines_of_business"):
                total += rate_of(f)
        if total > best:
            best, best_stratum = total, stratum
    return best, best_stratum


def validate(spec, now_ms=None, lint=True):
    """Schema, semantic, platform-limit and realism checks. Returns a list of issues."""
    import time
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    issues = [_issue("error", path, message, "schema") for path, message in schema.validate(spec)]
    if issues:
        return issues
    s = normalized(spec)
    targets = {t["id"]: t for t in s["targets"]}
    platforms = {t["platform"] for t in s["targets"]}
    if len(targets) != len(s["targets"]):
        issues.append(_issue("error", "$.targets", "Target IDs must be unique.", "duplicate_id"))
    for i, t in enumerate(s["targets"]):
        p = f"$.targets[{i}]"
        if t["platform"] == "ios":
            issues.append(_issue("error", p, "iOS sessions are reserved for a later release.", "unsupported_platform"))
        if t["platform"] == "android":
            shares = sum(v["share"] for v in t["android"]["versions"])
            if abs(shares - 1) > .01:
                issues.append(_issue("error", f"{p}.android.versions", f"Version adoption shares sum to {shares:.2f}; they must sum to 1.", "shares"))
        if t["platform"] == "web":
            o = t["web"].get("origin_header", {})
            if o.get("policy") == "allowed" and not o.get("value"):
                issues.append(_issue("error", f"{p}.web.origin_header", "An allowed Origin policy needs the allowed origin.", "origin"))
        for lob in lines_of_business(s):
            if not t.get("api_domains", {}).get(lob):
                issues.append(_issue("error", f"{p}.api_domains", f"No API domain for {lob}.", "api_domain"))
    unknown = set(s["population"]["platform_mix"]) - set(targets)
    if unknown:
        issues.append(_issue("error", "$.population.platform_mix", f"Unknown target IDs: {', '.join(sorted(unknown))}.", "unknown_target"))
    if not lines_of_business(s):
        issues.append(_issue("error", "$.population.line_of_business_mix", "At least one line of business needs a positive share.", "mix"))

    # Destinations.
    mode = s["destinations"]["mode"]
    if mode == "end_to_end" and not (s.get("chronosphere") or {}).get("tenant"):
        issues.append(_issue("error", "$.chronosphere", "End-to-end runs need a Cortex XCOR tenant and token reference.", "destination"))
    if mode == "sessions_only" and (s["destinations"].get("backend_logs") or s["destinations"].get("emit_client_spans")):
        issues.append(_issue("warning", "$.destinations", "Sessions-only runs send nothing to Cortex XCOR; backend logs and client spans are ignored.", "destination"))
    if s["destinations"].get("backend_metrics"):
        issues.append(_issue("error", "$.destinations.backend_metrics", "Backend RED metrics are planned for a later phase.", "unsupported"))

    # Traffic and platform limits.
    t = s["traffic"]
    start_ms, end_ms = traffic_window(s, now_ms)
    if t["mode"] == "live":
        if not t.get("rate_per_minute"):
            issues.append(_issue("error", "$.traffic.rate_per_minute", "Live streams need a rate.", "traffic"))
        elif t["rate_per_minute"] > limits.MAX_RATE_PER_STREAM:
            issues.append(_issue("error", "$.traffic.rate_per_minute", f"At most {limits.MAX_RATE_PER_STREAM:g} sessions per minute per stream.", "guardrail"))
    else:
        if not t.get("count"):
            issues.append(_issue("error", "$.traffic.count", f"{t['mode'].title()} jobs need a session count.", "traffic"))
        elif t["count"] > limits.MAX_SESSIONS_PER_JOB:
            issues.append(_issue("error", "$.traffic.count", f"At most {limits.MAX_SESSIONS_PER_JOB} sessions per job.", "guardrail"))
        if end_ms <= start_ms:
            issues.append(_issue("error", "$.traffic.window", "The window must end after it starts.", "window"))
        if end_ms > now_ms + limits.MINUTE_MS:
            issues.append(_issue("error", "$.traffic.window", "Burst and backfill windows cannot extend into the future.", "limit"))
        if now_ms - start_ms > limits.BACKFILL_MAX_MS + limits.MINUTE_MS:
            issues.append(_issue("error", "$.traffic.window", "Cortex XCOR RUM history can be backfilled for at most 24 hours.", "limit"))
        if mode == "end_to_end" and now_ms - start_ms > limits.TRACE_ELIGIBLE_MS:
            issues.append(_issue("warning", "$.traffic.window", limits.TRACE_CAVEAT, "trace_window"))
    curve = t.get("daily_curve", {})
    if curve.get("kind") == "custom" and len(curve.get("hourly_weights", [])) != 24:
        issues.append(_issue("error", "$.traffic.daily_curve", "A custom daily curve needs 24 hourly weights.", "traffic"))
    if curve.get("kind") == "custom" and not any(curve.get("hourly_weights", [])):
        issues.append(_issue("error", "$.traffic.daily_curve", "At least one hourly weight must be positive.", "traffic"))

    # Faults.
    all_ids = [f["id"] for f in s["client_faults"] + s["backend_faults"]]
    for dup in sorted({x for x in all_ids if all_ids.count(x) > 1}):
        issues.append(_issue("error", "$", f"Fault ID {dup} is used more than once.", "duplicate_id"))
    android = next((t for t in s["targets"] if t["platform"] == "android"), None)
    versions = {v["version"] for v in android["android"]["versions"]} if android else set()
    devices = {d[1]: set(d[2]) for d in ANDROID_DEVICES}
    rings_available = bool(s["rollouts"])
    for kind, faults in (("client_faults", s["client_faults"]), ("backend_faults", s["backend_faults"])):
        for i, f in enumerate(faults):
            p = f"$.{kind}[{i}]"
            flt = f.get("filters", {})
            lobs = fault_lines(f, s)
            if not lobs:
                issues.append(_issue("error", p, "The fault's lines of business match none of the run's lines of business.", "filter_empty"))
            if kind == "client_faults":
                meta = catalog.CLIENT_FAULTS[f["type"]]
                plats = set(meta["platforms"]) if f["platform"] == "any" else {f["platform"]}
                if not plats & platforms:
                    issues.append(_issue("error", p, f"No {' or '.join(sorted(plats))} target for {f['type']}.", "no_target"))
                elif not plats & platforms & set(meta["supported"]):
                    issues.append(_issue("error", p, f"{f['type']} is not produced by the engine yet for {', '.join(sorted(plats & platforms))}.", "unsupported"))
                issues += _client_details(f, p, lobs)
            else:
                if f["type"] in catalog.PLANNED_BACKEND_TYPES:
                    issues.append(_issue("error", p, f"{catalog.PLANNED_BACKEND_TYPES[f['type']]} ({f['type']}) has no builder yet.", "unsupported"))
                    continue
                variant = catalog.resolve_variant(f)
                if not variant:
                    options = catalog.variants_for(f["type"], f.get("lines_of_business"))
                    issues.append(_issue("error", f"{p}.variant", f"Choose a variant for {f['type']}: {', '.join(options) or 'none for these lines of business'}.", "variant"))
                    continue
                v = catalog.BACKEND_VARIANTS[variant]
                if v["type"] != f["type"]:
                    issues.append(_issue("error", f"{p}.variant", f"Variant {variant} is a {v['type']} fault, not {f['type']}.", "variant"))
                if f.get("service") and f["service"] != v["service"]:
                    issues.append(_issue("error", f"{p}.service", f"Variant {variant} affects {v['service']}.", "variant"))
                bad = set(f.get("endpoints") or []) - set(v["endpoints"].values())
                if bad:
                    issues.append(_issue("error", f"{p}.endpoints", f"Variant {variant} lands on {', '.join(sorted(set(v['endpoints'].values())))}, not {', '.join(sorted(bad))}.", "variant"))
                if set(f.get("platforms") or ["android"]) - platforms:
                    issues.append(_issue("error", f"{p}.platforms", "A listed platform has no target.", "no_target"))
                d = f.get("details", {})
                if "fallback" in d and not isinstance(d["fallback"], bool):
                    issues.append(_issue("error", f"{p}.details.fallback", "Fallback must be true or false.", "details"))
                for field, lo, hi in (("lag_seconds", 10, 240), ("lag_messages", 1, 10000000), ("retry_after_seconds", 1, 60)):
                    if field in d and (not isinstance(d[field], (int, float)) or isinstance(d[field], bool) or not lo <= d[field] <= hi):
                        issues.append(_issue("error", f"{p}.details.{field}", f"{field} must be between {lo} and {hi}.", "details"))
                if "version" in d and (not isinstance(d["version"], str) or len(d["version"]) > 64):
                    issues.append(_issue("error", f"{p}.details.version", "Version must be a string of at most 64 characters.", "details"))
            for dim, allowed in (("app_versions", versions), ("device_models", set(devices))):
                bad = set(flt.get(dim) or []) - allowed
                if bad:
                    issues.append(_issue("error", f"{p}.filters.{dim}", f"Matches nothing: {', '.join(sorted(bad))}.", "filter_empty"))
            if flt.get("os_versions"):
                models = set(flt.get("device_models") or devices)
                tiers = set(flt.get("device_tiers") or ("budget", "mid", "flagship"))
                ok = any(DEVICE_TIERS[m] in tiers and devices[m] & set(flt["os_versions"]) for m in models)
                if not ok:
                    issues.append(_issue("error", f"{p}.filters", "No device in the catalog combines these models, tiers and OS versions.", "filter_empty"))
            if flt.get("release_rings") and not rings_available:
                issues.append(_issue("error", f"{p}.filters.release_rings", "Release rings need a rollout.", "filter_empty"))
            for dim in ("web_releases", "browser_families", "browser_versions", "device_types", "connections",
                        "page_groups", "routes"):
                if flt.get(dim) and "web" not in platforms:
                    issues.append(_issue("error", f"{p}.filters.{dim}", "Web filters need a web target.", "filter_empty"))
            for lob in lobs:
                bad = set(flt.get("endpoints") or []) - set(JOURNEY_ORDER[lob]) - {e for o in JOURNEY_ORDER.values() for e in o}
                if bad:
                    issues.append(_issue("error", f"{p}.filters.endpoints", f"Unknown endpoints: {', '.join(sorted(bad))}.", "filter_empty"))
                bad = set(flt.get("screens") or []) - {x for v in ANDROID_SCREENS.values() for x in v}
                if bad:
                    issues.append(_issue("error", f"{p}.filters.screens", f"Unknown screens: {', '.join(sorted(bad))}.", "filter_empty"))
            couple = f.get("details", {}).get("with_fault")
            if couple and couple not in all_ids:
                issues.append(_issue("error", f"{p}.details.with_fault", f"No fault with ID {couple}.", "reference"))

    # Exclusive groups and fatal outcomes.
    every = s["client_faults"] + s["backend_faults"]
    fatal_groups = {f.get("exclusive_group") for f in s["client_faults"] if catalog.CLIENT_FAULTS[f["type"]].get("fatal")}
    if len(fatal_groups) > 1:
        issues.append(_issue("error", "$.client_faults", "Fatal faults must share one exclusive group: a session ends at its first fatal outcome.", "fatal_group"))
    backend_groups = {f["exclusive_group"] for f in s["backend_faults"]}
    if len(backend_groups) > 1:
        issues.append(_issue("error", "$.backend_faults", "Backend faults must share one exclusive group: each session carries one backend story.", "backend_group"))
    for group in sorted({f.get("exclusive_group") for f in every if f.get("exclusive_group")}):
        members = [f for f in every if f.get("exclusive_group") == group and not f.get("details", {}).get("with_fault")]

        def peak(f):
            inc = incident_for(f, s)
            return inc.get("peak_rate", f.get("rate", 0)) if inc else f.get("rate", 0)
        total, stratum = _max_group_sum(members, s, peak)
        if total > 1 + 1e-9:
            where = ", ".join(f"{k}={v}" for k, v in (stratum or {}).items() if v != "<other>")
            issues.append(_issue("error", "$", f"Faults in exclusive group {group} sum to {total:.0%} for sessions with {where or 'any attributes'}; "
                                               "mutually exclusive outcomes cannot exceed 100%.", "exclusive_sum"))
    # A fault coupled to a fatal one (details.with_fault) must land before the session ends.
    by_id = {f["id"]: f for f in every}
    for g in every:
        f = by_id.get(g.get("details", {}).get("with_fault"))
        if not f or f.get("type") not in catalog.CLIENT_FAULTS or not catalog.CLIENT_FAULTS[f["type"]].get("fatal"):
            continue
        for lob in fault_lines(f, s) & fault_lines(g, s):
            crash_at, order = _crash_endpoint(f, lob), JOURNEY_ORDER[lob]
            for endpoint in _endpoints_of(g, lob):
                if crash_at in order and endpoint in order and order.index(endpoint) > order.index(crash_at):
                    issues.append(_issue("error", "$", f"{g['id']} lands on {endpoint}, after {f['id']} has already ended "
                                                       f"{lob} sessions at {crash_at}; a session ends at its fatal outcome.", "after_fatal"))

    # Incidents.
    fault_ids = set(all_ids)
    for i, inc in enumerate(s["incidents"]):
        p = f"$.incidents[{i}]"
        if inc["fault_ref"] not in fault_ids:
            issues.append(_issue("error", f"{p}.fault_ref", f"No fault with ID {inc['fault_ref']}.", "reference"))
        w = incident_window(inc)
        end = w["start_ms"] + w["onset_ms"] + w["peak_ms"] + w["recovery_ms"]
        if w["repeat_ms"] and w["onset_ms"] + w["peak_ms"] + w["recovery_ms"] > w["repeat_ms"]:
            issues.append(_issue("error", p, "A repeating incident must fit inside its repeat interval.", "schedule"))
        if not w["repeat_ms"] and (end < start_ms or (end_ms is not None and w["start_ms"] > end_ms)):
            issues.append(_issue("error", p, f"The incident ({iso(w['start_ms'])} to {iso(end)}) falls outside the traffic window.", "schedule"))
        if mode == "end_to_end" and not w["repeat_ms"] and w["start_ms"] < now_ms - limits.TRACE_ELIGIBLE_MS:
            issues.append(_issue("warning", p, "Sessions in this incident that are older than 8 minutes at send time carry no trace links. " + limits.TRACE_CAVEAT, "trace_window"))
    for f in s["backend_faults"] + s["client_faults"]:
        if f.get("schedule") and f["schedule"] not in {x["id"] for x in s["incidents"]}:
            issues.append(_issue("error", "$", f"{f['id']} is scheduled by unknown incident {f['schedule']}.", "reference"))
    for i, r in enumerate(s["rollouts"]):
        if r.get("rollback_at") and r.get("start") and parse_time(r["rollback_at"]) <= parse_time(r["start"]):
            issues.append(_issue("error", f"$.rollouts[{i}]", "Rollback must come after the rollout starts.", "schedule"))
        if r["kind"] == "app_version" and r["subject"] not in versions:
            issues.append(_issue("error", f"$.rollouts[{i}].subject", f"Unknown app version {r['subject']}.", "filter_empty"))
    if len([r for r in s["rollouts"] if r["kind"] == "feature_cohort"]) > 1:
        issues.append(_issue("error", "$.rollouts", "One feature cohort rollout per run.", "rollout"))
    for i, case in enumerate(s["verification_cases"]):
        if case.get("backend_fault") and case["backend_fault"] not in catalog.BACKEND_VARIANTS:
            issues.append(_issue("error", f"$.verification_cases[{i}].backend_fault", "Unknown backend fault variant.", "variant"))
        elif case.get("backend_fault") and case["line_of_business"] not in catalog.BACKEND_VARIANTS[case["backend_fault"]]["endpoints"]:
            issues.append(_issue("error", f"$.verification_cases[{i}]", "That variant does not affect this line of business.", "variant"))
    from .web_validation import validate as validate_web
    issues += validate_web(s, _issue, now_ms)
    if lint:
        from .linter import lint_spec
        issues += lint_spec(s)
    return issues


def _crash_endpoint(fault, lob):
    d = fault.get("details", {})
    if d.get("endpoint"):
        return d["endpoint"]
    family = d.get("family", 0)
    return CRASH_STAGE_ENDPOINT[lob][family if isinstance(family, int) else 0]


def _endpoints_of(fault, lob):
    if fault.get("type") in catalog.CLIENT_FAULTS:
        e = fault.get("details", {}).get("endpoint")
        return [e] if e else []
    variant = catalog.resolve_variant(fault)
    return [catalog.BACKEND_VARIANTS[variant]["endpoints"][lob]] if variant and lob in catalog.BACKEND_VARIANTS[variant]["endpoints"] else []


def _client_details(f, p, lobs):
    issues, d = [], f.get("details", {})
    t = f["type"]
    if t == "jvm_crash" and "family" in d and d["family"] not in range(5):
        issues.append(_issue("error", f"{p}.details.family", "Crash family must be 0 through 4.", "details"))
    if t == "main_thread_blockage":
        lo, hi = d.get("min_ms", 6000), d.get("max_ms", 12000)
        if not (1000 <= lo <= hi <= 60000):
            issues.append(_issue("error", f"{p}.details", "Blockage duration must be between 1 and 60 seconds, min at most max.", "details"))
    if t == "network_failure":
        bad = set(d.get("kinds", [])) - set(catalog.NETWORK_FAILURES)
        if bad:
            issues.append(_issue("error", f"{p}.details.kinds", f"Unknown failure kinds: {', '.join(sorted(bad))}.", "details"))
    if t == "slow_startup":
        if d.get("phase", "application_init") not in catalog.STARTUP_PHASES:
            issues.append(_issue("error", f"{p}.details.phase", f"Phase must be one of {', '.join(catalog.STARTUP_PHASES)}.", "details"))
        if d.get("min_ms", 4200) < 4000:
            issues.append(_issue("warning", f"{p}.details.min_ms", "Slow cold starts are usually defined as over 4 s.", "details"))
    if t == "slow_root_span":
        span = d.get("span")
        known = {x for lob in lobs for x in catalog.ROOT_SPANS[lob]}
        if span not in known:
            issues.append(_issue("error", f"{p}.details.span", f"Root span must be one of {', '.join(sorted(known))}.", "details"))
        p50, p95 = d.get("p50_ms"), d.get("p95_ms")
        if not (isinstance(p50, (int, float)) and isinstance(p95, (int, float)) and 0 < p50 <= p95 <= 120000):
            issues.append(_issue("error", f"{p}.details", "Set p50_ms and p95_ms with 0 < p50 <= p95 <= 120000.", "details"))
    if t == "http_error":
        status = d.get("status")
        if not (isinstance(status, int) and 400 <= status <= 599):
            issues.append(_issue("error", f"{p}.details.status", "HTTP errors need a 4xx or 5xx status.", "details"))
        if not d.get("endpoint") or not any(d["endpoint"] in JOURNEY_ORDER[lob] for lob in lobs):
            issues.append(_issue("error", f"{p}.details.endpoint", "Name the endpoint the error lands on.", "details"))
    if d.get("endpoint") and t in ("jvm_crash", "network_failure", "handled_exception"):
        if not any(d["endpoint"] in JOURNEY_ORDER[lob] for lob in lobs):
            issues.append(_issue("error", f"{p}.details.endpoint", f"{d['endpoint']} is not an endpoint of {', '.join(sorted(lobs))}.", "details"))
    return issues


def errors(issues):
    return [i for i in issues if i["severity"] == "error"]


# --- JSON Patch (RFC 6902) ----------------------------------------------------------------------------

def _pointer(path):
    if path == "":
        return []
    if not path.startswith("/"):
        raise ValueError(f"Invalid JSON Pointer: {path}")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _walk(doc, parts):
    node = doc
    for part in parts:
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def _add(doc, parts, value):
    if not parts:
        return value
    parent = _walk(doc, parts[:-1])
    key = parts[-1]
    if isinstance(parent, list):
        index = len(parent) if key == "-" else int(key)
        if not 0 <= index <= len(parent):
            raise ValueError("Array index out of range")
        parent.insert(index, value)
    else:
        parent[key] = value
    return doc


def _remove(doc, parts):
    parent = _walk(doc, parts[:-1])
    key = parts[-1]
    if isinstance(parent, list):
        return parent.pop(int(key))
    return parent.pop(key)


def apply_patch(doc, ops):
    """Apply a JSON Patch to a copy of doc; raises ValueError if any operation fails."""
    out = copy.deepcopy(doc)
    try:
        for op in ops:
            kind, parts = op["op"], _pointer(op["path"])
            if kind == "add":
                out = _add(out, parts, copy.deepcopy(op["value"]))
            elif kind == "remove":
                _remove(out, parts)
            elif kind == "replace":
                _walk(out, parts)  # must exist
                if parts:
                    _remove(out, parts)
                out = _add(out, parts, copy.deepcopy(op["value"]))
            elif kind == "move":
                value = _remove(out, _pointer(op["from"]))
                out = _add(out, parts, value)
            elif kind == "copy":
                out = _add(out, parts, copy.deepcopy(_walk(out, _pointer(op["from"]))))
            elif kind == "test":
                if _walk(out, parts) != op["value"]:
                    raise ValueError(f"Test failed at {op['path']}")
            else:
                raise ValueError(f"Unknown patch operation: {kind}")
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(f"Patch path not found: {error}") from None
    return out


# --- explanation --------------------------------------------------------------------------------------

def _pct(x):
    return f"{x * 100:.1f}%".replace(".0%", "%")


def _filters_text(flt):
    parts = []
    labels = {"lines_of_business": "line of business", "app_versions": "app version", "os_versions": "Android",
              "device_models": "device", "device_tiers": "device tier", "release_rings": "release ring",
              "endpoints": "endpoint", "screens": "screen"}
    for key, label in labels.items():
        if flt.get(key):
            parts.append(f"{label} {' or '.join(map(str, flt[key]))}")
    if flt.get("signed_in") is not None:
        parts.append("signed in" if flt["signed_in"] else "signed out")
    return " and ".join(parts)


def explain(spec):
    """Plain-language summary of what a spec generates."""
    s = normalized(spec)
    lines = []
    t, mode = s["traffic"], s["destinations"]["mode"]
    dest = "Cortex XCOR RUM sessions only" if mode == "sessions_only" else "sessions and distributed traces (end to end)"
    if t["mode"] == "live":
        curve = t.get("daily_curve", {}).get("kind", "flat")
        dur = f" for {t['duration_minutes']:g} minutes" if t.get("duration_minutes") else " until stopped"
        lines.append(f"Live stream of {t.get('rate_per_minute', 0):g} sessions per minute ({curve} daily curve){dur}; destination: {dest}.")
    else:
        w = t.get("window") or {}
        span = f"the last {w['last_minutes']:g} minutes" if "last_minutes" in w else f"{w.get('start')} to {w.get('end')}"
        lines.append(f"{t['mode'].title()} of {t.get('count', 0)} sessions dated over {span}; destination: {dest}.")
    mix = s["population"]["line_of_business_mix"]
    total = sum(mix.values()) or 1
    lines.append("Lines of business: " + ", ".join(f"{lob} {_pct(mix[lob] / total)}" for lob in lines_of_business(s)) + ".")
    for target in s["targets"]:
        lines.append(f"Target {target['id']}: {target['platform']} app {target['embrace_app_id']} ({target.get('app_name', '')}).")
    for f in s["client_faults"]:
        where = _filters_text(f["filters"])
        lines.append(f"Client fault {f['id']}: {f['type']} in {_pct(f['rate'])} of sessions" + (f" where {where}" if where else "") +
                     (f", within sessions affected by {f['details']['with_fault']}" if f["details"].get("with_fault") else "") + ".")
    for f in s["backend_faults"]:
        variant = catalog.resolve_variant(f)
        summary = catalog.BACKEND_VARIANTS[variant]["summary"] if variant else f["type"]
        inc = incident_for(f, s)
        when = ""
        if inc:
            w = incident_window(inc)
            when = (f", scheduled from {inc['start']} (onset {inc.get('onset_minutes', 0):g} min, peak {inc.get('peak_minutes', 30):g} min, "
                    f"recovery {inc.get('recovery_minutes', 0):g} min" + (f", repeating every {inc['repeat_every_minutes']:g} min" if w["repeat_ms"] else "") + ")")
        lines.append(f"Backend fault {f['id']}: {summary} Affects {_pct(inc.get('peak_rate', f['rate']) if inc else f['rate'])} of "
                     f"{', '.join(sorted(fault_lines(f, s)))} sessions{when}.")
    for r in s["rollouts"]:
        lines.append(f"Rollout {r['id']}: {r['kind']} {r['subject']} to {_pct(r.get('canary_share', 0))} of users"
                     + (f", rolled back at {r['rollback_at']}" if r.get("rollback_at") else "") + ".")
    if mode == "end_to_end":
        lines.append(limits.TRACE_CAVEAT)
    return lines
