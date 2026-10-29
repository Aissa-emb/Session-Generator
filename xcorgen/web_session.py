"""Simulated browser session that produces Embrace Web SDK payloads.

Payload shapes, scope names, attribute keys and value types mirror what
@embrace-io/web-sdk 2.31.0 sends (see reference/web-sdk-2.31.0-capture).
Every API call gets a W3C traceparent; the matching backend trace is built
with the same trace ID and with the browser span as the parent of the
backend root span, so Embrace's network span forwarding completes the trace
in Chronosphere.
"""

import json
import random
from contextlib import contextmanager

from . import otlp
from .calls import Result, deliver_backend, release_backend, simulate
from .util import emb_id, hex_id, lat, ns, weighted

SDK_SCOPES = {
    "part": {"name": "embrace-web-sdk-session-parts"},
    "nav": {"name": "NavigationInstrumentation", "version": "1.0.0"},
    "doc": {"name": "DocumentLoadInstrumentation", "version": "1.0.0"},
    "fetch": {"name": "@opentelemetry/instrumentation-fetch", "version": "0.223.0"},
    "traces": {"name": "embrace-web-sdk-traces"},
    "logs": {"name": "embrace-web-sdk-logs"},
    "vitals": {"name": "WebVitalsInstrumentation", "version": "1.0.0"},
    "first": {"name": "FirstInteractionInstrumentation", "version": "1.0.0"},
    "scroll": {"name": "MaxScrollDepthInstrumentation", "version": "1.0.0"},
}

VITAL_THRESHOLDS = {"ttfb": (800, 1800), "fcp": (1800, 3000), "lcp": (2500, 4000), "inp": (200, 500), "cls": (0.1, 0.25)}
LOG_BATCH_MS = 5000  # BatchLogRecordProcessor scheduled delay


def rating(name, v):
    good, ni = VITAL_THRESHOLDS[name]
    return "good" if v <= good else "needs-improvement" if v <= ni else "poor"


class Part:
    def __init__(self, number, index, start, reason):
        self.id = emb_id()
        self.number, self.index, self.start, self.start_reason = number, index, start, reason
        self.end = None
        self.end_reason = None
        self.events, self.spans, self.logs = [], {}, []
        self.error_logs = self.unhandled = 0
        self.final = None  # termination reason when the user session ends in this part
        self.trace_id = hex_id(16)
        self.span_id = hex_id(8)


class WebSession:
    def __init__(self, profile, persona, app_id, start_ms, incident=None, emit_client_spans=False, rng=random):
        self.profile, self.persona, self.app_id, self.rng = profile, persona, app_id, rng
        self.incident, self.emit_client_spans = incident, emit_client_spans
        self.t = start_ms
        self.start_ms = start_ms
        self.user_session_id = emb_id()
        self.previous_user_session_id = persona["previous_user_session_id"]
        self.user_session_number = persona["user_session_number"] + 1
        self.part_number = persona["session_part_number"]
        self.app_version, self.sdk_version = weighted([((v, s), w) for v, s, w in profile.app_versions], rng)
        self.user_id = None
        self.properties = profile.properties(persona, rng)
        self.connected_hosts = set()
        self.exception_number = 0
        self.part = None
        self.page = None
        self.deliveries = []  # dicts: due, target, kind, payload
        self.calls = []       # (endpoint key, status, trace_id) for the run summary
        self.clicked_since_load = False
        self.page_clicks = []
        self.ended = False
        self.navigation_id = rng.randint(3000, 9000)
        self.resource = self._resource()
        self.api_host = profile.api_host

    # --- identity -------------------------------------------------------------

    def _resource(self):
        p = self.persona
        a = {
            "telemetry.sdk.name": "embrace-web-sdk", "app_version": self.app_version, "app_framework": 1,
            "sdk_version": self.sdk_version, "telemetry.sdk.version": self.sdk_version, "sdk_simple_version": 1,
            "sdk_platform": "web", "telemetry.sdk.language": "webjs", "emb.app_instance_id": emb_id(),
            "user_agent.original": p["ua"], "screen_resolution": p["screen"], "viewport_resolution": p["viewport"],
        }
        if p["chromium"]:
            a["network_effective_type"] = p["effective_type"]
            a["device_memory"] = p["device_memory"]
        a["webdriver"] = False
        a["service.name"] = "embrace-web-sdk"
        return {"attributes": otlp.attrs(a)}

    def login(self, user_id=None):
        self.user_id = user_id or self.persona["user_id"]

    def _page_attrs(self):
        pg = self.page
        return {"browser.url.full": pg["url"], "user.id": self.user_id, "app.surface.name": pg["template"],
                "app.surface.id": pg["id"], "app.surface.label": pg["label"], "emb.perf_clock_lag_ms": 0}

    def _log_attrs(self):
        pg = self.page
        return {"log.record.uid": emb_id(), "emb.user_session_id": self.user_session_id,
                "emb.user_session_previous_id": self.previous_user_session_id, "emb.session_part_id": self.part.id,
                "browser.url.full": pg["url"], "url.full": pg["url"], "emb.is_tab_engaged": True,
                "user.id": self.user_id, "app.surface.name": pg["template"], "app.surface.id": pg["id"],
                "app.surface.label": pg["label"], "emb.perf_clock_lag_ms": 0}

    # --- time -----------------------------------------------------------------

    def wait(self, lo_s, hi_s):
        self.t += self.rng.uniform(lo_s, hi_s) * 1000

    def _think(self):
        self.t += self.rng.uniform(40, 220)

    # --- navigation -------------------------------------------------------------

    def page_view(self, key, **params):
        pg_def = self.profile.pages[key]
        url = self.profile.web_origin + pg_def.path(params)
        first = self.part is None
        if not first:
            self._end_page()
            self._end_part("web_soft_navigation")
        self.part_number += 1
        index = 1 if first else self.part.index + 1
        self.part = Part(self.part_number, index, self.t, "init" if first else "web_soft_navigation")
        self.page = {"template": pg_def.template, "label": pg_def.label(params), "url": url, "id": emb_id(),
                     "start": self.t, "def": pg_def}
        self.page_clicks = []
        if first:
            self._document_load(pg_def)
        else:
            self.t += lat(60, 260, self.rng)  # route render

    def _end_page(self):
        pg = self.page
        a = {"emb.type": "ux.surface", "app.surface.name": pg["template"], "app.surface.id": pg["id"],
             "browser.url.full": pg["url"], "user.id": self.user_id, "app.surface.label": pg["label"],
             "emb.perf_clock_lag_ms": 0}
        self._span("nav", pg["template"], pg["start"], self.t, a)
        # Per-page vitals the SDK reports when the page is left: CLS always, INP after interactions.
        cls = round(max(0.0, self.rng.gauss(0.03, 0.05) * (2.2 if pg["def"].shifty else 1.0)), 4)
        self._vital("cls", cls, pg["start"] + self.rng.uniform(200, 1200),
                    {"largestShiftTarget": pg["def"].shift_target, "largestShiftTime": round(self.rng.uniform(300, 2500), 1),
                     "largestShiftValue": cls, "loadState": "complete"})
        if self.page_clicks:
            when = self.rng.choice(self.page_clicks)
            slow = 2.4 if self.persona["kind"] != "desktop" else 1.0
            inp = round(lat(64 * slow, 260 * slow, self.rng))
            delay = round(self.rng.uniform(0.5, 6), 1)
            proc = round(inp * self.rng.uniform(0.3, 0.7), 1)
            self._vital("inp", inp, when, {
                "interactionType": "pointer", "interactionTime": round(when - self.start_ms, 1),
                "nextPaintTime": round(when - self.start_ms + inp, 1), "inputDelay": delay,
                "processingDuration": proc, "presentationDelay": round(inp - delay - proc, 1), "loadState": "complete"})
        doc_h = pg["def"].doc_height + self.rng.randint(-200, 400)
        depth = self.rng.choice([0.35, 0.6, 0.8, 1.0, 1.0])
        vh = int(self.persona["viewport"].split("x")[1])
        px = max(0, int((doc_h - vh) * depth))
        self._otel_log("scroll", "max-scroll-depth", {
            "max_scroll_depth.pixels": px, "max_scroll_depth.did_scroll": px > 0,
            "max_scroll_depth.percent": int(100 * (px + vh) / doc_h) if doc_h else 100,
            "max_scroll_depth.document_height": doc_h})

    def _end_part(self, reason, final=None):
        part = self.part
        part.end, part.end_reason, part.final = self.t, reason, final
        pg = self.page
        a = {
            "emb.user_session_id": self.user_session_id,
            "emb.user_session_previous_id": self.previous_user_session_id,
            "emb.user_session_number": self.user_session_number,
            "emb.user_session_part_index": part.index,
            "emb.session_part_number": part.number,
            "emb.user_session_start_ts": round(self.start_ms, 1),
            "emb.user_session_max_duration_seconds": 43200,
            "emb.user_session_inactivity_timeout_seconds": 1800,
            "emb.user_session_foreground_inactivity_timeout_seconds": 1800,
            "emb.type": "ux.session_part", "emb.state": "foreground",
            "emb.session_part_id": part.id,
            "emb.session_part_start_reason": part.start_reason,
            "emb.cold_start": part.index == 1,
        }
        a.update({f"emb.properties.{k}": v for k, v in self.properties.items()})
        a["emb.session_part_end_reason"] = reason
        if part.error_logs:
            a["emb.error_log_count"] = part.error_logs
        if part.unhandled:
            a["emb.unhandled_exceptions_count"] = part.unhandled
        a["emb.sdk_startup_duration"] = self.sdk_startup
        if part.index == 1:
            a["emb.page_load"] = True
        if final:
            a["emb.is_final_session_part"] = 1
            a["emb.user_session_termination_reason"] = final
        a.update({"browser.url.full": pg["url"], "user.id": self.user_id, "app.surface.name": pg["template"],
                  "app.surface.id": pg["id"], "app.surface.label": pg["label"], "emb.perf_clock_lag_ms": 0})
        span = {"traceId": part.trace_id, "spanId": part.span_id, "name": "emb-session-part", "kind": 1,
                "startTimeUnixNano": str(ns(part.start)), "endTimeUnixNano": str(ns(part.end)),
                "attributes": otlp.attrs(a), "droppedAttributesCount": 0,
                "events": sorted(part.events, key=lambda e: int(e["timeUnixNano"])),
                "droppedEventsCount": 0, "status": {"code": 0}, "links": [], "droppedLinksCount": 0, "flags": 257}
        order = ["part", "nav", "doc", "fetch", "traces"]
        part.spans.setdefault("part", []).insert(0, span)
        scope_spans = [{"scope": SDK_SCOPES[k], "spans": part.spans[k]} for k in order if part.spans.get(k)]
        due = part.end + self.rng.uniform(80, 900)
        self._deliver(due, "embrace", "spans", {"resourceSpans": [{"resource": self.resource, "scopeSpans": scope_spans}]})
        release_backend(self, part.end, self.rng)
        self._flush_logs(part)

    def _flush_logs(self, part):
        # The SDK batches logs every few seconds; whatever is pending goes out when the part ends.
        batches = {}
        for t, scope, rec in part.logs:
            due = min(part.end, part.start + (int((t - part.start) // LOG_BATCH_MS) + 1) * LOG_BATCH_MS)
            batches.setdefault(due, []).append((scope, rec))
        for due, items in sorted(batches.items()):
            scopes = {}
            for scope, rec in items:
                scopes.setdefault(scope, []).append(rec)
            payload = {"resourceLogs": [{"resource": self.resource, "scopeLogs": [
                {"scope": SDK_SCOPES[s], "logRecords": recs} for s, recs in scopes.items()]}]}
            self._deliver(due + self.rng.uniform(20, 400), "embrace", "logs", payload)

    def leave(self):
        """Visitor closes the tab or switches away; the last part ends on background."""
        if self.ended:
            return
        self._end_page()
        self._end_part("background")
        self.ended = True

    def logout(self):
        """App calls endUserSession() on sign-out."""
        if self.ended:
            return
        self._end_page()
        self._end_part("user_session_ended", final="manual")
        self.ended = True

    # --- page load ----------------------------------------------------------------

    def _document_load(self, pg_def):
        p, rng = self.persona, self.rng
        mobile = p["kind"] != "desktop"
        rtt = p["rtt"]
        tid = hex_id(16)
        load_id = hex_id(8)
        t0 = self.t
        self.sdk_startup = rng.randint(6, 28) if not mobile else rng.randint(14, 60)
        dns, tcp, tls = lat(6, 30, rng), rtt * rng.uniform(0.9, 1.2), rtt * rng.uniform(1.0, 1.5)
        server = lat(110, 420, rng) * (1.5 if self.incident == "slow-origin" else 1.0)
        html = rng.randint(28_000, 64_000)
        req_start = t0 + rng.uniform(1, 6) + dns + tcp + tls
        resp_start = req_start + rtt + server
        resp_end = resp_start + html * 8 / (p["mbps"] * 1000) + rng.uniform(2, 15)
        page_attrs = self._page_attrs()
        self._span("doc", "documentFetch", t0, resp_end, dict({
            "url.full": self.page["url"], "http.response_content_length": html, "emb.type": "ux.resource_fetch",
            "http.request.initiator_type": "navigation", "http.request.render_blocking_status": "non-blocking",
            "http.response.status_code": 200, "http.response.size": html + 300, "http.response.body.size": html,
            "http.response.decoded_body_size": html * 4, "browser.navigation_timing.type": "navigate"}, **page_attrs),
            trace_id=tid, parent=load_id, events=self._timing_events(t0, dns, tcp, tls, req_start, resp_start, resp_end, True))
        returning = self.user_session_number > 1
        cursor = resp_end + rng.uniform(5, 40)
        last_end = cursor
        for asset in self.profile.static_assets:
            a_start = cursor + rng.uniform(0, 25)
            size = asset["size"]
            cached = returning and rng.random() < 0.85
            dur = rng.uniform(1, 6) if cached else rtt + size * 8 / (p["mbps"] * 1000) + rng.uniform(3, 30)
            a_end = a_start + dur
            last_end = max(last_end, a_end)
            self._span("doc", "resourceFetch", a_start, a_end, dict({
                "url.full": self.profile.cdn + asset["path"], "http.response_content_length": size,
                "emb.type": "ux.resource_fetch", "http.request.initiator_type": asset["initiator"],
                "http.request.render_blocking_status": asset.get("blocking", "non-blocking"),
                "http.response.status_code": 200, "http.response.size": 0 if cached else size + 320,
                "http.response.body.size": size, "http.response.decoded_body_size": int(size * asset.get("ratio", 1.0))},
                **page_attrs), trace_id=tid, parent=load_id,
                events=self._timing_events(a_start, 0, 0, 0, a_start + 0.4, a_end - (0.5 if cached else size * 8 / (p["mbps"] * 1000)), a_end, False))
        cpu = 2.6 if mobile else 1.0
        dom_interactive = last_end + lat(40, 160, rng) * cpu
        dcl_end = dom_interactive + rng.uniform(1, 12) * cpu
        dom_complete = dcl_end + lat(80, 400, rng) * cpu
        load_end = dom_complete + rng.uniform(0.2, 3)
        ev = [("fetchStart", t0), ("domInteractive", dom_interactive), ("domContentLoadedEventStart", dom_interactive + 0.3),
              ("domContentLoadedEventEnd", dcl_end), ("domComplete", dom_complete), ("loadEventStart", dom_complete + 0.1),
              ("loadEventEnd", load_end)]
        self._span("doc", "documentLoad", t0, load_end, dict({
            "emb.type": "ux.document_load", "url.full": self.page["url"], "user_agent.original": p["ua"]}, **page_attrs),
            trace_id=tid, span_id=load_id,
            events=[{"name": n, "timeUnixNano": str(ns(t)), "attributes": [], "droppedAttributesCount": 0} for n, t in ev])

        ttfb = resp_start - t0
        self._vital("ttfb", round(ttfb, 1), t0, {
            "waitingDuration": round(rng.uniform(0.1, 3), 1), "cacheDuration": round(rng.uniform(0.5, 4), 1),
            "dnsDuration": round(dns, 1), "connectionDuration": round(tcp + tls, 1), "requestDuration": round(rtt + server, 1)},
            extra={"emb.web_vital.attribution.redirect": 0, "emb.web_vital.attribution.domainLookup": int(dns),
                   "emb.web_vital.attribution.tcpConnection": int(tcp), "emb.web_vital.attribution.tlsNegotiation": int(tls),
                   "emb.web_vital.attribution.serverResponse": int(rtt + server),
                   "emb.web_vital.attribution.unattributed": rng.randint(1, 9)})
        fcp = ttfb + lat(260, 900, rng) * cpu
        self._vital("fcp", round(fcp, 1), t0 + fcp, {"timeToFirstByte": round(ttfb, 1),
                                                     "firstByteToFCP": round(fcp - ttfb, 1), "loadState": "dom-interactive"})
        lcp = fcp + lat(180, 1300, rng) * cpu * (1.6 if pg_def.heavy else 1.0)
        res_delay = round(rng.uniform(20, 200), 1)
        res_dur = round(rng.uniform(40, 500) * (1.4 if mobile else 1), 1)
        vw = int(p["viewport"].split("x")[0])
        self._vital("lcp", round(lcp, 1), t0 + lcp, {
            "timeToFirstByte": round(ttfb, 1), "resourceLoadDelay": res_delay, "resourceLoadDuration": res_dur,
            "elementRenderDelay": round(max(1.0, lcp - ttfb - res_delay - res_dur), 1), "target": pg_def.lcp_target},
            extra={"emb.web_vital.attribution.elementType": pg_def.lcp_element,
                   "emb.web_vital.attribution.elementBoundingRect": json.dumps(
                       {"x": 0 if mobile else 24, "y": 64, "width": vw if mobile else vw - 48, "height": int(vw * 0.56)},
                       separators=(",", ":"))})
        self.t = max(self.t, load_end) + rng.uniform(50, 400)

    @staticmethod
    def _timing_events(t0, dns, tcp, tls, req_start, resp_start, resp_end, secure):
        marks = [("fetchStart", t0), ("domainLookupStart", t0 + 0.2), ("domainLookupEnd", t0 + 0.2 + dns),
                 ("connectStart", t0 + 0.2 + dns)]
        if secure and tls:
            marks.append(("secureConnectionStart", t0 + 0.2 + dns + tcp))
        marks += [("connectEnd", t0 + 0.2 + dns + tcp + tls), ("requestStart", req_start),
                  ("responseStart", resp_start), ("responseEnd", resp_end)]
        return [{"name": n, "timeUnixNano": str(ns(t)), "attributes": [], "droppedAttributesCount": 0} for n, t in marks]

    # --- interactions ---------------------------------------------------------------

    def click(self, element_html, selector=None, breadcrumb=None):
        vw, vh = (int(x) for x in self.persona["viewport"].split("x"))
        x, y = self.rng.randint(16, max(17, vw - 16)), self.rng.randint(80, max(81, vh - 40))
        t = self.t
        self.part.events.append({"attributes": otlp.attrs({"emb.type": "ux.tap", "view.name": element_html,
                                                          "tap.coords": f"{x},{y}"}),
                                 "name": "click", "timeUnixNano": str(ns(t)), "droppedAttributesCount": 0})
        self.page_clicks.append(t)
        if not self.clicked_since_load:
            self.clicked_since_load = True
            tag = element_html[1:].split(" ")[0].split(">")[0]
            self._otel_log("first", "first-interaction", {
                "first_interaction.interaction_type": "click", "first_interaction.element_type": tag,
                "first_interaction.element_selector": selector or tag, "first_interaction.time": round(t - self.start_ms, 1),
                "first_interaction.x": x, "first_interaction.y": y}, t=t)
        if breadcrumb:
            self.breadcrumb(breadcrumb)
        self._think()

    def breadcrumb(self, message):
        self.part.events.append({"attributes": otlp.attrs({"message": message}), "name": "emb-breadcrumb",
                                 "timeUnixNano": str(ns(self.t)), "droppedAttributesCount": 0})

    def type_text(self, chars):
        self.t += chars * self.rng.uniform(140, 320)

    # --- logs ------------------------------------------------------------------

    def log(self, severity, message, **attrs):
        sev = {"info": (9, "INFO"), "warning": (13, "WARNING"), "error": (17, "ERROR")}[severity]
        a = dict(attrs)
        a["emb.type"] = "sys.log"
        if severity != "info":
            a["emb.stacktrace.js"] = self._js_stack("Error", None, self.page["def"].bundle, internal=True)
            a["emb.js_file_bundle_ids"] = "{}"
        a["emb.state"] = "foreground"
        a.update(self._log_attrs())
        if severity == "error":
            self.part.error_logs += 1
        self._add_log("logs", {"timeUnixNano": str(ns(self.t)), "observedTimeUnixNano": str(ns(self.t + 0.1)),
                               "severityNumber": sev[0], "severityText": sev[1], "body": {"stringValue": message},
                               "eventName": "emb-log", "attributes": otlp.attrs(a), "droppedAttributesCount": 0})

    def exception(self, exc_type, message, handled=False, function="anonymous", **attrs):
        self.exception_number += 1
        a = dict(attrs)
        a.update({"emb.type": "sys.exception", "emb.exception_handling": "handled" if handled else "unhandled_error",
                  "emb.exception_cause": "", "exception.type": exc_type, "exception.name": exc_type,
                  "exception.message": message,
                  "exception.stacktrace": self._js_stack(exc_type, message, self.page["def"].bundle, function=function),
                  "emb.js_file_bundle_ids": "{}", "emb.state": "foreground", "emb.exception_number": self.exception_number})
        a.update(self._log_attrs())
        if not handled:
            self.part.unhandled += 1
        self._add_log("logs", {"timeUnixNano": str(ns(self.t)), "observedTimeUnixNano": str(ns(self.t + 0.1)),
                               "severityNumber": 17, "severityText": "ERROR", "body": {"stringValue": message},
                               "eventName": "emb-exception", "attributes": otlp.attrs(a), "droppedAttributesCount": 0})

    def _js_stack(self, exc_type, message, bundle, internal=False, function="anonymous"):
        cdn = self.profile.cdn
        main = self.profile.main_bundle
        r = self.rng
        head = "Error" if internal else f"{exc_type}: {message}"
        frames = []
        if internal:
            frames += [f"EmbraceLogManager2.message ({cdn}{main}:2:{r.randint(40000, 49999)})",
                       f"ProxyLogManager.message ({cdn}{main}:2:{r.randint(1000, 9999)})"]
        frames += [f"{function} ({cdn}{bundle}:1:{r.randint(2000, 60000)})",
                   f"Object.Ui ({cdn}{main}:9:{r.randint(10000, 99999)})",
                   f"pc ({cdn}{main}:9:{r.randint(10000, 99999)})"]
        return head + "\n" + "\n".join(f"    at {f}" for f in frames)

    def _vital(self, name, v, t, body, extra=None):
        a = {"emb.type": "ux.web_vital", "browser.web_vital.name": name, "browser.web_vital.value": float(v),
             "browser.web_vital.delta": float(v), "browser.web_vital.rating": rating(name, v),
             "browser.web_vital.id": f"v6-{int(self.start_ms)}-{self.rng.randint(10**12, 10**13 - 1)}",
             "browser.web_vital.navigation_type": "navigate", "browser.web_vital.navigation_id": self.navigation_id}
        a.update({"browser.url.full": self.page["url"], "app.surface.name": self.page["template"],
                  "app.surface.id": self.page["id"], "app.surface.label": self.page["label"]})
        a.update(extra or {})
        a.update(self._log_attrs())
        self._add_log("vitals", {"timeUnixNano": str(ns(t)), "observedTimeUnixNano": str(ns(t + self.rng.uniform(1, 5))),
                                 "severityNumber": 9, "body": {"stringValue": json.dumps(body, separators=(",", ":"))},
                                 "eventName": "browser.web_vital", "attributes": otlp.attrs(a), "droppedAttributesCount": 0},
                      t=t)

    def _otel_log(self, scope, event, a, t=None):
        t = self.t if t is None else t
        attrs = {"emb.type": "emb.otel_log"}
        attrs.update(a)
        attrs.update(self._log_attrs())
        self._add_log(scope, {"timeUnixNano": str(ns(t)), "observedTimeUnixNano": str(ns(t + 0.2)), "severityNumber": 9,
                              "body": {}, "eventName": event, "attributes": otlp.attrs(attrs), "droppedAttributesCount": 0},
                      t=t)

    def _add_log(self, scope, rec, t=None):
        self.part.logs.append((self.t if t is None else t, scope, rec))

    # --- spans ------------------------------------------------------------------

    def _span(self, scope, name, start, end, a, kind=1, trace_id=None, span_id=None, parent=None, events=None, status=0):
        span = {"traceId": trace_id or hex_id(16), "spanId": span_id or hex_id(8), "name": name, "kind": kind,
                "startTimeUnixNano": str(ns(start)), "endTimeUnixNano": str(ns(end)), "attributes": otlp.attrs(a),
                "droppedAttributesCount": 0, "events": events or [], "droppedEventsCount": 0, "status": {"code": status},
                "links": [], "droppedLinksCount": 0, "flags": 257}
        if parent:
            span["parentSpanId"] = parent
        self.part.spans.setdefault(scope, []).append(span)
        return span

    @contextmanager
    def flow(self, name, **attrs):
        """Custom performance span (trace.startSpan) around a user-facing operation."""
        holder = {"failed": False}
        start = self.t
        yield holder
        a = dict(attrs)
        if holder["failed"]:
            a["emb.error_code"] = "FAILURE"
        a["emb.type"] = "perf"
        a.update(self._page_attrs())
        self._span("traces", name, start, self.t + self.rng.uniform(2, 30), a)

    # --- network ----------------------------------------------------------------

    def api(self, key, **params):
        return self.parallel((key, params))[0]

    def parallel(self, *calls):
        start = self.t
        results = [self._request(key, params, start + i * self.rng.uniform(0.2, 4)) for i, (key, params) in enumerate(calls)]
        self.t = max(r.end for r in results) + lat(15, 90, self.rng)
        return results

    def third_party(self, method, url, status=204):
        t0 = self.t + self.rng.uniform(1, 30)
        d = lat(60, 220, self.rng)
        host = url.split("/")[2]
        a = {"http.request.method": method, "url.full": url, "http.response.status_code": status,
             "server.address": host, "server.port": 443, "browser.url.full": self.page["url"],
             "emb.type": "perf.network_request"}
        a.update({k: v for k, v in self._page_attrs().items() if k != "browser.url.full"})
        self._span("fetch", method, t0, t0 + d, a, kind=3)

    def _request(self, key, params, start):
        c = simulate(self, key, params, start, self.rng)
        ep, m = c.ep, c.marks
        host = self.profile.api_host
        a = {"http.request.method": ep.method, "url.full": c.url, "emb.w3c_traceparent": c.traceparent,
             "http.response.status_code": c.status, "server.address": host, "server.port": 443}
        if c.status >= 400:
            a["error.type"] = str(c.status)
        a.update({"browser.url.full": self.page["url"], "emb.type": "perf.network_request"})
        a.update({k: v for k, v in self._page_attrs().items() if k != "browser.url.full"})
        events = self._timing_events(start, m["dns"], m["tcp"], m["tls"], m["req_start"], m["resp_start"],
                                     m["resp_end"], m["first"])
        self._span("fetch", ep.method, start, c.end, a, kind=3, trace_id=c.trace_id, span_id=c.span_id,
                   events=events, status=2 if c.status >= 400 else 0)
        deliver_backend(self, c, self.rng)
        self.calls.append((key, c.status, c.trace_id))
        return Result(c.status, c.outcome, c.data, c.trace_id, start, c.end)

    @property
    def client_user_agent(self):
        return self.persona["ua"]

    def client_resource_spans(self, c):
        """Browser span in Chronosphere for setups without network span forwarding."""
        res = otlp.attrs({"service.name": self.profile.web_service, "service.namespace": self.profile.namespace,
                          "service.version": self.app_version, "deployment.environment.name": "production",
                          "telemetry.sdk.name": "opentelemetry", "telemetry.sdk.language": "webjs",
                          "user_agent.original": self.persona["ua"]})
        span = {"traceId": c.trace_id, "spanId": c.span_id, "name": f"{c.ep.method} {c.ep.route}", "kind": 3,
                "startTimeUnixNano": str(ns(c.start)), "endTimeUnixNano": str(ns(c.end)), "flags": 257,
                "status": {"code": 2 if c.status >= 400 else 0},
                "attributes": otlp.attrs({"http.request.method": c.ep.method, "url.full": c.url, "http.route": c.ep.route,
                                          "http.response.status_code": c.status, "server.address": self.profile.api_host,
                                          "emb.device_id": self.persona["device_id"], "emb.session_id": self.part.id,
                                          "emb.user_session_id": self.user_session_id})}
        return {"resource": {"attributes": res}, "scopeSpans": [{"scope": SDK_SCOPES["fetch"], "spans": [span]}]}

    # --- output -------------------------------------------------------------------

    def _deliver(self, due, target, kind, payload):
        self.deliveries.append({"due": due, "target": target, "kind": kind, "payload": payload,
                                "app_id": self.app_id, "device_id": self.persona["device_id"],
                                "ua": self.persona["ua"], "origin": self.profile.web_origin})

    def finish(self):
        """Close the session if the journey did not, and update the visitor's counters."""
        if not self.ended:
            self.leave()
        self.persona["user_session_number"] = self.user_session_number
        self.persona["session_part_number"] = self.part_number
        self.persona["previous_user_session_id"] = self.user_session_id
        self.end_ms = self.t
        return self.deliveries


TIME_KEYS = ("startTimeUnixNano", "endTimeUnixNano", "timeUnixNano", "observedTimeUnixNano")


def shift(obj, delta_ns):
    """Shift every OTLP timestamp in a payload (used to place a simulated session in the past)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TIME_KEYS and isinstance(v, str):
                obj[k] = str(int(v) + delta_ns)
            elif k == "key" and v == "emb.user_session_start_ts":
                obj["value"]["doubleValue"] = round(obj["value"]["doubleValue"] + delta_ns / 1e6, 1)
            else:
                shift(v, delta_ns)
    elif isinstance(obj, list):
        for x in obj:
            shift(x, delta_ns)
