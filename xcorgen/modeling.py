"""Optional performance profiles resolved before execution, with separate local evidence."""
from collections import Counter
import hashlib
import json

from . import distributions as dist
from .catalog import LOB_FOR

def validate(config, spec):
    errors = []
    if not config: return errors
    if spec.get("causal") or spec.get("verification_cases"):
        errors.append(("$.modeling", "Diagnostic fixtures own exact timing; use performance profiles with population scenarios."))
    bindings = config.get("bindings", [])
    ids, priorities = set(), set()
    for i, b in enumerate(bindings):
        path = f"$.modeling.bindings[{i}]"
        if b["id"] in ids: errors.append((path, "Give each performance override a unique ID."))
        ids.add(b["id"])
        key = (b["quantity"], b.get("priority", 0))
        if key in priorities: errors.append((path, "Overrides of the same quantity need distinct priorities."))
        priorities.add(key)
        for error in dist.validate(b["distribution"], dist.QUANTITIES[b["quantity"]]["max"]): errors.append((path + ".distribution", error))
        c = b.get("cohort", {})
        if c.get("endpoints") and b["quantity"] not in ("network_rtt_ms", "backend_work_ms"):
            errors.append((path + ".cohort", "Endpoint filters apply to API transport and gateway work only."))
        from .profiles import load
        endpoints = {k for p in ("ecommerce", "banking", "travel") for k in load(p).endpoints}
        if any(k not in endpoints for k in c.get("endpoints", [])): errors.append((path + ".cohort", "Unknown endpoint."))
        for exp in c.get("experiments", {}):
            definition = next((e for e in config.get("experiments", []) if e["id"] == exp), None)
            if not definition or any(v not in definition["variants"] for v in c["experiments"][exp]):
                errors.append((path + ".cohort", "Audience refers to an unknown experiment or variant."))
    from .profiles import load
    from .population import NETWORKS, INTENTS
    endpoints = {k for p in ("ecommerce", "banking", "travel") for k in load(p).endpoints}
    screens = {k for p in ("ecommerce", "banking", "travel") for k in load(p).pages}
    for i, b in enumerate(bindings):
        for key, allowed in (("networks", set(NETWORKS)), ("screens", screens), ("intents", {k for mix in INTENTS.values() for k in mix})):
            if set(b.get("cohort", {}).get(key, [])) - allowed:
                errors.append((f"$.modeling.bindings[{i}].cohort.{key}", "Unknown audience value."))
    exp_ids = set()
    for i, e in enumerate(config.get("experiments", [])):
        path = f"$.modeling.experiments[{i}]"
        if e["id"] in exp_ids: errors.append((path, "Experiment IDs must be unique."))
        exp_ids.add(e["id"])
        if not e["exposure_endpoints"] or set(e["exposure_endpoints"]) - endpoints:
            errors.append((path, "Choose known exposure endpoints."))
        if sum(e["variants"].values()) <= 0: errors.append((path, "At least one variant needs a positive weight."))
    from .adoption import validate as validate_releases
    return errors + validate_releases(config.get('releases', []), spec)


def assign_experiments(config, persona, seed, origin=""):
    """Device/origin scoped; namespaced hash does not consume the population RNG."""
    assignments = {}
    for e in config.get("experiments", []):
        r = dist.rng_for("experiment", seed, persona["device_id"], origin, e["id"], e.get("version", "1"), e.get("salt", ""))
        if r.random() >= e.get("enrollment", 1): continue
        from .util import weighted
        assignments[e["id"]] = weighted(sorted(e["variants"].items()), r)
    return assignments


class Model:
    def __init__(self, s, config, seed, index, target):
        self.session, self.config, self.seed, self.index = s, config, seed, index
        self.counters = Counter(); self.rows = []; self.dropped = 0
        self.assignments = assign_experiments(config, s.persona, seed, getattr(s, "origin", ""))
        self.exposed = set()
        p = s.persona
        from .personas import DEVICE_TIERS
        self.context = {"platforms": p.get("platform", "web"), "lines_of_business": LOB_FOR[s.profile.key],
                        "device_tiers": p.get("hardware_tier", DEVICE_TIERS.get(p.get('model'), 'mid')), "networks": p.get("connection"),
                        "intents": (getattr(s.plan, "shape", None) or {}).get("intent"), "app_versions": s.app_version,
                        "returning": bool(p.get("returning_visit")), "target": target}
        for key, variant in self.assignments.items(): s.properties["experiment_" + key] = variant

    def matches(self, cohort, operation):
        s = self.session
        screen = getattr(s, "screen_def_key", None) or (getattr(s, "page", None) or {}).get("key")
        ctx = dict(self.context, endpoints=operation, screens=screen)
        for key, values in cohort.items():
            if key == "experiments":
                if any(self.assignments.get(e) not in variants or e not in self.exposed for e, variants in values.items()): return False
            elif key == "returning":
                if ctx[key] != values: return False
            elif values and ctx.get(key) not in values: return False
        return True

    def expose(self, endpoint):
        for e in self.config.get("experiments", []):
            if e["id"] in self.assignments and endpoint in e["exposure_endpoints"]:
                self.exposed.add(e["id"])
                self.session.properties["exposed_" + e["id"]] = self.assignments[e["id"]]

    def sample(self, quantity, fallback, operation=""):
        # Always evaluate the legacy draw first, preserving its RNG consumption.
        baseline = fallback()
        key = (quantity, operation)
        occurrence = self.counters[key]; self.counters[key] += 1
        candidates = [b for b in self.config.get("bindings", []) if b["quantity"] == quantity and self.matches(b.get("cohort", {}), operation)]
        if not candidates: return baseline
        b = max(candidates, key=lambda x: x.get("priority", 0))
        maximum = dist.QUANTITIES[quantity]["max"]
        r = dist.rng_for("quantity", self.seed, self.index, quantity, operation, occurrence)
        raw, resampled, clipped = dist.draw(b["distribution"], r, maximum)
        selected = dist.rng_for("exposure", self.seed, self.index, b["id"]).random() < b.get("share", 1)
        value, constrained = dist.transform(raw, b["distribution"], b.get("effect") if selected else None, maximum)
        # A binding defines the cohort's baseline distribution; share selects its incident effect.
        row = {"binding": b["id"], "quantity": quantity, "operation": operation, "occurrence": occurrence,
               "selected": selected, "before": raw, "after": value, "resampled": resampled, "bounded": clipped or constrained}
        if len(self.rows) < 512: self.rows.append(row)
        else: self.dropped += 1
        return value

    def evidence(self):
        return {"version": dist.VERSION, "seed": self.seed, "index": self.index, "context": self.context,
                "assignments": self.assignments, "exposed": sorted(self.exposed), "draws": self.rows, "omitted_draws": self.dropped}


def sample(s, quantity, fallback, operation=""):
    model = getattr(s, "performance_model", None)
    return model.sample(quantity, fallback, operation) if model else fallback()


def report(observations, spec):
    """Modeled component deltas and independent emitted request/session durations."""
    config = spec.get("modeling")
    if not config: return None
    rows = []
    for b in config.get("bindings", []):
        visits = [o for o in observations if any(d["binding"] == b["id"] for d in o.get("modeling", {}).get("draws", []))]
        draws = [d for o in visits for d in o["modeling"]["draws"] if d["binding"] == b["id"]]
        selected = [o for o in visits if any(d["binding"] == b["id"] and d["selected"] for d in o["modeling"]["draws"])]
        ids = {o["session_id"] for o in selected}
        evidence = {}
        for name, group in (("affected" if b.get("effect") else "configured", selected), ("control", [o for o in visits if o["session_id"] not in ids])):
            nets = []
            for o in group:
                operations = {d['operation'] for d in o['modeling']['draws'] if d['binding'] == b['id'] and d['operation']}
                nets.extend(n for n in o['net'] if not operations or n['endpoint'] in operations)
            evidence[name] = {"visits": len(group), "request_ms": dist.stats([n["ms"] for n in nets]),
                              "session_ms": dist.stats([o["visit_end_ms"] - o["visit_start_ms"] for o in group if "visit_end_ms" in o]),
                              "request_errors": sum(n["status"] is None or n["status"] >= 400 for n in nets)}
        rows.append({"id": b["id"], "quantity": b["quantity"], "requested": b, "denominator": "visits reaching a matching modeled operation",
                     "eligible_visits": len(visits), "selected_visits": len(selected), "modeled_before": dist.stats([d["before"] for d in draws]),
                     "modeled_after": dist.stats([d["after"] for d in draws]), "resampled": sum(d["resampled"] for d in draws),
                     "bounded": sum(d["bounded"] for d in draws), "emitted": evidence,
                     "examples": [{"session_id": o["session_id"], "trace_ids": sorted({n["trace_id"] for n in o["net"]})[:3]}
                                  for o in sorted(selected, key=lambda o: o['modeling']['index'])[:3]],
                     "note": "No matching operations in this sample." if not visits else "Small sample; tail estimates vary." if len(draws) < 100 else None})
    experiments = []
    for e in config.get("experiments", []):
        groups = []
        for variant in e["variants"]:
            assigned = [o for o in observations if o.get("modeling", {}).get("assignments", {}).get(e["id"]) == variant]
            exposed = [o for o in assigned if e["id"] in o["modeling"].get("exposed", [])]
            groups.append({"variant": variant, "assigned_visits": len(assigned), "exposed_visits": len(exposed),
                           "assigned_devices": len({o["model_device"] for o in assigned})})
        experiments.append({"id": e["id"], "variants": groups})
    releases = []
    for curve in config.get('releases', []):
        visits = [o for o in observations if o.get('modeling', {}).get('context', {}).get('target') == curve['target']]
        counts = Counter(o['app_version'] for o in visits)
        releases.append({'target': curve['target'], 'requested': curve, 'visits': len(visits),
                         'observed': [{'version': v, 'visits': n, 'percent': round(n * 100 / len(visits), 2)} for v, n in sorted(counts.items())]})
    return {"version": dist.VERSION, "seed": spec["seed"] if spec.get("seed") is not None else 7331,
            "configuration_digest": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            "effects": rows, "experiments": experiments, "releases": releases,
            "omitted_draws": sum(o.get('modeling', {}).get('omitted_draws', 0) for o in observations),
            "scope": "local_generation", "storage_verified": False}
