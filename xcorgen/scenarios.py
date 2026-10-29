"""Seeded session policy and bounded, request-local application context.

This module models synthetic demonstration data. It never determines coverage
from its own scenario labels: validation inspects the emitted records.
"""
import hashlib
import random
from contextvars import ContextVar
from dataclasses import dataclass, field

from .util import weighted

DEFAULT_WEIGHTS = dict(clean=.30, mobile=.21, backend=.21, slow=.15,
                       handled=.10, blockage=.016, crash=.014)
FEATURES = {
    "ecommerce": {"place_order": "payment_orchestration", "get_recommendations": "recommendation_serving"},
    "banking": {"accounts": "banking_api_routing", "create_transfer": "banking_api_routing"},
    "travel": {"create_booking": "payment_orchestration", "flight_search": "search_aggregation",
               "hotel_search": "search_aggregation"},
}
INCIDENTS = {
    "ecommerce": {"mobile": ("payments-db-pool", "payment-timeouts"), "backend": ("recs-latency",),
                  "slow": ("reviews-n-plus-one", "recs-latency"), "handled": ("inventory-sync",)},
    "banking": {"mobile": ("fraud-latency", "rails-outage"), "backend": ("account-read-fallback",),
                "slow": ("core-banking-latency", "statements-slow", "fraud-latency"), "handled": ("amount-validation",)},
    "travel": {"mobile": ("payments-db-pool", "gds-pnr-timeouts"), "backend": ("supplier-timeouts", "hotel-supplier-outage"),
               "slow": ("search-complexity",), "handled": ("traveler-validation",)},
}
REQUEST = ContextVar("request_context", default=None)


def fraction(identity, salt):
    return int.from_bytes(hashlib.sha256(f"{salt}:{identity}".encode()).digest()[:8], "big") / 2**64


@dataclass
class Runtime:
    """State shared by requests, never stored in Redis connection resources."""
    cache: dict = field(default_factory=dict)
    operations: dict = field(default_factory=dict)

    def lookup(self, key, now, ttl=60_000, initial_hit=.55):
        entry = self.cache.get(key)
        if entry is None:
            # A deployment starts with a populated cache from preceding traffic.
            entry = now + ttl * .5 if fraction(key, "cache-prime") < initial_hit else now - 1
            self.cache[key] = entry
        return entry > now

    def fill(self, key, now, ttl=60_000):
        self.cache[key] = now + ttl
        if len(self.cache) > 20_000:
            self.cache = {k: v for k, v in self.cache.items() if v > now}


class Plan:
    def __init__(self, session, runtime, settings=None, forced=None):
        self.s, self.runtime, self.settings = session, runtime, settings or {}
        self.seed = str(self.settings.get("cohort_seed", "vantra-2026-10"))
        identity = session.persona.get("user_id") or session.persona["device_id"]
        assignments = session.persona.setdefault("rollout_assignments", {})
        assignments.setdefault(self.seed, "canary" if fraction(identity, self.seed) < self.settings.get("canary_share", .25) else "stable")
        self.ring = assignments[self.seed]
        self.rollback = self.settings.get("rollout_phase", "active") == "rollback"
        # Assignment precedes scenario choice; the conditional model produces
        # 80% affected / 20% healthy canaries, while preserving global budgets.
        weights = dict(DEFAULT_WEIGHTS, **self.settings.get("scenario_weights", {}))
        share = self.settings.get("canary_share", .25)
        canary = dict(clean=.20, mobile=.20, backend=.60)
        if self.ring == "canary" and not self.rollback:
            choice = canary
        elif not self.rollback:
            choice = {k: (v - share * canary.get(k, 0)) / (1 - share) for k, v in weights.items()}
        else:
            choice = weights
        if any(v < 0 for v in choice.values()) or abs(sum(weights.values()) - 1) > 1e-6:
            raise ValueError("scenario_weights must sum to 1 and accommodate the configured canary allocation")
        self.primary = weighted(list(choice.items()), session.rng)
        self.primary = self.settings.get("primary_scenario") or self.primary
        if self.rollback and self.ring == "canary":
            self.primary = "clean"
        self.incident = forced or session.rng.choice(INCIDENTS[session.profile.key].get(self.primary, (None,)))
        if forced:
            self.primary = self.settings.get("primary_scenario") or next((k for k, values in INCIDENTS[session.profile.key].items() if forced in values), "mobile")
        self.phase = self.settings.get("incident_phase", "incident")
        self.occurrence = f"payments-{int(session.start_ms // 1_200_000)}"
        self.target = None
        self.used = False
        self.attempts = {}
        self.contexts = []
        self.evidence = []
        self.operation_id = hashlib.sha256(f"{session.user_session_id}:operation".encode()).hexdigest()[:24]
        self.crash_family = weighted([(0, 40), (1, 25), (2, 15), (3, 12), (4, 8)], session.rng)
        if self.settings.get("crash_family") is not None:
            self.crash_family = int(self.settings["crash_family"])
        session.properties.update({"release_ring": self.ring, "feature_exposed": "false"})

    def request(self, key, params):
        s = self.s
        self.attempts[key] = self.attempts.get(key, 0) + 1
        feature = FEATURES[s.profile.key].get(key)
        context = {"release_ring": self.ring, "business_unit": s.profile.android["business_unit"],
                   "app.version": s.app_version, "feature_exposed": "false"}
        if feature:
            variant = "v2" if self.ring == "canary" and not self.rollback else "legacy"
            prop = f"feature.{feature}"
            s.properties.update({prop: variant, "feature_exposed": "true"})
            context.update({prop: variant, "feature_exposed": "true", "routing.policy": f"{feature}.{variant}",
                            "routing.reason": "stable_identity", "routing.target": "canary" if variant == "v2" else "stable"})
        if key in ("place_order", "create_booking", "create_transfer") or key.endswith("_status"):
            context["operation.id"] = self.operation_id
            context["retry.attempt"] = self.attempts[key] - 1
        incident = self.incident if key == self.target and not self.used else None
        if incident == "payments-db-pool":
            self.occurrence = f"payments-{int(s.t // 1_200_000)}"
            context.update({"incident.occurrence_id": self.occurrence, "incident.phase": self.phase,
                            "routing.reason": "weighted_deployment_route",
                            "routing.target": "payments-hot", "db.client.connection.pool.name": "HikariPool-1"})
            if self.phase == "recovery":
                incident = None
        if key == self.target:
            self.used = True
        outcome = "ok"
        if incident:
            outcome = {"payments-db-pool": "payment_db_pool", "payment-timeouts": "processor_timeout",
                       "rails-outage": "rail_unavailable", "gds-pnr-timeouts": "pnr_timeout",
                       "inventory-sync": "out_of_stock", "hotel-supplier-outage": "supplier_503",
                       "supplier-timeouts": "supplier_timeout", "account-read-fallback": "snapshot_fallback",
                       "recs-latency": "fallback" if self.primary == "backend" else "slow_model",
                       "fraud-latency": "fraud_timeout" if self.primary == "mobile" else "fraud_slow"}.get(incident, "ok")
        if incident == "payments-db-pool" and self.phase == "onset":
            outcome = "ok"
        req = {"attributes": context, "plan": self, "key": key, "incident": incident, "outcome": outcome,
               "now": s.t, "params": params}
        self.contexts.append(dict(context))
        return req


def current():
    return REQUEST.get()


def cache_read(svc, purpose, key, now=None, ttl=60_000, initial_hit=.55, force_miss=False, fill=True):
    req = current()
    if req is None:
        return random.random() < initial_hit
    plan = req["plan"]
    now = req["now"] if now is None else now
    cache_key = (svc.name, purpose, key)
    hit = plan.runtime.lookup(cache_key, now, ttl, initial_hit) and not force_miss
    if not hit and fill:
        plan.runtime.fill(cache_key, now, ttl)
    return hit
