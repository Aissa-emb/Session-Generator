"""Seeded session policy and bounded, request-local application context.

This module models synthetic demonstration data. It never determines coverage
from its own scenario labels: validation inspects the emitted records.
"""
import hashlib
import math
import random
from contextvars import ContextVar
from dataclasses import dataclass, field

from .util import weighted

DEFAULT_WEIGHTS = dict(clean=.15, mobile=.30, backend=.25, slow=.15,
                       handled=.15, blockage=0, crash=0)
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


def validate_settings(settings):
    """Reject impossible budgets before generating or exporting any sessions."""
    def number(key, default, low, high):
        value = settings.get(key, default)
        if not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"generation.{key} must be finite and between {low} and {high}")
        return value
    share = number("canary_share", .25, 0, .999)
    number("transport_share", .4, 0, 1)
    crash_rate = number("crash_session_rate", .10, 0, 1)
    blockage_rate = number("blockage_session_rate", .30, 0, 1)
    if crash_rate + blockage_rate > 1:
        raise ValueError("Crash and blockage session rates must sum to at most 1")
    cycle = number("incident_cycle_minutes", 60, 2, 1440)
    onset = number("incident_onset_minutes", 2, 0, cycle)
    recovery = number("incident_recovery_minutes", 6, 0, cycle)
    if onset + recovery >= cycle:
        raise ValueError("Incident onset and recovery must leave an active incident window")
    if settings.get("incident_phase", "scheduled") not in ("scheduled", "onset", "incident", "recovery"):
        raise ValueError("Unknown incident_phase")
    if settings.get("rollout_phase", "active") not in ("active", "rollback"):
        raise ValueError("Unknown rollout_phase")
    if settings.get("primary_scenario") not in (None, *DEFAULT_WEIGHTS):
        raise ValueError("Unknown primary_scenario")
    if settings.get("crash_family") not in (None, 0, 1, 2, 3, 4):
        raise ValueError("crash_family must be 0..4")
    weights = dict(DEFAULT_WEIGHTS, **settings.get("scenario_weights", {}))
    if (set(weights) != set(DEFAULT_WEIGHTS) or
            any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in weights.values()) or
            abs(sum(weights.values()) - 1) > 1e-6 or
            any(weights[k] < share * v for k, v in dict(clean=.2, mobile=.2, backend=.6).items())):
        raise ValueError("scenario_weights must sum to 1 and accommodate the configured canary allocation")
    for key, value in settings.get("journey_weights", {}).items():
        if key not in ("ecommerce_priority", "banking_priority", "travel_priority") or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError("journey_weights must use profile_priority keys and probabilities in [0, 1]")


def incident_phase_at(timestamp_ms, settings):
    phase = settings.get("incident_phase", "scheduled")
    if phase != "scheduled":
        return phase
    window = settings.get("incident_window")
    if window:
        from .spec import phase_at
        return phase_at(timestamp_ms, window) or "recovery"
    cycle = settings.get("incident_cycle_minutes", 60)
    minute = (timestamp_ms / 60000) % cycle
    return ("onset" if minute < settings.get("incident_onset_minutes", 2) else
            "recovery" if minute >= cycle - settings.get("incident_recovery_minutes", 6) else "incident")


def fraction(identity, salt):
    return int.from_bytes(hashlib.sha256(f"{salt}:{identity}".encode()).digest()[:8], "big") / 2**64


def occurrence_id(timestamp_ms, settings):
    """Shared incident occurrence: Shop and Travel requests in one window report the same ID."""
    window = settings.get("incident_window")
    if window:
        period = window["repeat_ms"] or 10**15
        return f"payments-{int((timestamp_ms - window['start_ms']) // period + window['start_ms'] // 60000)}"
    return f"payments-{int(timestamp_ms // (settings.get('incident_cycle_minutes', 60) * 60000))}"


def assign_ring(persona, settings):
    """Stable release ring for a visitor; persisted so an anonymous visitor keeps it after signing in."""
    seed = str(settings.get("cohort_seed", "vantra-2026-10"))
    identity = persona.get("user_id") or persona["device_id"]
    assignments = persona.setdefault("rollout_assignments", {})
    assignments.setdefault(seed, "canary" if fraction(identity, seed) < settings.get("canary_share", .25) else "stable")
    return assignments[seed]


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
    def __init__(self, session, runtime, settings=None, forced=None, assignment=None):
        self.s, self.runtime, self.settings = session, runtime, settings or {}
        self.seed = str(self.settings.get("cohort_seed", "vantra-2026-10"))
        self.ring = assign_ring(session.persona, self.settings)
        self.rollback = self.settings.get("rollout_phase", "active") == "rollback"
        self.handled = False
        self.transport = self.transport_target = None
        self.transport_used = False
        self.slow_startup = self.slow_root_span = None
        self.http_errors = {}
        self.crash_details = {}
        if assignment is not None:
            self._assigned(session, assignment)
            return
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
        # Client faults are assigned across both rollout groups, before any
        # journey executes. One client-fault budget can overlap a backend issue.
        if self.primary in ("crash", "blockage"):
            self.client_fault = self.primary
        elif forced or self.settings.get("primary_scenario"):
            self.client_fault = None  # explicit controls stay reproducible
        else:
            cr = self.settings.get("crash_session_rate", .10)
            bl = self.settings.get("blockage_session_rate", .30)
            self.client_fault = weighted([(None, 1 - cr - bl), ("crash", cr), ("blockage", bl)], session.rng)
        self._common(session)
        self.crash_family = weighted([(0, 40), (1, 25), (2, 15), (3, 12), (4, 8)], session.rng)
        if self.settings.get("crash_family") is not None:
            self.crash_family = int(self.settings["crash_family"])

    def _common(self, session):
        self.phase = incident_phase_at(session.start_ms, self.settings)
        self.occurrence = occurrence_id(session.start_ms, self.settings)
        self.target = None
        self.used = False
        self.attempts = {}
        self.contexts = []
        self.evidence = []
        self.operation_id = hashlib.sha256(f"{session.user_session_id}:operation".encode()).hexdigest()[:24]
        session.properties.update({"release_ring": self.ring, "feature_exposed": "false"})

    def _assigned(self, session, a):
        """Take the outcome a ScenarioSpec planner allocated, instead of drawing one."""
        self.primary = a.get("primary", "clean")
        self.incident = a.get("incident")
        self.client_fault = a.get("client_fault")
        self.crash_family = a.get("crash_family", 0)
        self.crash_details = a.get("crash_details") or {}
        self.blockage_details = a.get("blockage_details") or {}
        self.blockage = bool(a.get("blockage"))
        self.handled_followup = a.get("handled_followup", 0.0)
        self.handled = bool(a.get("handled"))
        self.transport = a.get("transport")
        self.transport_target = a.get("transport_target")
        self.slow_startup = a.get("slow_startup")
        self.slow_root_span = a.get("slow_root_span")
        self.http_errors = dict(a.get("http_errors") or {})
        self.fault_ids = list(a.get("fault_ids", []))
        if self.rollback and self.ring == "canary" and a.get("rollback_clears_canary"):
            self.primary, self.incident = "clean", None
        self._common(session)

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
            self.phase = incident_phase_at(s.t, self.settings)
            self.occurrence = occurrence_id(s.t, self.settings)
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
                       "inventory-deadlock": "inventory_conflict",
                       "recs-latency": "fallback" if self.primary == "backend" else "slow_model",
                       "fraud-latency": "fraud_timeout" if self.primary == "mobile" else "fraud_slow"}.get(incident, "ok")
        if incident == "payments-db-pool" and self.phase == "onset":
            outcome = "ok"
        http_error = self.http_errors.pop(key, None) if not incident else None
        req = {"attributes": context, "plan": self, "key": key, "incident": incident, "outcome": outcome,
               "now": s.t, "params": params, "http_error": http_error}
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
