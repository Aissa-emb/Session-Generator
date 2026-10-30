"""Browser capability matrix: which signals a simulated browser can produce.

The SDK feature-detects every signal (PerformanceObserver.supportedEntryTypes, the
Navigation API, PerformanceEventTiming.interactionId); this table maps those tests to
browser engines and versions. Cells the build prompt marks "verify" (INP and the
soft-navigation polyfill outside Chromium) stay off until a golden payload or the
web-vitals support table proves them; nothing is emitted that the browser would not send.

Sources: SDK feature tests (WebVitalsInstrumentation, LoafInstrumentation,
ElementTimingInstrumentation, SoftNavigationPerformanceInstrumentation, EmbracePageManager),
per-engine goldens in reference/web-sdk-golden/ (Firefox and WebKit report TTFB, FCP and
LCP but no CLS, device_memory or network_effective_type; WebKit resource timing has no
response status or render-blocking status), and docs/v2/V2-BUILD-PROMPT.md section 7.4.
"""


def _ver(v):
    parts = [int(x) for x in str(v).split(".")[:2]] + [0]
    return parts[0] + parts[1] / 100


def capabilities(persona):
    """Capability flags for a browser persona (personas.BROWSERS fields)."""
    engine = persona.get("engine") or ("chromium" if persona.get("chromium") else "webkit")
    family = persona.get("browser")
    v = _ver(persona.get("browser_version") or 0)
    chromium = persona.get("chromium_version") or 0
    c = {
        "engine": engine,
        "ttfb": True, "fcp": True, "lcp": True,
        "cls": engine == "chromium",
        "inp": engine == "chromium",
        "loaf": engine == "chromium" and chromium >= 123,
        "element_timing": engine == "chromium",
        "event_timing": engine in ("chromium", "gecko"),  # the "event" entry type; INP still needs interactionId
        "device_info": engine == "chromium",           # device_memory, network_effective_type resource attributes
        "render_blocking_status": engine == "chromium",
        "delivery_type": engine == "chromium",
        "resource_status": engine in ("chromium", "gecko"),
        "not_restored_reasons": engine == "chromium",
        "response_start_event": True,
        "navigation_api": (engine == "chromium" and chromium >= 102) or (family == "safari" and v >= 26.02)
                          or (engine == "gecko" and v >= 147),
        "native_soft_navigation": engine == "chromium" and chromium >= 151,
        "fractional_time": engine == "chromium",
        "bfcache": True,
    }
    c["soft_navigation_polyfill"] = c["navigation_api"] and c["event_timing"] and not c["native_soft_navigation"]
    c["soft_navigation_vitals"] = c["native_soft_navigation"]
    return c


# For spec validation and the UI: what each browser family reports, by name.
MATRIX = {
    "chrome": {"cls": True, "inp": True, "loaf": True, "soft_navigation": "151+", "navigation_api": "102+"},
    "edge": {"cls": True, "inp": True, "loaf": True, "soft_navigation": "151+", "navigation_api": "102+"},
    "samsung_internet": {"cls": True, "inp": True, "loaf": True, "soft_navigation": "Chromium 151+", "navigation_api": "yes"},
    "safari": {"cls": False, "inp": False, "loaf": False, "soft_navigation": False, "navigation_api": "26.2+"},
    "firefox": {"cls": False, "inp": False, "loaf": False, "soft_navigation": False, "navigation_api": "147+"},
}
CHROMIUM_FAMILIES = ("chrome", "edge", "samsung_internet")


def unsupported(metric, families):
    """Browser families in `families` that never report `metric` (cls, inp, tbd, soft navigation)."""
    key = {"tbd": "loaf"}.get(metric, metric)
    if key in ("ttfb", "fcp", "lcp"):
        return []
    return [f for f in families if not MATRIX[f].get(key)]
