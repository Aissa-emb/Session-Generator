"""Population evidence read back from serialized SDK session/network spans."""
from .population import proportions

DIMENSIONS = ("visit_intent", "visitor_type", "network_profile", "hardware_tier", "market")


def capture(obs, attributes, start_ms, end_ms):
    obs["visit_start_ms"] = min(obs.get("visit_start_ms", start_ms), start_ms)
    obs["visit_end_ms"] = max(obs.get("visit_end_ms", end_ms), end_ms)
    for key in (*DIMENSIONS, "journey_steered", "visit_outcome"):
        if "emb.properties." + key in attributes:
            obs[key] = attributes["emb.properties." + key]
    if "journey_steered" in obs:
        obs["journey_steered"] = str(obs["journey_steered"]).lower() == "true"
    obs["session_parts"] = obs.get("session_parts", 0) + 1
    if start_ms == obs["visit_start_ms"]:
        obs["cold_start"] = str(attributes.get("emb.cold_start")).lower() == "true"


def summary(observations):
    from .emitted import percentile
    rows = []
    for platform, lob in sorted({(o["platform"], o.get("lob")) for o in observations if o.get("visit_intent")}):
        cohort = [o for o in observations if o["platform"] == platform and o.get("lob") == lob and o.get("visit_intent")]
        n = len(cohort)
        duration = [(o["visit_end_ms"] - o["visit_start_ms"]) / 1000 for o in cohort]
        funnel = {"retail": ("home", "search", "get_product", "add_to_cart", "get_cart", "place_order"),
                  "banking": ("accounts", "transactions", "payees", "create_transfer", "statement"),
                  "travel": ("flight_search", "hotel_search", "offer", "create_booking", "trips")}[lob]
        rows.append({"platform": platform, "line_of_business": lob, "visits": n,
                     "dimensions": {key: proportions(o.get(key, "unknown") for o in cohort) for key in DIMENSIONS},
                     "duration_seconds": {f"p{int(q * 100)}": round(percentile(duration, q), 2) for q in (.5, .75, .95)},
                     "steered_visits": sum(bool(o.get("journey_steered")) for o in cohort),
                     "cold_visits": sum(o.get("cold_start", False) for o in cohort),
                     "returning_visits": sum(o.get("visitor_type") == "returning" for o in cohort),
                     "returning_cold_visits": sum(o.get("visitor_type") == "returning" and o.get("cold_start", False) for o in cohort),
                     "visits_with_multiple_parts": sum(o.get("session_parts", 0) > 1 for o in cohort),
                     "visits_with_inp": sum(any(v["name"] == "inp" for v in o.get("vitals", [])) for o in cohort),
                     "funnel": [{"endpoint": key,
                                 "attempted_visits": sum(any(r["endpoint"] == key for r in o["net"]) for o in cohort),
                                 "successful_visits": sum(any(r["endpoint"] == key and r["status"] is not None and 200 <= r["status"] < 300
                                                              for r in o["net"]) for o in cohort)} for key in funnel]})
    return rows
