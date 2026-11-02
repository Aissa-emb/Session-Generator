"""Versioned demo population assumptions, separate from fault incidence.

Weights are configurable session probabilities, not claims about market share.
Network conditions are per visit; hardware and locale belong to the visitor.
"""
from collections import Counter

from .util import weighted

VERSION = "1.0"
VISIT_FIELDS = ("returning_visit", "connection", "network", "mbps", "rtt", "effective_type")
INTENTS = {
    "retail": {"browser": 30, "buyer": 20, "cart_abandoner": 20, "searcher": 15, "bouncer": 10, "power_user": 5},
    "banking": {"quick_check": 35, "history_review": 30, "transfer": 15, "transfer_abandoner": 10, "statement": 7, "power_user": 3},
    "travel": {"looker": 35, "searcher": 25, "booker": 12, "booking_abandoner": 15, "itinerary": 10, "power_user": 3},
}
# Mbps down, RTT ms. These are conditions, not a radio-technology detector.
NETWORKS = {
    "fiber": (150, 18), "cable": (25, 28), "wifi": (30, 25), "5g": (40, 55),
    "4g": (9, 85), "4g-weak": (1.6, 150),
    "fast-4g": (9, 85), "slow-4g": (1.6, 150), "fast-3g": (1.5, 300), "slow-3g": (.5, 400),
}
NETWORK_MIX = {
    "desktop": {"cable": 55, "wifi": 35, "slow-4g": 8, "fast-3g": 2},
    "mobile": {"wifi": 45, "fast-4g": 25, "slow-4g": 20, "fast-3g": 8, "slow-3g": 2},
}
# Country, locale, timezone, relative weight. Metadata never changes the egress IP.
GEOS = {
    "us-west": ("US", "en-US", "America/Los_Angeles", 20),
    "us-east": ("US", "en-US", "America/New_York", 20),
    "us-central": ("US", "en-US", "America/Chicago", 10),
    "uk": ("GB", "en-GB", "Europe/London", 10),
    "germany": ("DE", "de-DE", "Europe/Berlin", 8),
    "india": ("IN", "en-IN", "Asia/Kolkata", 10),
    "japan": ("JP", "ja-JP", "Asia/Tokyo", 5),
    "brazil": ("BR", "pt-BR", "America/Sao_Paulo", 7),
    "australia": ("AU", "en-AU", "Australia/Sydney", 5),
    "south-africa": ("ZA", "en-ZA", "Africa/Johannesburg", 3),
    "nigeria": ("NG", "en-NG", "Africa/Lagos", 2),
}


def enabled(pop):
    return pop.get("model") == "varied"


def visit(persona, pop, platform, rng):
    """Configure one visit without changing a returning visitor's hardware."""
    if not enabled(pop):
        persona.pop("population_model", None)
        return
    from .personas import DEVICE_TIERS
    p = persona
    p["population_model"] = VERSION
    if "visitor_locale" not in p:
        mix = pop.get("geo_mix") or {k: v[3] for k, v in GEOS.items()}
        country, locale, zone, _ = GEOS[weighted(list(mix.items()), rng)]
        p.update(visitor_country=country, visitor_locale=locale, timezone=zone)
    if "cpu_factor" not in p:
        tier = DEVICE_TIERS.get(p.get("model"))
        if tier is None:
            tier = weighted(list(({"flagship": 60, "mid": 35, "budget": 5} if p["kind"] == "desktop"
                                  else {"flagship": 35, "mid": 45, "budget": 20}).items()), rng)
        p.update(hardware_tier=tier, cpu_factor={"flagship": .85, "mid": 1.15, "budget": 1.9}[tier])
    # Explicit web connection overrides take precedence over the shared mix.
    mix = (pop.get("web", {}).get("connection_mix") if platform == "web" else None) or pop.get("network_mix")
    mix = mix or NETWORK_MIX["desktop" if p["kind"] == "desktop" else "mobile"]
    connection = weighted(list(mix.items()), rng)
    speed, rtt = NETWORKS[connection]
    p.update(connection=connection, network="wifi" if connection in ("fiber", "cable", "wifi") else "wan",
             mbps=round(speed * rng.triangular(.7, 1.3, 1), 2), rtt=round(rtt * rng.triangular(.8, 1.25, 1), 1),
             effective_type="2g" if connection == "slow-3g" else "3g" if connection in ("fast-3g", "4g-weak") else "4g")


def shape(lob, pop, rng):
    mix = pop.get("intent_mix", {}).get(lob) or INTENTS[lob]
    intent = weighted(list(mix.items()), rng)
    s = dict(intent=intent, priority=False, short=False, add=False, statement=False, hotel=False, offer=False,
             bounce=False, overview_only=False, abandon=False, visits=1, power=False)
    if lob == "retail":
        s.update(priority=intent in ("buyer", "power_user"), add=intent == "cart_abandoner",
                 bounce=intent == "bouncer", search=intent == "searcher", category=False, visits=rng.randint(5, 7) if intent == "power_user" else
                 rng.randint(2, 4) if intent == "browser" else rng.randint(1, 2), power=intent == "power_user")
    elif lob == "banking":
        s.update(priority=intent in ("transfer", "power_user"), overview_only=intent == "quick_check",
                 abandon=intent == "transfer_abandoner", statement=intent == "statement", power=intent == "power_user")
    else:
        s.update(priority=intent in ("booker", "power_user"), short=intent == "itinerary",
                 abandon=intent == "booking_abandoner", offer=intent in ("looker", "booker", "booking_abandoner", "power_user"),
                 hotel=intent == "searcher" and rng.random() < .4, power=intent == "power_user")
    return s


def configure_session(s, pop, shape=None):
    if not enabled(pop):
        return
    p = s.persona
    s.population_settings = pop
    s.properties.update(visit_intent=(shape or {}).get("intent", "selected_scenario"),
                        journey_steered=bool((shape or {}).get("steered")),
                        visitor_type="returning" if p.get("returning_visit") else "new",
                        network_profile=p["connection"], hardware_tier=p["hardware_tier"],
                        visitor_locale=p["visitor_locale"], visitor_timezone=p["timezone"],
                        market=p["visitor_country"])
    if p.get("platform") in ("android", "ios"):
        cold = (not p.get("native_process_id") or p.get("native_process_version") != s.app_version
                or s.rng.random() < pop.get("native_cold_start_share", .35))
        cold |= bool(getattr(s.plan, "slow_startup", None))
        s.forced_cold_start = cold
        if cold:
            p.update(native_process_id=s.process_id, native_process_start_ms=s.start_ms, native_process_version=s.app_version)
        else:
            s.process_id = p["native_process_id"]
        s.resource = s._resource()


def read(s, lo=3, hi=15):
    """A bounded skewed reading pause; network retry/deadline waits stay separate."""
    s.t += s.rng.triangular(lo, hi, lo + (hi - lo) * .3) * 1000


def proportions(values):
    counts = Counter(values)
    total = sum(counts.values())
    return [{"value": k, "count": n, "percent": round(n * 100 / total, 2)} for k, n in sorted(counts.items())] if total else []
