"""Target-driven web vitals with internally consistent attribution.

Each page load draws one latent "speed" value shared by TTFB, FCP and LCP (and a
correlated one for INP), so a slow load is slow everywhere. Timings are log-normal
with the 75th percentile on the requested target; CLS is zero-inflated (most loads
have no shift) with its overall 75th percentile on target. Attribution always adds up
(LCP = TTFB + resource load delay + resource load duration + element render delay;
INP = input delay + processing + presentation delay; TTFB = waiting + cache + DNS +
connection + request), and every rating matches the dashboard thresholds.
"""
import math
from statistics import NormalDist

THRESHOLDS = {"ttfb": (800, 1800), "fcp": (1800, 3000), "lcp": (2500, 4000), "inp": (200, 500),
              "cls": (0.1, 0.25), "tbd": (200, 600)}
Z75 = NormalDist().inv_cdf(.75)
SIGMA = {"ttfb": .6, "fcp": .45, "lcp": .45, "inp": .65}
CLS_ZERO_SHARE, CLS_SIGMA = .45, .8

# Healthy baselines (p75, milliseconds; CLS unitless) by device type, then per page group.
BASELINE = {
    "desktop": {"ttfb": 380, "fcp": 900, "lcp": 1650, "inp": 90, "cls": .03},
    "tablet": {"ttfb": 470, "fcp": 1150, "lcp": 1950, "inp": 130, "cls": .05},
    "mobile": {"ttfb": 560, "fcp": 1380, "lcp": 2250, "inp": 160, "cls": .06},
}
PAGE_FACTOR = {"Home": {"lcp": 1.1}, "Product": {"lcp": 1.15, "cls": 1.2}, "Search": {"lcp": 1.05, "cls": 1.3},
               "Category": {"cls": 1.4}, "Checkout": {"inp": 1.2}, "Account": {"lcp": .9}}
TBD_MIX = {"good": .82, "needs-improvement": .13, "poor": .05}


def rating(name, value):
    good, poor = THRESHOLDS[name]
    return "good" if value <= good else "needs-improvement" if value <= poor else "poor"


def targets(spec_entries, page_group, device_type, browser_family, persona=None):
    """p75 targets and TBD rating mix for one page view, with spec entries overriding the baseline."""
    t = dict(BASELINE[device_type])
    for k, f in PAGE_FACTOR.get(page_group, {}).items():
        t[k] *= f
    if persona and persona.get("population_model"):
        # Add transport cost to load metrics, and hardware cost to synchronous
        # rendering/interaction work. Slow HTTP alone never becomes poor INP.
        net = max(0, 2 * (persona["rtt"] - 35))
        transfer = max(0, 160000 * 8 / (persona["mbps"] * 1000) - 160000 * 8 / 30000)
        cpu = persona["cpu_factor"]
        t["ttfb"] += net
        t["fcp"] += net + transfer * .2 + max(0, cpu - 1) * 250
        t["lcp"] += net + transfer + max(0, cpu - 1) * 450
        t["inp"] *= cpu
    mix = dict(TBD_MIX)
    for entry in spec_entries or []:
        if entry.get("page_groups") and page_group not in entry["page_groups"]:
            continue
        if entry.get("device_types") and device_type not in entry["device_types"]:
            continue
        if entry.get("browser_families") and browser_family not in entry["browser_families"]:
            continue
        t.update(entry.get("targets", {}))
        mix.update(entry.get("tbd_rating_mix", {}))
    return t, mix


def timing(p75, sigma, z):
    return p75 * math.exp(sigma * (z - Z75))


def cls_value(p75, rng):
    if p75 <= 0 or rng.random() < CLS_ZERO_SHARE:
        return 0.0
    q = (.75 - CLS_ZERO_SHARE) / (1 - CLS_ZERO_SHARE)
    median = p75 / math.exp(CLS_SIGMA * NormalDist().inv_cdf(q))
    return median * math.exp(CLS_SIGMA * rng.gauss(0, 1))


def precision(value, fractional):
    """Chromium reports fractional milliseconds; Firefox and WebKit whole milliseconds (sent as integers)."""
    if fractional:
        v = round(value, 1)
        return int(v) if v == int(v) else v
    return int(round(value))


class PageVitals:
    """Draws the vitals of one page load (or one soft navigation) against its targets."""

    def __init__(self, t, rng, fractional=True):
        self.t, self.rng, self.fractional = t, rng, fractional
        z = rng.gauss(0, 1)
        mix = lambda rho: rho * z + math.sqrt(1 - rho * rho) * rng.gauss(0, 1)
        self.ttfb = timing(t["ttfb"], SIGMA["ttfb"], mix(.85))
        self.fcp = max(timing(t["fcp"], SIGMA["fcp"], mix(.9)), self.ttfb + rng.uniform(30, 140))
        self.lcp = max(timing(t["lcp"], SIGMA["lcp"], mix(.9)), self.fcp)
        self.inp = timing(t["inp"], SIGMA["inp"], mix(.4))
        self.cls = cls_value(t["cls"], rng)

    def ttfb_parts(self, dns, tcp, tls):
        """TTFB body and integer attributes; connection phases come from the document fetch."""
        r = self.rng
        waiting = r.uniform(.1, 3)
        cache = r.uniform(.5, 4)
        request = max(1.0, self.ttfb - waiting - cache - dns - tcp - tls)
        self.ttfb = waiting + cache + dns + tcp + tls + request
        p = lambda v: precision(v, self.fractional)
        body = {"waitingDuration": p(waiting), "cacheDuration": p(cache), "dnsDuration": p(dns),
                "connectionDuration": p(tcp + tls), "requestDuration": p(request)}
        server = round(request)
        attrs = {"redirect": 0, "domainLookup": round(dns), "tcpConnection": round(tcp), "tlsNegotiation": round(tls),
                 "serverResponse": server}
        attrs["unattributed"] = max(0, round(self.ttfb) - sum(attrs.values()))
        return body, attrs, {"fetch": waiting, "cache": cache, "request": request}

    def lcp_parts(self, image):
        r, rest = self.rng, self.lcp - self.ttfb
        if image:
            delay = rest * r.uniform(.05, .25)
            duration = rest * r.uniform(.35, .65)
        else:
            delay = duration = 0.0
        render = max(0.0, rest - delay - duration)
        p = lambda v: precision(v, self.fractional)
        return {"timeToFirstByte": p(self.ttfb), "resourceLoadDelay": p(delay), "resourceLoadDuration": p(duration),
                "elementRenderDelay": p(render)}

    def inp_parts(self, slow_handler=0.0):
        r = self.rng
        value = self.inp + slow_handler
        delay = value * r.uniform(.05, .2)
        processing = value * r.uniform(.35, .6)
        presentation = value - delay - processing
        p = lambda v: precision(v, self.fractional)
        return value, {"inputDelay": p(delay), "processingDuration": p(processing), "presentationDelay": p(presentation)}


def value(v, fractional):
    """The number a vital reports as value and delta."""
    return precision(v, fractional)
