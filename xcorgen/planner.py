"""Turn a ScenarioSpec into per-session plans the V1 journeys and builders execute.

For every session the planner fixes, before anything is generated:
  skeleton: target, line of business, visitor, app version, release ring, start time, trace eligibility
            and the journey shape (priority flow, branch, short visit);
  faults:   which spec faults the session carries (exact quotas or probabilities, exclusive groups);
  assignment: the V1 Plan fields (story, incident, client fault, crash family, transport failure, ...).
The journeys stay unchanged in V1 mode; in spec mode they read the shape instead of drawing it.
"""
import random

from . import catalog, limits, quota
from .android_session import pick_version
from .journeys import ROOT_OPERATIONS
from .personas import DEVICE_TIERS
from .scenarios import assign_ring
from .spec import (CRASH_FAMILY_WEIGHTS, CRASH_STAGE_ENDPOINT, JOURNEY_ORDER, incident_for, incident_window,
                   lines_of_business, normalized, parse_time, ramp)
from .util import weighted

PRIORITY_ENDPOINTS = {"retail": {"get_cart", "shipping_options", "place_order", "get_order", "operation_status", "login"},
                      "banking": {"payees", "create_transfer", "operation_status"},
                      "travel": {"offer", "create_booking", "operation_status"}}
SHORT_DRAW = {"retail": .35, "banking": .42, "travel": .42}
ROOT_ENDPOINT = {}
for _key, _name in ROOT_OPERATIONS.items():
    ROOT_ENDPOINT.setdefault(_name, _key)

WEB_PAGE_ENDPOINTS = {
    "retail": {"home": "home", "search": "search", "product": "get_product", "cart": "get_cart",
               "signin": "login", "checkout": "place_order", "confirmation": "get_order", "category": "list_products"},
    "banking": {"signin": "login", "mfa": "mfa_verify", "dashboard": "accounts", "account": "transactions",
                "transfer": "payees", "transfer_review": "create_transfer", "transfer_done": "create_transfer",
                "statements": "statement", "cards": "lock_card", "billpay": "pay_bill"},
    "travel": {"home": "locations", "flights": "flight_search", "hotels": "hotel_search", "offer": "offer",
               "checkout": "create_booking", "confirmation": "create_booking", "trips": "trips"},
}


def web_pages(fault, lob):
    from .web_sites import SITES
    flt, detail = fault.filters, fault.details
    return [p for p in SITES[lob].values() if (not flt.get("page_groups") or p.group in flt["page_groups"])
            and (not flt.get("routes") or p.route in flt["routes"])
            and (not detail.get("page") or detail["page"] in (p.key, p.group, p.route))]


def natural_shape(lob, depth, rng):
    """The journey a session follows when no fault steers it (V1 branch probabilities)."""
    priority = rng.random() < depth
    shape = {"priority": priority, "short": not priority and rng.random() < SHORT_DRAW[lob],
             "add": False, "statement": False, "hotel": False, "offer": False}
    if lob == "retail":
        shape["add"] = rng.random() < .3
    elif lob == "banking":
        shape["statement"] = rng.random() < .25
    else:
        shape["hotel"] = rng.random() < .3
        shape["offer"] = rng.random() < .5
    return shape


def reach(lob, shape, signed_in=True):
    """Endpoints (and the 'content' stage) a session with this shape requests."""
    if shape.get("bounce"):
        return {"home"}
    if shape.get("overview_only"):
        return {"accounts", "content"} | (set() if signed_in else {"login", "mfa_verify"})
    if shape["short"]:
        return {"retail": {"home", "get_recommendations"}, "banking": {"lock_card"}, "travel": {"trips"}}[lob]
    if lob == "retail":
        out = {"home", "search", "get_product", "get_recommendations", "content"}
        if shape.get("intent"):
            out.discard("search")
            if shape.get("search"):
                out.add("search")
            if shape.get("category"):
                out.add("list_products")
        if shape["priority"] or shape["add"]:
            out.add("add_to_cart")
            if shape.get("intent"):
                out.add("get_cart")
        if shape["priority"]:
            out |= {"get_cart", "shipping_options", "place_order", "operation_status", "get_order"}
        return out
    if lob == "banking":
        out = {"accounts", "transactions", "content"} | (set() if signed_in else {"login", "mfa_verify"})
        if shape["priority"]:
            out |= {"payees", "create_transfer", "operation_status"}
        elif shape.get("abandon"):
            out.add("payees")
        elif shape["statement"]:
            out.add("statement")
        return out
    out = {"locations", "content"}
    if shape["hotel"] and not shape["priority"]:
        return out | {"hotel_search", "hotel_revalidate"}
    out.add("flight_search")
    if shape["priority"] or shape["offer"] or shape.get("abandon"):
        out.add("offer")
    if shape["priority"]:
        out |= {"create_booking", "operation_status", "trips"}
    return out


def steer(lob, shape, endpoint):
    """Change the shape so the session reaches endpoint; False when no journey can."""
    shape["short"] = False
    shape["bounce"] = shape["overview_only"] = False
    if endpoint in (None, "content", "startup"):
        return True
    if endpoint in PRIORITY_ENDPOINTS[lob]:
        shape["priority"] = True
        shape["abandon"] = False
    elif lob == "retail" and endpoint == "add_to_cart":
        shape["add"] = True
    elif lob == "retail" and endpoint in ("search", "list_products"):
        shape["search" if endpoint == "search" else "category"] = True
    elif lob == "banking" and endpoint == "statement":
        shape.update(priority=False, statement=True)
    elif lob == "banking" and endpoint == "lock_card":
        shape["short"] = True
    elif lob == "travel" and endpoint in ("hotel_search", "hotel_revalidate"):
        shape.update(priority=False, hotel=True)
    elif lob == "travel" and endpoint == "flight_search":
        shape["hotel"] = False
    elif lob == "travel" and endpoint == "trips":
        shape["priority"] = True
    return endpoint in reach(lob, shape)


class Fault:
    """A normalized spec fault with everything the planner needs."""

    def __init__(self, raw, kind, spec):
        self.raw, self.kind = raw, kind
        self.spec = spec
        self.id, self.type = raw["id"], raw["type"]
        self.filters = raw.get("filters", {})
        self.details = raw.get("details", {})
        self.group = raw.get("exclusive_group")
        self.quota = raw.get("quota", "auto")
        self.rate = raw.get("rate", 0)
        self.with_fault = self.details.get("with_fault")
        incident = incident_for(raw, spec)
        self.window = incident_window(incident) if incident else None
        if incident and "peak_rate" in incident:
            self.rate = incident["peak_rate"]
        self.variant = catalog.resolve_variant(raw) if kind == "backend" else None
        self.meta = catalog.BACKEND_VARIANTS[self.variant] if self.variant else catalog.CLIENT_FAULTS.get(self.type, {})
        self.fatal = bool(self.meta.get("fatal"))
        lobs = set(lines_of_business(spec))
        if self.filters.get("lines_of_business"):
            lobs &= set(self.filters["lines_of_business"])
        if raw.get("lines_of_business"):
            lobs &= set(raw["lines_of_business"])
        if self.variant:
            lobs &= set(self.meta["endpoints"])
        self.lobs = lobs
        self.steer = raw.get("steer_journey", True)
        self.rollbacks = []

    def family(self, skel, rng):
        """Crash family for this session, drawn once and reused by eligibility and assignment."""
        d = self.details
        if isinstance(d.get("family"), int):
            return d["family"]
        key = "crash_family:" + self.id
        if skel is not None and key not in skel:
            weights = d.get("family_weights") or CRASH_FAMILY_WEIGHTS
            skel[key] = weighted([(int(k), w) for k, w in weights.items()], rng)
        return skel[key] if skel is not None else None

    def endpoint(self, lob, skel=None, rng=random):
        """Where in the journey the fault lands for a line of business."""
        if self.variant:
            return self.meta["endpoints"][lob]
        d = self.details
        if self.raw.get("platform") == "web" and (self.filters.get("page_groups") or self.filters.get("routes") or d.get("page")):
            pages = web_pages(self, lob)
            if pages:
                return WEB_PAGE_ENDPOINTS[lob].get(pages[0].key)
        if self.type in ("jvm_crash","ios_crash"):
            if d.get("endpoint"):
                return d["endpoint"]
            family = self.family(skel, rng)
            return CRASH_STAGE_ENDPOINT[lob][family] if family is not None else None
        if self.type in ("main_thread_blockage", "handled_exception"):
            return d.get("endpoint") or "content"
        if self.type in ("network_failure", "http_error"):
            return d.get("endpoint") or ("content" if self.type == "network_failure" else None)
        if self.type == "slow_root_span":
            return ROOT_ENDPOINT.get(d.get("span"))
        if self.type == "slow_startup":
            return "startup"
        return None

    def requested_at(self, t_ms):
        """Share of eligible sessions that should show the fault (the pool incident fails only at its peak)."""
        return self.rate_at(t_ms)

    def rate_at(self, t_ms):
        if any(t_ms >= rb for rb in self.rollbacks):
            return 0.0
        if self.window is None:
            return self.rate
        if self.variant == "payments-db-pool":
            # Exact quotas cover the failing peak; onset sessions are drawn separately (onset_rate_at).
            from .spec import phase_at
            return self.rate if phase_at(t_ms, self.window) == "incident" else 0.0
        return self.rate * ramp(t_ms, self.window)

    def onset_rate_at(self, t_ms):
        """Onset sessions get the pool story (slow payment-token lock queries, no failure), as in V1."""
        if self.variant != "payments-db-pool" or self.window is None or any(t_ms >= rb for rb in self.rollbacks):
            return 0.0
        from .spec import phase_at
        return self.rate if phase_at(t_ms, self.window) == "onset" else 0.0


class Planner:
    def __init__(self, spec, rng=random):
        self.spec = normalized(spec)
        self.rng = rng
        self.faults = ([Fault(f, "client", self.spec) for f in self.spec["client_faults"]] +
                       [Fault(f, "backend", self.spec) for f in self.spec["backend_faults"]])
        # Coupled faults are allocated after the faults they depend on.
        self.faults.sort(key=lambda f: f.with_fault is not None)
        self.by_id = {f.id: f for f in self.faults}
        self.depth = self.spec["population"]["journey_depth"]
        self.streams = {f.id: quota.StreamingQuota(rng) for f in self.faults}
        rollbacks = {}
        for r in self.spec["rollouts"]:
            if r["kind"] == "app_version" and r.get("rollback_at"):
                rollbacks[r["subject"]] = parse_time(r["rollback_at"])
        for f in self.faults:
            versions = f.filters.get("app_versions") or []
            if versions and all(v in rollbacks for v in versions):
                f.rollbacks = [max(rollbacks[v] for v in versions)]
        cohort = next((r for r in self.spec["rollouts"] if r["kind"] == "feature_cohort"), None)
        self.cohort = cohort
        self.cohort_rollback = parse_time(cohort["rollback_at"]) if cohort and cohort.get("rollback_at") else None

    # --- settings for the V1 Plan --------------------------------------------------------------------

    def settings(self, start_ms):
        s = {"canary_share": self.cohort.get("canary_share", .25) if self.cohort else 0,
             "cohort_seed": (self.cohort or {}).get("cohort_seed", "vantra-2026-10"),
             "rollout_phase": "rollback" if self.cohort_rollback and start_ms >= self.cohort_rollback else "active",
             "crash_session_rate": 0, "blockage_session_rate": 0, "incident_phase": "incident",
             "journey_weights": {f"{catalog.PROFILE_FOR[k]}_priority": v for k, v in self.depth.items()}}
        pool = next((f for f in self.faults if f.variant == "payments-db-pool"), None)
        if pool and pool.window:
            # The session's incident phase is fixed at its planned start, so planned counts are exact.
            from .spec import phase_at
            s.update(incident_phase=phase_at(start_ms, pool.window) or "recovery", incident_window=pool.window)
        return s

    # --- eligibility -------------------------------------------------------------------------------------

    def eligible(self, f, skel, assigned):
        if skel["lob"] not in f.lobs:
            return False
        if f.type == "deploy_regression" and skel.get("service_versions", {}).get(f.meta["service"]) != f.details.get("version", f.meta["regression_version"]):
            return False
        if f.kind == "client" and f.raw.get("platform") not in ("any", skel["platform"]):
            return False
        if f.kind == "backend" and skel["platform"] not in (f.raw.get("platforms") or ["android", "web", "ios"]):
            return False
        flt = f.filters
        checks = (("app_versions", skel["app_version"]), ("os_versions", skel["os_version"]),
                  ("device_models", skel["model"]), ("device_tiers", skel["tier"]),
                  ("release_rings", skel["ring"]))
        if any(flt.get(dim) and value not in flt[dim] for dim, value in checks):
            return False
        if skel["platform"] == "web":
            from .web_capabilities import capabilities
            from .browser_session import releases
            caps = capabilities(skel["persona"])
            metric = {"layout_instability": "cls", "interaction_latency": "inp", "soft_navigation_slowness": "native_soft_navigation"}.get(f.type)
            if metric and not caps[metric]:
                return False
            if not web_pages(f, skel["lob"]):
                return False
            bounds=flt.get('browser_versions') or {}
            major=int(skel['persona']['browser_version'].split('.')[0])
            if not bounds.get('min',0) <= major <= bounds.get('max',9999):
                return False
            target = next(t for t in self.spec["targets"] if t["id"] == skel["target"])
            if f.type == "soft_navigation_slowness" and target["web"]["estate"][skel["lob"]]["archetype"] == "multi_page":
                return False
            if f.type == "chunk_load_failures":
                deployed = [r for r in releases(target["web"], skel["start_ms"]) if not r.get("deployed") or parse_time(r["deployed"]) <= skel["start_ms"]]
                newest = max(deployed, key=lambda r: r.get("deployed", ""))["version"]
                if skel["app_version"] == newest:
                    return False
            for dim, key in (("web_releases", "app_version"), ("browser_families", "browser"),
                             ("device_types", "kind"), ("connections", "connection")):
                value = skel["app_version"] if key == "app_version" else skel["persona"].get(key)
                if flt.get(dim) and value not in flt[dim]:
                    return False
        if flt.get("signed_in") is not None and flt["signed_in"] != skel["signed_in"]:
            return False
        if f.with_fault and f.with_fault not in assigned:
            return False
        if f.steer:
            return True
        natural = reach(skel["lob"], skel["shape"], skel["signed_in"]) | {"startup", "content"}
        return all(e in natural for e in self._stages(f, skel))

    def _stages(self, f, skel):
        return ({f.endpoint(skel["lob"], skel, self.rng)} | set(f.filters.get("endpoints") or [])) - {None}

    def available(self, f, skel, have):
        """Whether f can be placed in a session that already carries the faults in `have`."""
        if not self._free(f, skel, have):
            return False
        faults = [self.by_id[x] for x in have] + [f]
        if f.fatal and any(g.fatal for g in faults[:-1]):
            return False
        lob, order = skel["lob"], JOURNEY_ORDER[skel["lob"]]
        stages = {g.id: self._stages(g, skel) for g in faults}
        for g in faults:
            # A fatal crash, or a failure the journey cannot continue past, ends the session at its stage.
            if not (g.fatal or g.meta.get("ends_journey")):
                continue
            ends = [order.index(e) for e in stages[g.id] if e in order]
            if not ends:
                continue
            for h in faults:
                if h is not g and any(e in order and order.index(e) > min(ends) for e in stages[h.id]):
                    return False
        probe = dict(skel["shape"])
        for g in faults:
            if g.steer or g.kind == "backend":
                for e in stages[g.id]:
                    steer(lob, probe, e)
        reached = reach(lob, probe, skel["signed_in"]) | {"startup", "content"}
        return all(e in reached for g in faults for e in stages[g.id])

    # --- skeletons -----------------------------------------------------------------------------------------

    def skeleton(self, gen, target, start_ms, now_ms, trace_mode, lob=None):
        lob_mix = self.spec["population"]["line_of_business_mix"]
        weights = {catalog.PROFILE_FOR[k]: v for k, v in lob_mix.items()}
        profile, persona = gen.pick(catalog.PROFILE_FOR[lob] if lob else None, now_s=start_ms / 1000, weights=weights)
        lob = catalog.LOB_FOR[profile.key]
        settings = self.settings(start_ms)
        app = gen.app_overrides.get(profile.key, {})
        version = pick_version(app.get("versions") or profile.android["versions"], persona, self.rng)
        if target["platform"] == "web":
            from .browser_session import pick_release
            version = (pick_release(target["web"], start_ms, self.rng),)
        if self.spec.get('modeling', {}).get('releases'):
            from .adoption import choose
            version = choose(target, persona, start_ms, self.spec['modeling'], self.spec.get('seed', 7331), version)
        if target["platform"] != "web" and self.spec["population"].get("model") == "varied":
            # Batch planning selects return visits before the first one executes.
            persona.setdefault("installed_version", version[0])
        service_versions = {}
        for f in self.faults:
            if f.type != "deploy_regression":
                continue
            service, deployed = f.meta["service"], f.details.get("version", f.meta["regression_version"])
            rollout = next((r for r in self.spec["rollouts"] if r["kind"] == "service_version" and r["subject"] in (service, deployed)), {})
            active = (not rollout.get("start") or start_ms >= parse_time(rollout["start"])) and (not rollout.get("rollback_at") or start_ms < parse_time(rollout["rollback_at"]))
            import hashlib
            cohort = int(hashlib.sha256((persona["device_id"] + service + rollout.get("cohort_seed", "deploy")).encode()).hexdigest()[:8], 16) / 2**32
            if active and cohort < rollout.get("canary_share", 1):
                service_versions[service] = deployed
        from .population import VISIT_FIELDS
        return {"target": target["id"], "platform": target["platform"], "lob": lob, "profile": profile,
                "visit": {k: persona[k] for k in VISIT_FIELDS if k in persona},
                "service_versions": service_versions,
                "persona": persona, "version": version, "app_version": version[0],
                "os_version": persona.get("os_version"), "model": persona.get("model"),
                "tier": DEVICE_TIERS.get(persona.get("model"), "mid"), "signed_in": persona.get("user_id") is not None,
                "ring": assign_ring(persona, settings) if self.cohort else "stable", "start_ms": start_ms,
                "traces": trace_mode == "end_to_end" and limits.trace_eligible(start_ms, now_ms),
                "shape": self._shape(lob), "settings": settings}

    def _shape(self, lob):
        from .population import enabled, shape
        pop = self.spec["population"]
        return shape(lob, pop, self.rng) if enabled(pop) else natural_shape(lob, self.depth.get(lob, .2), self.rng)

    # --- allocation --------------------------------------------------------------------------------------------

    def allocate_batch(self, skeletons):
        exact = [f for f in self.faults if f.quota in ("auto", "exact")]
        # Backend stories are mutually exclusive. Reserve narrowly filtered
        # cohorts first: otherwise a broad fault can consume canary slots that
        # a later fault needs, despite enough eligible stable sessions remaining.
        # Keep dependent faults after their parents and client fault precedence.
        backend = sorted((f for f in exact if f.kind == "backend" and not f.with_fault),
                         key=lambda f: sum(self.eligible(f, sk, set()) and f.rate_at(sk["start_ms"]) > 0
                                           for sk in skeletons))
        exact = [f for f in exact if f.kind == "client" and not f.with_fault] + backend + [f for f in exact if f.with_fault]
        loose = [f for f in self.faults if f.quota == "probability"]
        assigned, report = quota.allocate_exact(
            skeletons, [f.raw for f in exact], lambda raw, sk: self.by_id[raw["id"]].rate_at(sk["start_ms"]),
            lambda raw, sk, have: self.eligible(self.by_id[raw["id"]], sk, have),
            lambda raw, sk, have: self.available(self.by_id[raw["id"]], sk, have), self.rng)
        for i, sk in enumerate(skeletons):
            have = assigned[i]
            for f in exact:
                onset = f.onset_rate_at(sk["start_ms"])
                if (onset and self.eligible(f, sk, set(have)) and self.available(f, sk, set(have))
                        and quota.draw_probability(onset, self.rng)):
                    have.append(f.id)
            for f in loose:
                if (self.eligible(f, sk, set(have)) and self.available(f, sk, set(have))
                        and quota.draw_probability(f.rate_at(sk["start_ms"]), self.rng)):
                    have.append(f.id)
        return [assigned[i] for i in range(len(skeletons))], report

    def allocate_one(self, skel):
        """Live streams: probability for auto/probability faults, streaming quotas for exact faults."""
        have, considered = [], {}
        for f in self.faults:
            if not self.eligible(f, skel, set(have)):
                continue
            if not self.available(f, skel, set(have)):
                if f.group:
                    considered[f.group] = considered.get(f.group, 0.0) + f.rate_at(skel["start_ms"])
                continue
            rate = f.rate_at(skel["start_ms"]) + f.onset_rate_at(skel["start_ms"])
            if f.quota == "exact":
                hit = self.streams[f.id].take(rate)
            else:
                # Sequential draws inside a group: conditional on no earlier member having been drawn.
                prior = considered.get(f.group, 0.0) if f.group else 0.0
                hit = quota.draw_probability(min(1.0, rate / max(1e-9, 1 - prior)), self.rng)
                if f.group:
                    considered[f.group] = prior + rate
            if hit:
                have.append(f.id)
        return have

    def _free(self, f, skel, have):
        return not f.group or all(self.by_id[x].group != f.group for x in have)

    # --- assignment -------------------------------------------------------------------------------------------

    def assignment(self, skel, fault_ids):
        """V1 Plan fields for one session; also finalizes the journey shape."""
        lob = skel["lob"]
        shape = skel["shape"]
        original_shape = dict(shape)
        a = {"primary": "clean", "incident": None, "client_fault": None, "fault_ids": list(fault_ids),
             "http_errors": {}, "service_versions": skel.get("service_versions", {})}
        handled_followup = 0.0
        for fid in fault_ids:
            f = self.by_id[fid]
            d = f.details
            endpoint = f.endpoint(lob, skel, self.rng)
            if f.steer or f.kind == "backend":
                steer(lob, shape, endpoint)
            else:
                shape["short"] = False
            for e in f.filters.get("endpoints") or []:
                if f.steer:
                    steer(lob, shape, e)
            if f.kind == "backend":
                a["primary"], a["incident"] = f.meta["story"], f.meta["v1_incident"]
                if f.meta.get("builder") == "scenario":
                    a["incident"] = None
                    a["backend_fault"] = dict(f.raw, service=f.meta["service"], endpoint=endpoint, details=d)
                    if f.type == "cache_outage" and d.get("fallback") is False:
                        a["primary"] = "mobile"
                handled_followup = d.get("handled_followup", handled_followup)
            elif f.type in ("jvm_crash","ios_crash"):
                a["client_fault"] = "crash"
                a["crash_family"] = f.family(skel, self.rng)
                a["crash_details"] = {k: d[k] for k in ("endpoint", "on_failure_only", "exception", "message", "method",
                                                        "class", "line") if k in d}
            elif f.type == "main_thread_blockage":
                a["blockage"] = True
                a["blockage_details"] = {k: d[k] for k in ("min_ms", "max_ms", "cause") if k in d}
            elif f.type == "handled_exception":
                a["handled"] = True
                if d.get("story"):
                    # V1's handled-error story for banking and travel: no backend effect.
                    a["primary"] = a["primary"] if a["incident"] else "handled"
                    a["incident"] = a["incident"] or catalog.HANDLED_ONLY_INCIDENTS.get(catalog.PROFILE_FOR[lob])
            elif f.type == "network_failure":
                kinds = d.get("kinds") or sorted(catalog.NETWORK_FAILURES)
                a["transport"] = self.rng.choice(kinds)
                a["transport_target"] = d.get("endpoint")
                if d.get("story"):
                    a["primary"] = a["primary"] if a["incident"] else d["story"]
                    handled_followup = d.get("handled_followup", handled_followup)
            elif f.type == "slow_startup":
                a["slow_startup"] = dict(min_ms=4200, max_ms=7500)
                a["slow_startup"].update({k: d[k] for k in ("min_ms", "max_ms", "phase") if k in d})
            elif f.type == "slow_root_span":
                a["slow_root_span"] = {"span": d["span"], "p50_ms": d["p50_ms"], "p95_ms": d["p95_ms"]}
            elif f.type == "http_error":
                a["http_errors"][d["endpoint"]] = {k: d[k] for k in ("status", "service") if k in d}
            elif skel["platform"] == "web":
                a.setdefault("web_faults", []).append(f.raw)
                if f.type in ("chunk_load_failures", "soft_navigation_slowness"):
                    shape["short"] = False
                    shape["priority"] = True
        if fault_ids:
            shape["short"] = shape["short"] and all(self.by_id[x].endpoint(lob, skel, self.rng) == "lock_card"
                                                    for x in fault_ids)
        a["handled_followup"] = handled_followup
        if self.cohort_rollback and skel["start_ms"] >= self.cohort_rollback:
            a["rollback_clears_canary"] = True
        if shape != original_shape:
            shape["steered"] = True
        return a
