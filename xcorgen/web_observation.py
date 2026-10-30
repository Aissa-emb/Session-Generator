"""Measure browser output without consulting assignments or session objects."""
import json
import re
from urllib.parse import urlsplit

from .web_sites import PAGE_GROUP_RULES


def attrs(item):
    return {a["key"]: next(iter(a["value"].values()), None) for a in item.get("attributes", [])}


def records(payload, kind):
    for rs in payload.get("resourceSpans" if kind == "spans" else "resourceLogs", []):
        for scope in rs.get("scopeSpans" if kind == "spans" else "scopeLogs", []):
            yield from scope.get("spans" if kind == "spans" else "logRecords", [])


def observe(payloads, traces):
    from .emitted import endpoint_of
    obs = {"platform": "web", "net": [], "crash": False, "handled": set(), "blockage": False,
           "cold_start_ms": None, "roots": {}, "signed_in": False, "traces": False, "breadcrumbs": set(),
           "logs": [], "tier": "mid", "vitals": [], "exceptions": [], "pages": set(), "abandoned": False,
           "rage_clicks": 0, "soft_navigations": []}
    for p in payloads:
        for rs in p.get("resourceSpans", []) + p.get("resourceLogs", []):
            r = attrs(rs["resource"])
            ua = r.get("user_agent.original", "")
            browser = "edge" if "Edg/" in ua else "samsung_internet" if "SamsungBrowser" in ua else "firefox" if "Firefox" in ua else "chrome" if "Chrome/" in ua else "safari"
            device = "tablet" if "iPad" in ua else "mobile" if "Mobile" in ua else "desktop"
            match = re.search({"edge": r"Edg/([\d.]+)", "samsung_internet": r"SamsungBrowser/([\d.]+)",
                               "firefox": r"Firefox/([\d.]+)", "chrome": r"Chrome/([\d.]+)", "safari": r"Version/([\d.]+)"}[browser], ua)
            version = match[1] if match else ""
            obs.update(browser=browser, device_type=device, app_version=r.get("app_version"),
                       connection=r.get("network_effective_type"), ring="stable", browser_version=version)
        for s in records(p, "spans"):
            a = attrs(s)
            ms = (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6
            if a.get("emb.type") == "ux.session_part":
                obs.update(lob=a.get("emb.properties.business_unit"), session_id=a["emb.user_session_id"],
                           start_ms=float(a["emb.user_session_start_ts"]), signed_in=bool(a.get("user.id")))
                obs["abandoned"] |= a.get("emb.page_load") is False
                obs["breadcrumbs"].update(attrs(e).get("message") for e in s.get("events", []) if e["name"] == "emb-breadcrumb")
                obs["pages"].add(a.get("app.surface.name"))
            elif a.get("emb.type") == "perf.network_request" and (a.get("server.address") or "").startswith("api.vantra"):
                obs["traces"] |= "emb.w3c_traceparent" in a
                obs["net"].append({"method": a["http.request.method"], "path": urlsplit(a["url.full"]).path,
                    "status": int(a["http.response.status_code"]) or None, "error_type": a.get("error.type"),
                    "ms": ms, "trace_id": s["traceId"], "start_ms": int(s["startTimeUnixNano"]) / 1e6})
            elif a.get("app.operation"):
                obs["roots"].setdefault(s["name"], []).append(ms)
            elif s["name"] == "Soft Navigation":
                obs["soft_navigations"].append({"ms": ms, "source": a["emb.soft_navigation.source"]})
        for log in records(p, "logs"):
            a = attrs(log)
            if a.get("emb.type") == "ux.web_vital":
                obs["vitals"].append({"name": a["browser.web_vital.name"], "value": a["browser.web_vital.value"],
                    "rating": a["browser.web_vital.rating"], "surface": a.get("app.surface.name"),
                    "label": a.get("app.surface.label"), "url": a.get("browser.url.full"),
                    "time_ms": int(log["timeUnixNano"]) / 1e6})
            elif a.get("emb.type") == "sys.exception":
                obs["exceptions"].append({"type": a["exception.type"], "name": a["exception.name"],
                    "message": a["exception.message"], "stack": a["exception.stacktrace"], "handling": a["emb.exception_handling"]})
                if a["emb.exception_handling"] == "handled":
                    obs["handled"].add(a["exception.type"])
            elif a.get("emb.type") == "sys.log":
                obs["logs"].append(log.get("body", {}).get("stringValue", ""))
            elif log.get("eventName") == "rage-click":
                obs["rage_clicks"] += 1
    for n in obs["net"]:
        n["endpoint"] = endpoint_of(obs.get("lob"), n["method"], n["path"])
        n["spans"] = traces.get(n["trace_id"], [])
    obs["endpoints"] = {n["endpoint"] for n in obs["net"]}
    from .web_sites import SITES
    route_groups = {p.route: p.group for p in SITES.get(obs.get("lob"), {}).values()}
    for v in obs["vitals"]:
        v["page_group"] = route_groups.get(v["surface"], "Unknown")
    return obs


FAMILY_PATTERNS = {"property_undefined": r"reading 'price'|evaluating 't.price'|t is undefined",
    "not_a_function": r"not a function", "chunk_load": r"module script failed|dynamically imported module",
    "webpack_chunk_load": r"Loading chunk", "json_html": r"Unexpected token|JSON Parse error|JSON.parse:",
    "failed_to_fetch": r"Failed to fetch$|Load failed|NetworkError", "abort": r"aborted",
    "quota_exceeded": r"quota", "hydration": r"React error #418", "script_error": r"^Script error\.$"}


def affected(fault, obs):
    t, d = fault.type, fault.details
    if t in ("js_exception", "chunk_load_failures"):
        family = d.get("family", "chunk_load" if t == "chunk_load_failures" else "property_undefined")
        pattern = FAMILY_PATTERNS[family]
        if t == "chunk_load_failures":
            pattern += "|Loading chunk"
        return any(re.search(pattern, e["message"], re.I) and
                   (not d.get("handling") or d["handling"] == e["handling"]) for e in obs["exceptions"])
    if t == "page_abandonment":
        return obs["abandoned"]
    if t == "rage_clicks":
        return obs["rage_clicks"] > 0
    metric = {"lcp_regression": "lcp", "layout_instability": "cls", "interaction_latency": "inp",
              "slow_server_response": "ttfb", "soft_navigation_slowness": "lcp", "long_task": "tbd",
              "third_party_blocking": "tbd"}.get(t)
    if metric:
        return any(v["name"] == metric and v["rating"] != "good" for v in obs["vitals"])
    if t == "poor_web_vitals":
        return any(v["name"] in d.get("targets", {"lcp": 4000}) and v["rating"] != "good" for v in obs["vitals"])
    return False
