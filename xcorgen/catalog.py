"""Fault catalog: every client and backend fault a spec can request, and how the engine produces it.

Backend variants reuse the V1 incidents and their builders. Each variant
names the session story it creates (V1's primary scenario: visible failure,
hidden degradation, slow response or handled error), the endpoint it lands on
per line of business, and the evidence that proves it in emitted payloads,
so the requested-versus-emitted report never trusts the plan.
"""

LINES_OF_BUSINESS = ("retail", "banking", "travel")
PROFILE_FOR = {"retail": "ecommerce", "banking": "banking", "travel": "travel"}
LOB_FOR = {v: k for k, v in PROFILE_FOR.items()}

# --- client faults ---------------------------------------------------------------------------------------

CLIENT_FAULTS = {
    "jvm_crash": {"platforms": ("android",), "fatal": True, "supported": ("android",),
                  "summary": "Fatal JVM crash (sys.android.crash); the process terminates and the crash uploads on the next launch."},
    "main_thread_blockage": {"platforms": ("android",), "supported": ("android",),
                             "summary": "Main-thread blockage captured as perf.thread_blockage with samples (Cortex XCOR RUM's ANR signal)."},
    "handled_exception": {"platforms": ("android",), "supported": ("android",),
                          "summary": "Handled exception (sys.exception, handling=handled) with a Kotlin stack."},
    "network_failure": {"platforms": ("android", "web"), "supported": ("android", "web"),
                        "summary": "Request fails without an HTTP response (timeout, reset, DNS, offline, TLS); a retry usually succeeds."},
    "slow_startup": {"platforms": ("android",), "supported": ("android",),
                     "summary": "Cold start over 4 s, with the time in application init, SDK init or first render."},
    "http_error": {"platforms": ("android", "web"), "supported": ("android", "web"),
                   "summary": "A backend returns a 4xx or 5xx on a named endpoint, with a matching backend trace."},
    "slow_root_span": {"platforms": ("android", "web"), "supported": ("android", "web"),
                       "summary": "A named business root span reaches a target p50 and p95 duration."},
}
for _web in ("js_exception", "chunk_load_failures", "poor_web_vitals", "lcp_regression", "layout_instability",
             "interaction_latency", "slow_server_response", "soft_navigation_slowness", "rage_clicks",
             "page_abandonment"):
    CLIENT_FAULTS[_web] = {"platforms": ("web",), "supported": ("web",), "fatal": _web == "page_abandonment",
                           "summary": "Web fault (section 7.6); produced by the web generator."}

NETWORK_FAILURES = {
    "timeout": ("java.net.SocketTimeoutException", "timeout", True),
    "reset": ("java.net.SocketException", "Connection reset", True),
    "dns": ("java.net.UnknownHostException", "Unable to resolve host \"{host}\": No address associated with hostname", False),
    "offline": ("java.net.ConnectException", "Failed to connect to {host}/443", False),
    "tls": ("javax.net.ssl.SSLHandshakeException", "Connection closed by peer", False),
    "cancelled": ("java.io.IOException", "Canceled", False),
}
# Kinds whose request reached the gateway: end-to-end mode still produces a backend trace for them.
REACHED_SERVER = {k for k, (_, _, reached) in NETWORK_FAILURES.items() if reached}

# Default request a client network failure lands on, per line of business (V1 transport targets).
TRANSPORT_TARGET = {"retail": "search", "banking": "accounts", "travel": "flight_search"}

STARTUP_PHASES = ("application_init", "sdk_init", "first_render")

# --- backend faults --------------------------------------------------------------------------------------

# story: V1 primary scenario the journey follows (mobile = visible failure, backend = hidden degradation,
# slow = slow response, handled = handled error). endpoints: line of business -> endpoint key.
BACKEND_VARIANTS = {
    "payments-db-pool": dict(
        type="db_pool_exhaustion", service="payment-authorization-service", story="mobile",
        endpoints={"retail": "place_order", "travel": "create_booking"},
        summary="Payments database pool exhaustion: checkouts and bookings fail together with 502, and the slow "
                "payment_tokens lock query that drains the pool is visible."),
    "payment-timeouts": dict(
        type="downstream_timeout", service="payment-authorization-service", story="mobile",
        endpoints={"retail": "place_order"},
        summary="Card processor timeouts: payment-authorization-service gives up after 8 s and checkout returns 504."),
    "inventory-sync": dict(
        type="validation_conflict", service="inventory-availability-service", story="handled",
        endpoints={"retail": "add_to_cart"}, ends_journey=True,
        summary="Inventory sync conflicts: add to bag returns 409 for items the storefront still lists."),
    "inventory-deadlock": dict(
        type="db_deadlock", service="inventory-availability-service", story="mobile", v1_incident="inventory-deadlock",
        endpoints={"retail": "place_order"},
        summary="Stock reservation deadlocks: Postgres aborts the reservation and checkout returns 409 after retries."),
    "reviews-n-plus-one": dict(
        type="n_plus_one", service="product-reviews-service", story="slow", endpoints={"retail": "get_product"},
        summary="Review media N+1: product pages run one media query per review."),
    "recs-latency": dict(
        type="dependency_latency", service="personalization-service", story="slow",
        endpoints={"retail": "get_recommendations"},
        summary="Recommendation latency: model inference takes 1–3 s."),
    "recs-fallback": dict(
        type="downstream_timeout", service="personalization-service", story="backend", v1_incident="recs-latency",
        endpoints={"retail": "get_recommendations"},
        summary="Recommendation timeouts: the experience API serves default recommendations with HTTP 200."),
    "fraud-latency": dict(
        type="downstream_timeout", service="fraud-risk-scoring-service", story="mobile",
        endpoints={"banking": "create_transfer"},
        summary="Fraud model latency: transfers hit the 4 s gRPC deadline and fail with 504."),
    "fraud-slow": dict(
        type="dependency_latency", service="fraud-risk-scoring-service", story="slow", v1_incident="fraud-latency",
        endpoints={"banking": "create_transfer"},
        summary="Fraud scoring slows to 2–6 s; transfers succeed slowly."),
    "rails-outage": dict(
        type="dependency_5xx", service="payment-rails-gateway", story="mobile", endpoints={"banking": "create_transfer"},
        summary="Instant-payments outage: the payment network returns 503."),
    "core-banking-latency": dict(
        type="slow_query", service="core-banking-gateway", story="slow", endpoints={"banking": "accounts"},
        summary="Core banking latency: balance inquiries that miss the cache take 1.4–3.8 s."),
    "account-read-fallback": dict(
        type="downstream_timeout", service="account-summary-service", story="backend", endpoints={"banking": "accounts"},
        summary="Account read fallback: a core read deadline is served from a read-only snapshot with HTTP 200."),
    "statements-slow": dict(
        type="dependency_latency", service="statement-rendering-service", story="slow", endpoints={"banking": "statement"},
        summary="Statement rendering latency: statement PDFs take 3–6 s."),
    "supplier-timeouts": dict(
        type="partial_results", service="ndc-connector-service", story="backend", endpoints={"travel": "flight_search"},
        summary="Supplier timeouts: one airline adapter times out at 3 s and search returns partial results."),
    "gds-pnr-timeouts": dict(
        type="downstream_timeout", service="pnr-management-service", story="mobile", endpoints={"travel": "create_booking"},
        summary="PNR creation timeouts: bookings fail with 504 and move to reconciliation."),
    "hotel-supplier-outage": dict(
        type="dependency_5xx", service="lodging-search-service", story="backend", endpoints={"travel": "hotel_search"},
        summary="Hotel supplier outage: the supplier returns 503 and results fall back to cached rates."),
    "search-complexity": dict(
        type="slow_query", service="air-shopping-service", story="slow", endpoints={"travel": "flight_search"},
        summary="Complex itinerary searches bypass the cache and run slowly."),
}
# Emitted evidence per variant. "client" is visible in Cortex XCOR RUM payloads (status on the variant's
# endpoint, or a slow request); "backend" needs the trace and is used when the session has one.
EVIDENCE = {
    "payments-db-pool": {"client": {"status": 502}, "backend": {"span_error": "HikariPool.getConnection"}},
    "payment-timeouts": {"client": {"status": 504}},
    "inventory-sync": {"client": {"status": 409}},
    "inventory-deadlock": {"client": {"status": 409}, "backend": {"error_contains": "deadlock detected"}},
    "reviews-n-plus-one": {"client": {"slow_ms": 1200}, "backend": {"repeated": "SELECT reviews.review_media", "min": 6}},
    "recs-latency": {"client": {"slow_ms": 1000, "unless_breadcrumb": "default_recommendations_displayed"},
                     "backend": {"slow_span": "ranker.predict", "min_ms": 950}},
    "recs-fallback": {"client": {"breadcrumb": "default_recommendations_displayed"}, "backend": {"error_ok_client": True}},
    "fraud-latency": {"client": {"status": 504}},
    "fraud-slow": {"client": {"slow_ms": 1800}, "backend": {"slow_span": "model.predict", "min_ms": 1700}},
    "rails-outage": {"client": {"status": 503}},
    "core-banking-latency": {"client": {"slow_ms": 1950},
                             "backend": {"slow_span": "POST", "peer": "core-banking-system", "min_ms": 1350}},
    "account-read-fallback": {"client": {"log_contains": "Account snapshot is read-only"}, "backend": {"error_ok_client": True}},
    "statements-slow": {"client": {"slow_ms": 3000}, "backend": {"slow_span": "pdf.render", "min_ms": 2900}},
    "supplier-timeouts": {"client": {"breadcrumb": "partial_flight_results_displayed"}, "backend": {"error_ok_client": True}},
    "gds-pnr-timeouts": {"client": {"status": 504}},
    "hotel-supplier-outage": {"client": {"breadcrumb": "cached_hotel_rates_displayed"}, "backend": {"error_ok_client": True}},
    "search-complexity": {"client": {"slow_ms": 4000}, "backend": {"slow_span": "fare_combinations.rank", "min_ms": 3900}},
}
from .backend_faults import VARIANTS as NEW_BACKEND_VARIANTS, EVIDENCE as NEW_BACKEND_EVIDENCE
BACKEND_VARIANTS.update({k: dict(v, builder="scenario") for k, v in NEW_BACKEND_VARIANTS.items()})
EVIDENCE.update(NEW_BACKEND_EVIDENCE)
for _key, _v in BACKEND_VARIANTS.items():
    _v.setdefault("v1_incident", _key)
    _v["supported"] = True
    _v["evidence"] = EVIDENCE[_key]

# Types with no V1 builder yet. Validation rejects them instead of promising data the engine cannot produce.
PLANNED_BACKEND_TYPES = {}
BACKEND_TYPES = sorted({v["type"] for v in BACKEND_VARIANTS.values()} | set(PLANNED_BACKEND_TYPES))

# Handled-error stories with no backend effect (V1 handled scenarios for banking and travel).
HANDLED_ONLY_INCIDENTS = {"banking": "amount-validation", "travel": "traveler-validation"}

ROOT_SPANS = {
    "retail": ("search_products", "load_product", "load_recommendations", "update_cart", "load_cart", "quote_shipping",
               "authorize_payment", "load_order_confirmation", "reconcile_operation"),
    "banking": ("authenticate", "verify_identity_challenge", "load_accounts", "load_transaction_history",
                "load_recipients", "submit_transfer", "download_statement", "lock_card", "reconcile_operation"),
    "travel": ("search_flights", "search_hotels", "revalidate_hotel_rate", "revalidate_fare", "authorize_booking",
               "load_itineraries", "reconcile_operation"),
}


def variants_for(type_, lines_of_business=None):
    lobs = set(lines_of_business or LINES_OF_BUSINESS)
    return [k for k, v in BACKEND_VARIANTS.items() if v["type"] == type_ and lobs & set(v["endpoints"])]


def resolve_variant(fault):
    """Catalog key for a backend fault entry, or None when the type has no unambiguous variant."""
    if fault.get("variant"):
        return fault["variant"] if fault["variant"] in BACKEND_VARIANTS else None
    candidates = variants_for(fault["type"], fault.get("lines_of_business"))
    if fault.get("service"):
        candidates = [k for k in candidates if BACKEND_VARIANTS[k]["service"] == fault["service"]]
    return candidates[0] if len(candidates) == 1 else None


def describe():
    """Catalog for get_capabilities and the UI."""
    return {
        "client_faults": {k: {"platforms": list(v["platforms"]), "supported_platforms": list(v["supported"]),
                              "fatal": bool(v.get("fatal")), "summary": v["summary"]} for k, v in CLIENT_FAULTS.items()},
        "backend_variants": {k: {"type": v["type"], "service": v["service"], "lines_of_business": sorted(v["endpoints"]),
                                 "endpoints": v["endpoints"], "summary": v["summary"]} for k, v in BACKEND_VARIANTS.items()},
        "planned_backend_types": PLANNED_BACKEND_TYPES,
        "network_failures": sorted(NETWORK_FAILURES),
        "root_spans": {k: list(v) for k, v in ROOT_SPANS.items()},
    }
