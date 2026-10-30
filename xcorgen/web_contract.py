"""Web SDK 2.31 invariants beyond the checked-in capture's exercised paths.

Additional keys below are from constants.ts and instrumentations in SDK 2.31:
DOMState, Loaf, UserTiming, ElementTiming, ServerTiming and SoftNavigationPerformance.
Keep this allow-list explicit: an arbitrary emb.* prefix is not an SDK contract.
"""
import json
from .web_observation import attrs, records

COMMON = {k: "stringValue" for k in ("app.surface.name", "app.surface.id", "app.surface.label", "browser.url.full", "user.id")}
COMMON["emb.perf_clock_lag_ms"] = "number"
EXTRA = {
    "DocumentLoadInstrumentation": {"http.response_content_length_uncompressed": "number",
        "http.response.cors_opaque": "boolValue", "http.response.cache_revalidated": "boolValue",
        "http.response.delivery_type": "stringValue", "browser.navigation_timing.not_restored_reasons": "arrayValue"},
    "WebVitalsInstrumentation": {"browser.navigation_timing.not_restored_reasons": "arrayValue",
        "emb.web_vital.attribution.clsLayoutShifts": "stringValue",
        "browser.web_vital.interaction_id": "number", "emb.web_vital.attribution.loaf_scripts": "stringValue",
        "emb.web_vital.attribution.element_type": "stringValue"},
    "DOMStateInstrumentation": {"emb.type": "stringValue", **{f"dom_state.{k}": "number" for k in (
        "average_depth", "document_height", "document_width", "element_count", "images_above_fold.count",
        "images_above_fold.viewport_height", "images_above_fold.viewport_width", "images_above_fold.timestamp",
        "images_above_fold.capture_time")}, "dom_state.traversal_limit_reached": "boolValue"},
    "LoafInstrumentation": {"emb.type": "stringValue", "browser.web_vital.id": "stringValue",
        "browser.web_vital.name": "stringValue", "browser.web_vital.value": "number", "browser.web_vital.rating": "stringValue",
        **{f"emb.tbd.loaf_{k}": "number" for k in ("total_duration", "work_duration", "style_and_layout_duration",
            "count", "longest_duration", "longest_duration_excluding_first")}},
    "ElementTimingInstrumentation": {"emb.type": "stringValue", **{f"emb.element_timing.{k}": "stringValue"
        for k in ("identifier", "element", "url")}, **{f"emb.element_timing.{k}": "number" for k in
        ("render_time", "load_time", "start_time", "natural_width", "natural_height")}},
    "ServerTimingInstrumentation": {"emb.type": "stringValue", "emb.server_timing.name": "stringValue",
        "emb.server_timing.description": "stringValue", "emb.server_timing.duration": "number"},
    "UserTimingInstrumentation": {"emb.type": "stringValue", "emb.instrumentation": "stringValue",
        **{f"emb.user_timing.{k}": "stringValue" for k in ("name", "entry_type", "detail")},
        "emb.user_timing.start_time": "number", "emb.user_timing.duration": "number"},
    "SoftNavigationPerformanceInstrumentation": {**{f"emb.soft_navigation.{k}": "number" for k in (
        "navigation_id", "interaction_id", "start_time", "duration", "paint_time", "presentation_time")},
        "emb.soft_navigation.source": "stringValue", **{f"emb.soft_navigation.{k}": "arrayValue" for k in
        ("span_ids", "span_id_types", "log_ids", "log_id_types")}},
    "RageClickInstrumentation": {"emb.type": "stringValue", **{f"rage_click.{k}": "number" for k in ("x", "y", "count")},
        **{f"rage_click.{k}": "stringValue" for k in ("element_type", "element_selector", "interaction_type")}},
}
LOG_COMMON = {k: "stringValue" for k in ("log.record.uid", "emb.user_session_id", "emb.user_session_previous_id",
                                       "emb.session_part_id", "url.full")}
LOG_COMMON["emb.is_tab_engaged"] = "boolValue"


def compatible(actual, expected):
    if isinstance(expected, set):
        return any(compatible(actual, x) for x in expected)
    return actual == expected or actual in ("intValue", "doubleValue") and expected in ("intValue", "doubleValue", "number")


def check(payload, problem):
    """Per-payload lifecycle, browser gates, parentage, attribution and integer timing."""
    spans, logs = list(records(payload, "spans")), list(records(payload, "logs"))
    resources = payload.get("resourceSpans", []) + payload.get("resourceLogs", [])
    if not resources:
        problem("empty browser export")
        return
    r = attrs(resources[0]["resource"])
    ua = r.get("user_agent.original", "")
    chromium = "Chrome/" in ua
    webkit = not chromium and "Safari/" in ua
    if not chromium and any(k in r for k in ("device_memory", "network_effective_type")):
        problem("non-Chromium resource has Chromium device information")
    if spans and sum(attrs(s).get("emb.type") == "ux.session_part" for s in spans) != 1:
        problem("web spans payload needs exactly one session part")
    ids = {s["spanId"]: s for s in spans}
    for s in spans:
        a = attrs(s)
        if int(s["endTimeUnixNano"]) < int(s["startTimeUnixNano"]):
            problem("browser span ends before it starts")
        if a.get("emb.type") == "ux.resource_fetch":
            parent = ids.get(s.get("parentSpanId"))
            if not parent or attrs(parent).get("emb.type") != "ux.document_load":
                problem("resource parent documentLoad is absent from this payload")
            if webkit and any(k in a for k in ("http.response.status_code", "http.request.render_blocking_status")):
                problem("WebKit resource contains unsupported status/render-blocking fields")
            if not chromium and "http.request.render_blocking_status" in a:
                problem("non-Chromium resource contains render-blocking status")
        if a.get("emb.type") != "ux.session_part" and any(k in a for k in ("emb.session_part_id", "emb.user_session_id")):
            problem("ordinary web span has session IDs")
        if "emb.w3c_traceparent" in a and a.get("emb.type") != "perf.network_request":
            problem("only network requests may propagate trace context")
        if a.get("emb.type") == "ux.session_part":
            for k in ("emb.error_log_count", "emb.unhandled_exceptions_count"):
                if k in a and not a[k]:
                    problem("zero error counters must be omitted")
            if a.get("emb.session_part_end_reason") == "background" and a.get("emb.is_final_session_part"):
                problem("backgrounding must not mark a final user session")
    for log in logs:
        a = attrs(log)
        if not all(k in a for k in ("emb.session_part_id", "emb.user_session_id", "log.record.uid")):
            problem("browser log lacks session association")
        name = a.get("browser.web_vital.name")
        if name:
            from .web_vitals import rating
            if name in ("ttfb", "fcp", "lcp", "inp", "cls", "tbd") and a["browser.web_vital.rating"] != rating(name, a["browser.web_vital.value"]):
                problem("web vital rating disagrees with its value")
        if name in ("cls", "inp", "tbd") and not chromium:
            problem(f"unsupported {name} emitted by non-Chromium browser")
        if name == "tbd":
            if "emb.was_from_inp" in a or "browser.web_vital.delta" in a or log.get("body") != {}:
                problem("TBD is a Loaf report with no delta, INP flag or body")
        if name in ("lcp", "inp", "fcp"):
            try:
                body = json.loads(log["body"]["stringValue"])
                keys = {"lcp": ("timeToFirstByte", "resourceLoadDelay", "resourceLoadDuration", "elementRenderDelay"),
                        "inp": ("inputDelay", "processingDuration", "presentationDelay"),
                        "fcp": ("timeToFirstByte", "firstByteToFCP")}[name]
                if abs(sum(body[k] for k in keys) - a["browser.web_vital.value"]) > 3:
                    problem(f"{name} attribution does not add up")
            except (ValueError, KeyError, TypeError):
                problem(f"invalid {name} attribution body")
        if a.get("emb.type") == "sys.exception":
            stack = a["exception.stacktrace"]
            if any(x in stack for x in ("java.lang.", ".kt:", "android.os.")):
                problem("browser exception contains Android frames")
            if not chromium and stack and "    at " in stack:
                problem("WebKit/Gecko stack uses Chromium frame syntax")
    if not chromium:
        for item in spans + logs:
            for k in ("timeUnixNano", "observedTimeUnixNano", "startTimeUnixNano", "endTimeUnixNano"):
                if k in item and int(item[k]) % 1000000:
                    problem("WebKit/Gecko timestamp has fractional milliseconds")
