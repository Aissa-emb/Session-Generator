"""Semantic checks shared by CLI, HTTP, MCP and the studio."""
from .web_capabilities import MATRIX, unsupported
from .web_faults import FAMILIES
from .web_sites import SITES


def validate(spec, issue, now_ms):
    from .spec import parse_time, traffic_window
    out = []
    groups = {p.group for site in SITES.values() for p in site.values()}
    routes = {p.route for site in SITES.values() for p in site.values()}
    start, _ = traffic_window(spec, now_ms)
    for i, target in enumerate(spec["targets"]):
        if target["platform"] != "web":
            continue
        path, web = f"$.targets[{i}].web", target["web"]
        releases = web.get("releases", [])
        if releases and abs(sum(r["share"] for r in releases) - 1) > .01:
            out.append(issue("error", path + ".releases", "Release shares must sum to 1.", "shares"))
        if releases and all(r.get("deployed") and parse_time(r["deployed"]) > start for r in releases):
            out.append(issue("error", path + ".releases", "No release was deployed at the start of the traffic window.", "release"))
        for lob, weight in spec["population"]["line_of_business_mix"].items():
            if weight and lob not in web["estate"]:
                out.append(issue("error", path + ".estate", f"The web estate needs a {lob} site.", "estate"))
        if spec["destinations"]["mode"] == "end_to_end":
            missing = set(target["api_domains"].values()) - set(web["propagation_allow_list"])
            if missing:
                out.append(issue("error", path + ".propagation_allow_list", "Trace propagation needs every API domain: " + ", ".join(sorted(missing)), "propagation"))
    entries = [(f"$.web_performance[{i}]", e) for i, e in enumerate(spec["web_performance"])]
    entries += [(f"$.client_faults[{i}].filters", f.get("filters", {})) for i, f in enumerate(spec["client_faults"])
               if f.get("platform") == "web"]
    for path, entry in entries:
        for dim, known in (("page_groups", groups), ("routes", routes)):
            missing = set(entry.get(dim, [])) - known
            if missing:
                out.append(issue("error", path + "." + dim, "Unknown values: " + ", ".join(sorted(missing)), "filter_empty"))
        families = entry.get("browser_families") or list(MATRIX)
        for metric in entry.get("targets", {}):
            bad = unsupported(metric, families)
            if bad:
                severity = "error" if len(bad) == len(families) else "warning"
                out.append(issue(severity, path + ".targets." + metric,
                    f"{', '.join(bad)} does not report {metric.upper()}; this target applies only to supported browsers.", "capability"))
        mix = entry.get("tbd_rating_mix")
        if mix and abs(sum(mix.values()) - 1) > .01:
            out.append(issue("error", path + ".tbd_rating_mix", "TBD rating shares must sum to 1.", "shares"))
        t = entry.get("targets", {})
        if ("fcp" in t and "lcp" in t and t["fcp"] > t["lcp"]) or ("ttfb" in t and "fcp" in t and t["ttfb"] > t["fcp"]):
            out.append(issue("error", path + ".targets", "Timing targets require TTFB ≤ FCP ≤ LCP.", "timing"))
    for i, f in enumerate(spec["client_faults"]):
        if f.get("platform") != "web":
            continue
        d, path = f.get("details", {}), f"$.client_faults[{i}].details"
        if f["type"] in ("js_exception", "chunk_load_failures"):
            if d.get("family", "property_undefined") not in FAMILIES:
                out.append(issue("error", path + ".family", "Choose a JavaScript exception family from the catalog.", "family"))
            if d.get("handling", "unhandled_error") not in ("handled", "unhandled_error", "unhandled_rejection"):
                out.append(issue("error", path + ".handling", "Handling must be handled, unhandled_error or unhandled_rejection.", "handling"))
        metric = {"layout_instability": "cls", "interaction_latency": "inp", "long_task": "tbd", "third_party_blocking": "tbd"}.get(f["type"])
        families = f.get("filters", {}).get("browser_families", [])
        if metric and families and len(unsupported(metric, families)) == len(families):
            out.append(issue("error", path, f"The selected browsers do not report {metric.upper()}.", "capability"))
    return out
