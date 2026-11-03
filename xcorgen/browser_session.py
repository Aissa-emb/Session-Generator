"""Archetype-aware Embrace Web SDK 2.31 session adapter.

Wire contracts come from the SDK instrumentations, per-engine goldens and
the scrubbed production capture. Local page/fault bookkeeping is never exported.
"""
import copy
import json
import random
import re
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import otlp, web_faults, web_sites, web_vitals
from .android_session import SessionEnded, compose_label, SHARED_REPLACED_PROPERTIES
from .calls import Result, deliver_backend, release_backend, simulate
from .catalog import LOB_FOR
from .util import emb_id, hex_id, lat, uuid4, weighted
from .web_capabilities import capabilities

SDK_SCOPES = {k: {"name": n, "version": "1.0.0"} for k, n in {
    "nav": "NavigationInstrumentation", "doc": "DocumentLoadInstrumentation",
    "vitals": "WebVitalsInstrumentation", "loaf": "LoafInstrumentation",
    "first": "FirstInteractionInstrumentation", "scroll": "MaxScrollDepthInstrumentation",
    "dom": "DOMStateInstrumentation", "rage": "RageClickInstrumentation",
    "user": "UserTimingInstrumentation", "element": "ElementTimingInstrumentation",
    "server": "ServerTimingInstrumentation", "soft": "SoftNavigationPerformanceInstrumentation"}.items()}
SDK_SCOPES.update({"part": {"name": "embrace-web-sdk-session-parts"},
    "traces": {"name": "embrace-web-sdk-traces"}, "logs": {"name": "embrace-web-sdk-logs"},
    "fetch": {"name": "@opentelemetry/instrumentation-fetch", "version": "0.223.0"},
    "xhr": {"name": "@opentelemetry/instrumentation-xml-http-request", "version": "0.223.0"}})
LOG_BATCH_MS = 1000


def releases(web, now_ms):
    """Configured deploys or two rolling releases; tabs retain their selected release."""
    from .spec import iso
    return web.get("releases") or [
        {"version": "2026.10.30.1", "deployed": iso(now_ms - 86400000), "share": .8},
        {"version": "2026.10.23.2", "deployed": iso(now_ms - 8 * 86400000), "share": .2}]


def pick_release(web, start_ms, rng=random):
    from .spec import parse_time
    available = [r for r in releases(web, start_ms) if not r.get("deployed") or parse_time(r["deployed"]) <= start_ms]
    if not available:
        raise ValueError("No web release had been deployed at the planned session start.")
    return weighted([(r["version"], r["share"]) for r in available], rng)


class Part:
    def __init__(self, number, index, start, reason, cold, rng):
        self.id, self.trace_id, self.span_id = emb_id(rng), hex_id(16, rng), hex_id(8, rng)
        self.number, self.index, self.start = number, index, start
        self.reason, self.cold = reason, cold
        self.events, self.spans, self.logs, self.frames = [], {}, [], []
        self.errors = self.unhandled = 0
        self.first = False
        self.closed = False


class BrowserSession:
    def __init__(self, profile, persona, app_id, start_ms, incident=None, emit_client_spans=False,
                 rng=random, shared=True, web=None, release=None, performance=None, population=None,
                 api_host=None, app_name="Vantra Web", environment="production"):
        from .spec import WEB_TARGET
        self.profile, self.persona, self.app_id, self.rng = profile, persona, app_id, rng
        self.lob = LOB_FOR[profile.key]
        self.web = copy.deepcopy(WEB_TARGET["web"])
        self.web.update(web or {})
        self.population, self.performance = population or {}, performance or []
        site = self.web["estate"][self.lob]
        self.origin, self.archetype, self.cdn = site["origin"].rstrip("/"), site["archetype"], self.web["cdn"].rstrip("/")
        self.pages, self.caps = web_sites.SITES[self.lob], capabilities(persona)
        self.incident, self.emit_client_spans, self.traces = incident, emit_client_spans, True
        self.api_host = api_host or profile.android["shared_api_host"]
        self.app_name, self.environment = app_name, environment
        self.t = self.start_ms = self.p(start_ms)
        self.app_version = release or pick_release(self.web, start_ms, rng)
        self.sdk_version = self.web.get("sdk_version", "2.31.0")
        self.storage = persona.setdefault("web_origins", {}).setdefault(self.origin, {
            "device_id": emb_id(rng), "user_session_number": 0, "session_part_number": 0,
            "previous_user_session_id": "", "exception_number": 0, "external_user_id": None})
        self.device_id = self.storage["device_id"]
        self.user_session_id = emb_id(rng)
        self.previous_user_session_id = self.storage["previous_user_session_id"]
        self.user_session_number = self.storage["user_session_number"] + 1
        self.part_number = self.storage["session_part_number"]
        self.exception_number = self.storage["exception_number"]
        self.user_id = persona.get("user_id") or self.storage.get("external_user_id")
        self.instance_id = emb_id(rng)
        self.properties = {"business_unit": self.lob, "market": "US",
            "membership_tier": persona["traits"].get("membership", "standard"),
            "entry_point": weighted([("direct", 35), ("organic_search", 40), ("paid_search", 15), ("email", 10)], rng)}
        self.properties.update({k: str(v) for k, v in profile.properties(persona, rng).items()
                                if k not in SHARED_REPLACED_PROPERTIES})
        self.connected_hosts, self.calls, self.deliveries = set(), [], []
        self.part = self.page = None
        self.parts, self.history, self.pending_logs, self.log_records = [], [], [], []
        self.ended, self.engaged = False, True
        self.page_count = 0
        self.navigation_id = rng.randint(3000, 9000) if persona["chromium"] else 0
        self.last_click, self.rage_window, self.used_faults = None, [], set()
        self.resource = self._resource()

    def p(self, value):
        return web_vitals.precision(value, self.caps["fractional_time"])

    def ns(self, value):
        # Multiply only after rounding: float epoch multiplication can break millisecond precision.
        if not self.caps["fractional_time"]:
            return str(round(value) * 1000000)
        return str(int(round(value * 1000)) * 1000)

    @property
    def client_user_agent(self):
        return self.persona["ua"]

    def _resource(self):
        p = self.persona
        a = {"telemetry.sdk.name": "embrace-web-sdk", "app_version": self.app_version,
             "app_framework": 1, "sdk_version": self.sdk_version, "telemetry.sdk.version": self.sdk_version,
             "sdk_simple_version": 1, "sdk_platform": "web", "telemetry.sdk.language": "webjs",
             "emb.app_instance_id": self.instance_id, "user_agent.original": p["ua"],
             "screen_resolution": p["screen"], "viewport_resolution": p["viewport"],
             "webdriver": False, "service.name": "embrace-web-sdk"}
        if self.caps["device_info"]:
            a.update(network_effective_type=p["effective_type"], device_memory=p["device_memory"])
        return {"attributes": otlp.attrs(a)}

    def _common(self, page=None):
        pg = page or self.page
        return {"browser.url.full": pg["url"], "app.surface.id": pg["id"], "app.surface.label": pg["label"],
                "app.surface.name": pg["template"] if self.web.get("router_integration") == "route_templates" else None,
                "user.id": self.user_id, "emb.perf_clock_lag_ms": 0}

    def _log_common(self, timestamp=None, page=None):
        part = self.part
        if timestamp is not None:
            part = next((p for p in reversed(self.parts) if p.start <= timestamp), self.parts[0])
        return dict(self._common(page), **{"log.record.uid": emb_id(self.rng),
            "emb.user_session_id": self.user_session_id, "emb.user_session_previous_id": self.previous_user_session_id,
            "emb.session_part_id": part.id, "url.full": (page or self.page)["url"], "emb.is_tab_engaged": self.engaged})

    def _events(self, marks):
        return [{"name": n, "timeUnixNano": self.ns(t), "attributes": [], "droppedAttributesCount": 0}
                for n, t in marks]

    def _span(self, scope, name, start, end, attributes, kind=1, trace_id=None, span_id=None,
              parent=None, events=None, status=0, page=None):
        a = dict(self._common(page), **attributes)
        span = {"traceId": trace_id or hex_id(16, self.rng), "spanId": span_id or hex_id(8, self.rng),
                "name": name, "kind": kind, "startTimeUnixNano": self.ns(start), "endTimeUnixNano": self.ns(end),
                "attributes": otlp.attrs(a), "droppedAttributesCount": 0, "events": events or [],
                "droppedEventsCount": 0, "status": {"code": status}, "links": [], "droppedLinksCount": 0, "flags": 257}
        if parent:
            span["parentSpanId"] = parent
        self.part.spans.setdefault(scope, []).append(span)
        return span

    def _log(self, scope, event, attributes, body=None, timestamp=None, severity=9, text=None, page=None):
        t = self.t if timestamp is None else timestamp
        a = dict(self._log_common(timestamp, page), **attributes)
        rec = {"timeUnixNano": self.ns(t), "observedTimeUnixNano": self.ns(self.t),
               "severityNumber": severity, "body": {} if body is None else {"stringValue": body},
               "eventName": event, "attributes": otlp.attrs(a), "droppedAttributesCount": 0}
        if text:
            rec["severityText"] = text
        self.pending_logs.append((self.t, scope, rec))
        self.log_records.append(rec)
        return rec

    def _start_part(self, reason, cold=False):
        self.part_number += 1
        self.part = Part(self.part_number, len(self.parts) + 1, self.t, reason, cold, self.rng)
        self.parts.append(self.part)
        if self.page:
            self.page["surface_start"] = self.t
            self.page["first_interaction"] = False
        for start, end, url in getattr(self, "pending_uploads", []):
            self._span("fetch", "POST", start, end, {"emb.type": "perf.network_request", "http.request.method": "POST",
                "url.full": url, "http.response.status_code": 200, "server.address": urlsplit(url).hostname, "server.port": 443}, kind=3)
        self.pending_uploads = []

    def _surface_end(self, outgoing_url=None):
        pg = self.page
        if pg is None or pg.get("surface_start") is None:
            return
        a = {"emb.type": "ux.surface"}
        if outgoing_url:
            a["browser.url.full"] = outgoing_url
        name = pg["template"] if self.web.get("router_integration") == "route_templates" else urlsplit(pg["url"]).path
        self._span("nav", name, pg["surface_start"], self.t, a)
        pg["surface_start"] = None

    def _end_part(self, reason, final=None, outgoing_url=None):
        part = self.part
        if not part or part.closed:
            return
        self._surface_end(outgoing_url)
        self._page_state()
        self._flush_rage()
        self._loaf_report()
        for span in part.spans.get("soft", []):
            lo, hi = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
            from .web_observation import attrs
            spans = [s for p in self.parts for group in p.spans.values() for s in group
                     if s is not span and lo <= int(s["startTimeUnixNano"]) <= hi]
            logs = [r for r in self.log_records if lo <= int(r["timeUnixNano"]) <= hi and int(r["observedTimeUnixNano"]) <= hi]
            a = attrs(span)
            a.update({"emb.soft_navigation.span_ids": [s["spanId"] for s in spans],
                      "emb.soft_navigation.span_id_types": [attrs(s).get("emb.type", "") for s in spans],
                      "emb.soft_navigation.log_ids": [attrs(r)["log.record.uid"] for r in logs],
                      "emb.soft_navigation.log_id_types": [attrs(r).get("emb.type", "") for r in logs]})
            span["attributes"] = otlp.attrs(a)
        self._flush_logs(hidden=reason == "background")
        a = {"emb.user_session_id": self.user_session_id, "emb.user_session_previous_id": self.previous_user_session_id,
             "emb.user_session_number": self.user_session_number, "emb.user_session_part_index": part.index,
             "emb.session_part_number": part.number, "emb.user_session_start_ts": self.start_ms,
             "emb.user_session_max_duration_seconds": 43200, "emb.user_session_inactivity_timeout_seconds": 1800,
             "emb.user_session_foreground_inactivity_timeout_seconds": 1800, "emb.type": "ux.session_part",
             "emb.state": "foreground", "emb.session_part_id": part.id, "emb.session_part_start_reason": part.reason,
             "emb.cold_start": part.cold, "emb.session_part_end_reason": reason,
             "emb.sdk_startup_duration": self.page["sdk_startup"]}
        a.update({"emb.properties." + k: str(v) for k, v in self.properties.items()})
        if part.errors:
            a["emb.error_log_count"] = part.errors
        if part.unhandled:
            a["emb.unhandled_exceptions_count"] = part.unhandled
        if self.page["first_part"]:
            a["emb.page_load"] = self.page["loaded"]
            self.page["first_part"] = False
        if final:
            a.update({"emb.is_final_session_part": 1, "emb.user_session_termination_reason": final})
        if outgoing_url:
            a["browser.url.full"] = outgoing_url
        self._span("part", "emb-session-part", part.start, self.t, a, trace_id=part.trace_id,
                   span_id=part.span_id, events=sorted(part.events, key=lambda e: int(e["timeUnixNano"])))
        scopes = [{"scope": SDK_SCOPES[k], "spans": part.spans[k]} for k in ["part"] +
                  [k for k in part.spans if k != "part"]]
        self._deliver(self.t + self.rng.uniform(80, 600), "embrace", "spans",
                      {"resourceSpans": [{"resource": self.resource, "scopeSpans": scopes}]})
        release_backend(self, self.t, self.rng)
        part.closed = True

    def page_view(self, key, **params):
        if self.ended:
            raise SessionEnded()
        definition = self.pages[key]
        params = dict(params, _user=self.user_id or self.device_id, _session=self.user_session_id)
        url = self.origin + definition.path(params)
        if self.page and url == self.page["url"]:
            return
        self.page_count += 1
        hard = self.page is None or self.archetype == "multi_page" or (
            self.archetype == "hybrid" and definition.render == "server")
        nav_type, restore = "navigate", None
        if self.page:
            self._finish_vitals()
            if hard:
                self._end_part("background")
                self.history.append(self.page)
                self.t = self.p(self.t + self.rng.uniform(30, 160))
                if len(self.history) > 1 and self.rng.random() < .08:
                    nav_type = "back_forward"
                    if self.rng.random() < self.population.get("bfcache_share", .6):
                        restore = next((p for p in reversed(self.history) if p["url"] == url), None)
            elif self.caps["navigation_api"]:
                self._end_part("web_soft_navigation", outgoing_url=url)
            else:
                self._surface_end(outgoing_url=url)
        document_zero = self.t if hard else self.page["document_zero"]
        first_part = hard and not restore or bool(self.page and self.page["first_part"] and not hard)
        above_fold = bool(restore) if hard else self.page["above_fold"]
        first_interaction = False if hard else self.page.get("first_interaction", False)
        self.navigation_id += 1 if self.persona["chromium"] else 0
        self.page = {"key": key, "def": definition, "params": params, "url": url, "template": definition.route,
                     "label": definition.label(params, self.lob), "id": restore["id"] if restore else emb_id(self.rng),
                     "zero": self.t, "document_zero": document_zero, "surface_start": self.t, "first_part": first_part,
                     "sdk_startup": self.rng.randint(5, 60) if hard else self.page["sdk_startup"],
                     "loaded": True, "above_fold": above_fold, "first_interaction": first_interaction,
                     "vitals_done": False, "lcp_done": False,
                     "clicks": [], "measures": set(), "nav_type": "back-forward-cache" if restore else
                     nav_type.replace("_", "-") if hard else "soft-navigation", "navigation_id": self.navigation_id}
        if hard or self.caps["navigation_api"]:
            self._start_part("foreground" if restore else "init" if hard else "web_soft_navigation", cold=hard and not restore)
        self.page["faults"] = self._page_faults()
        targets, mix = web_vitals.targets(self.performance, definition.group, self.persona["kind"], self.persona["browser"],
                                         None if hasattr(self, "execution") else self.persona)
        self.page["tbd_mix"] = mix
        self._fault_targets(targets)
        self.page["vitals"] = web_vitals.PageVitals(targets, self.rng, self.caps["fractional_time"])
        if hasattr(self, "execution"):
            # Causal fixtures budget rendering separately from asynchronous API latency.
            # Bounded simulated paint work ensures navigation completes before the next action.
            v = self.page["vitals"]
            v.ttfb = min(v.ttfb, 450)
            v.fcp = max(v.ttfb + 30, min(v.fcp, 800))
            v.lcp = max(v.fcp, min(v.lcp, 1800))

        if restore:
            v = self.page["vitals"]
            v.ttfb, v.fcp, v.lcp = 0, self.rng.uniform(20, 80), self.rng.uniform(100, 220)
            self._initial_vitals(0, 0, 0, 0)
            self.t = self.p(self.t + v.lcp + 10)
        elif hard:
            if self._has_fault("page_abandonment"):
                self.page["loaded"] = False
                self.t = self.p(self.t + self.rng.uniform(500, 1800))
                self._end_part("background")
                self.ended = True
                raise SessionEnded()
            self._document_load(nav_type)
        else:
            self._soft_navigation()
        self._page_exceptions()

    def _page_faults(self):
        out = []
        for f in getattr(getattr(self, "plan", None), "web_faults", []):
            flt, d = f.get("filters", {}), f.get("details", {})
            if flt.get("page_groups") and self.page["def"].group not in flt["page_groups"]:
                continue
            if flt.get("routes") and self.page["template"] not in flt["routes"]:
                continue
            if d.get("page") and d["page"] not in (self.page["key"], self.page["def"].group, self.page["template"]):
                continue
            out.append(f)
        return out

    def _has_fault(self, kind):
        return next((f for f in self.page["faults"] if f["type"] == kind), None)

    def _fault_targets(self, targets):
        for f in self.page["faults"]:
            d, kind = f["details"], f["type"]
            targets.update(d.get("targets", {}))
            if kind == "poor_web_vitals" and not d.get("targets"):
                targets.update(lcp=4800, inp=550, cls=.3)
            metric = {"lcp_regression": "lcp", "layout_instability": "cls", "interaction_latency": "inp",
                      "slow_server_response": "ttfb", "soft_navigation_slowness": "lcp"}.get(kind)
            if metric:
                targets[metric] = d.get("p75", d.get("p75_ms", d.get(metric, {"lcp": 4000, "cls": .3,
                                                                                   "inp": 450, "ttfb": 2200}[metric])))
            if kind == "lcp_regression" and d.get("cause") == "origin":
                targets["ttfb"] = max(targets["ttfb"], targets["lcp"] * .6)
        targets["fcp"] = max(targets["fcp"], targets["ttfb"] + 150)
        targets["lcp"] = max(targets["lcp"], targets["fcp"] + 150)

    def _resource_span(self, name, url, start, end, size, ratio, initiator, tid, parent,
                       blocking=False, cached=False, opaque=False, revalidated=False, extra=None, events=None):
        encoded = 0 if opaque else size
        decoded = int(encoded * ratio)
        a = {"url.full": url, "emb.type": "ux.resource_fetch", "http.response_content_length": encoded,
             "http.request.initiator_type": initiator, "http.response.size": 0 if opaque or cached else 300 if revalidated else size + 300,
             "http.response.body.size": encoded, "http.response.decoded_body_size": decoded}
        if decoded != encoded:
            a["http.response_content_length_uncompressed"] = decoded
        if self.caps["resource_status"] and not opaque:
            a["http.response.status_code"] = 200
        if self.caps["render_blocking_status"]:
            a["http.request.render_blocking_status"] = "blocking" if blocking else "non-blocking"
        if cached and self.caps["delivery_type"]:
            a["http.response.delivery_type"] = "cache"
        if opaque:
            a["http.response.cors_opaque"] = True
        if revalidated and not cached:
            a["http.response.cache_revalidated"] = True
        a.update(extra or {})
        return self._span("doc", name, start, end, a, trace_id=tid, parent=parent,
                          events=events or self._events([("fetchStart", start), ("responseEnd", end)]))

    def _document_load(self, nav_type):
        pg, rng = self.page, self.rng
        v, zero = pg["vitals"], pg["zero"]
        first = self.origin not in self.connected_hosts
        self.connected_hosts.add(self.origin)
        # Scale connection setup within the requested TTFB distribution.
        dns, tcp, tls = (v.ttfb * .04, v.ttfb * .09, v.ttfb * .12) if first else (0, 0, 0)
        body, attrs, parts = v.ttfb_parts(dns, tcp, tls)
        request = zero + v.ttfb - parts["request"]
        response = zero + v.ttfb
        html_end = response + rng.uniform(8, 30)
        # SDK uses responseEnd-startTime for its rounded breakdown, not the metric's responseStart.
        attrs["unattributed"] = max(0, round(html_end - zero) - sum(x for k, x in attrs.items() if k != "unattributed"))
        pg["ttfb_body"], pg["ttfb_attrs"] = body, attrs
        tid, load_id = hex_id(16, rng), hex_id(8, rng)
        marks = [("fetchStart", zero + parts["fetch"]), ("domainLookupStart", zero + parts["fetch"] + parts["cache"]),
                 ("domainLookupEnd", zero + parts["fetch"] + parts["cache"] + dns),
                 ("connectStart", zero + parts["fetch"] + parts["cache"] + dns)]
        if tls:
            marks.append(("secureConnectionStart", request - tls))
        marks += [("connectEnd", request), ("requestStart", request)]
        if self.caps["engine"] != "webkit" or rng.random() < .6:
            marks.append(("responseStart", response))
        marks.append(("responseEnd", html_end))
        extra = {"browser.navigation_timing.type": nav_type}
        if nav_type == "back_forward" and self.caps["not_restored_reasons"]:
            pg["not_restored"] = ["outstanding-network-request"]
            extra["browser.navigation_timing.not_restored_reasons"] = pg["not_restored"]
        self._resource_span("documentFetch", pg["url"], marks[0][1], html_end, rng.randint(24000, 50000), 4,
                            "navigation", tid, load_id, extra=extra, events=self._events(marks))
        varied = self.persona.get('population_model') and not hasattr(self, 'execution')
        latest = blocking_end = html_end
        for i, (url, initiator, size, ratio, blocking) in enumerate(web_sites.first_party(self.cdn, self.lob, self.app_version, pg["def"])):
            if self.web.get('symbolication') and initiator=='script':
                from .web_symbols import bundle
                import gzip
                raw=bundle(url,self.app_version)['bundle'].encode()
                size=len(gzip.compress(raw,mtime=0));ratio=len(raw)/size
            st = html_end + 4 + i * 2
            cached = self.user_session_number > 1 and rng.random() < .75
            en = st + (rng.uniform(2, 5) if cached else
                       self.persona['rtt'] + size * 8 / (self.persona['mbps'] * 1000) if self.persona.get('population_model')
                       and not hasattr(self, 'execution') else rng.uniform(30, 120))
            latest = max(latest, en)
            if blocking:
                blocking_end = max(blocking_end, en)
            self._resource_span("resourceFetch", url, st, en, size, ratio, initiator, tid, load_id,
                                blocking=blocking, cached=cached, revalidated=not cached and rng.random() < .06)
        if varied:
            v.fcp = max(v.fcp, blocking_end - zero + 40 * self.persona['cpu_factor'])
            v.lcp = max(v.lcp, v.fcp)
        lcp_parts = v.lcp_parts(bool(pg["def"].lcp[2]))
        if pg["def"].lcp[2]:
            if varied:
                # Fixed image bytes determine transport time, not the reverse.
                size = 160000
                transfer_ms = self.persona['rtt'] + size * 8 / (self.persona['mbps'] * 1000)
                delay = lcp_parts['resourceLoadDelay']
                v.lcp = max(v.lcp, v.ttfb + delay + transfer_ms + 40 * self.persona['cpu_factor'])
                lcp_parts.update(resourceLoadDuration=self.p(transfer_ms),
                                 elementRenderDelay=self.p(v.lcp - v.ttfb - delay - transfer_ms))
            url = self.cdn + pg["def"].lcp[2](pg["params"])
            st = response + lcp_parts["resourceLoadDelay"]
            en = st + max(1, lcp_parts["resourceLoadDuration"])
            if not varied:
                size = max(12000, round(lcp_parts["resourceLoadDuration"] * self.persona["mbps"] * 125 * .5))
            self._resource_span("resourceFetch", url, st, en, size, 1, "img", tid, load_id)
            latest = max(latest, en)
        pg["lcp_parts"] = lcp_parts
        for tag in web_sites.THIRD_PARTY.values():
            if self.lob not in tag["sites"]:
                continue
            st = html_end + rng.uniform(15, 60)
            en = st + rng.uniform(40, 180) * (1 + self.population.get("third_party_bloat", 0))
            latest = max(latest, en)
            self._resource_span("resourceFetch", tag["url"], st, en, tag["size"], 3, tag["initiator"], tid, load_id,
                                opaque=not tag["timing"], blocking=tag.get("blocking", False))
        interactive = max(html_end + 20, zero + v.fcp - 10)
        dcl = interactive + 8
        load_end = max(latest, dcl, zero + v.lcp) + rng.uniform(10, 40)
        events = [("fetchStart", marks[0][1]), ("domInteractive", interactive),
                  ("domContentLoadedEventStart", interactive + 1), ("domContentLoadedEventEnd", dcl),
                  ("domComplete", load_end - 3), ("loadEventStart", load_end - 1), ("loadEventEnd", load_end)]
        if self.caps["engine"] == "chromium":
            events.append(("firstPaint", zero + v.fcp - 5))
        if self.caps["engine"] != "webkit":
            events.append(("firstContentfulPaint", zero + v.fcp))
        self._span("doc", "documentLoad", marks[0][1], load_end,
                   {"emb.type": "ux.document_load", "url.full": pg["url"], "user_agent.original": self.client_user_agent},
                   trace_id=tid, span_id=load_id, events=self._events(events))
        self.t = self.p(load_end + 30)
        if self._has_fault("slow_server_response") or (self._has_fault("lcp_regression") or {}).get("details", {}).get("cause") == "origin":
            self.pending_origin_latency = parts["request"]
        self._initial_vitals(dns, tcp, tls, html_end - zero)
        if pg["def"].render == "server":
            for name, duration, description in web_sites.server_timing(rng, parts["request"]):
                self._log("server", "emb-server-timing", {"emb.type": "ux.server_timing",
                    "emb.server_timing.name": name, "emb.server_timing.duration": self.p(duration),
                    "emb.server_timing.description": description})
        if self.caps["element_timing"]:
            for identifier, tag in pg["def"].elements:
                self._span("element", identifier, zero, zero + v.lcp, {"emb.type": "ux.element_timing",
                    "emb.element_timing.identifier": identifier, "emb.element_timing.element": tag,
                    "emb.element_timing.render_time": self.p(v.lcp), "emb.element_timing.load_time": self.p(v.lcp - 10),
                    "emb.element_timing.start_time": self.p(v.lcp), "emb.element_timing.url":
                    self.cdn + pg["def"].lcp[2](pg["params"]) if pg["def"].lcp[2] else "",
                    "emb.element_timing.natural_width": 1600 if tag == "img" else 0,
                    "emb.element_timing.natural_height": 900 if tag == "img" else 0},
                    events=self._events([("load", zero + v.lcp - 10)]))
        self._load_frames()
        self.third_party("POST", "https://www.google-analytics.com/g/collect", status=0, opaque=True)

    def _initial_vitals(self, dns, tcp, tls, response_end):
        pg, v = self.page, self.page["vitals"]
        if pg["nav_type"] != "soft-navigation":
            if "ttfb_body" in pg:
                body, a = pg["ttfb_body"], pg["ttfb_attrs"]
            else:
                body, a, _ = v.ttfb_parts(dns, tcp, tls)
            if pg["nav_type"] == "back-forward-cache":
                v.ttfb = 0
                body, a = {k: 0 for k in body}, {k: 0 for k in a}
            self._vital("ttfb", v.ttfb, pg["zero"], pg.get("ttfb_body", body),
                        {"emb.web_vital.attribution." + k: val for k, val in pg.get("ttfb_attrs", a).items()})
        self._vital("fcp", v.fcp, pg["zero"] + v.fcp, {"timeToFirstByte": self.p(v.ttfb),
                    "firstByteToFCP": self.p(v.fcp - v.ttfb), "loadState": "dom-interactive"})

    def _vital(self, name, value, timestamp, body, extra=None):
        pg = self.page
        if not self.caps.get(name, False) or (pg["nav_type"] == "soft-navigation" and not self.caps["soft_navigation_vitals"]):
            return
        value = round(value, 5) if name == "cls" else self.p(value)
        a = {"emb.type": "ux.web_vital", "browser.web_vital.name": name, "browser.web_vital.value": value,
             "browser.web_vital.delta": value, "browser.web_vital.rating": web_vitals.rating(name, value),
             "browser.web_vital.id": f"v6-{int(self.t)}-{self.rng.randint(10**12, 10**13-1)}",
             "browser.web_vital.navigation_type": pg["nav_type"], "browser.web_vital.navigation_id": pg["navigation_id"]}
        if pg["nav_type"] == "soft-navigation":
            a["browser.web_vital.interaction_id"] = pg["interaction_id"]
        if pg.get("not_restored"):
            a["browser.navigation_timing.not_restored_reasons"] = pg["not_restored"]
        a.update(extra or {})
        self._log("vitals", "browser.web_vital", a, json.dumps(body, separators=(",", ":")), timestamp)

    def _lcp(self):
        pg, v = self.page, self.page["vitals"]
        if pg["lcp_done"] or not pg["loaded"]:
            return
        pg["lcp_done"] = True
        body = dict(pg.get("lcp_parts") or v.lcp_parts(bool(pg["def"].lcp[2])), target=pg["def"].lcp[0])
        if pg["def"].lcp[2]:
            body["url"] = self.cdn + pg["def"].lcp[2](pg["params"])
        vw = int(self.persona["viewport"].split("x")[0])
        self._vital("lcp", v.lcp, pg["zero"] + v.lcp, body, {
            "emb.web_vital.attribution.elementType": pg["def"].lcp[1],
            "emb.web_vital.attribution.elementBoundingRect": json.dumps({"x": 0, "y": 64, "width": vw, "height": int(vw * .56)})})

    def _finish_vitals(self):
        pg, v = self.page, self.page["vitals"]
        if pg["vitals_done"] or not pg["loaded"]:
            return
        self._lcp()
        pg["vitals_done"] = True
        shift = self.p(min(v.lcp, max(0, self.t - pg["zero"] - 1)))
        cls_body, extra = {}, {}
        if v.cls > 0:
            cls_body = {"largestShiftTarget": pg["def"].cls, "largestShiftTime": shift,
                        "largestShiftValue": round(v.cls, 5), "loadState": "complete"}
            extra = {"emb.web_vital.attribution.clsLayoutShifts": json.dumps([{"selector": pg["def"].cls,
                "startRect": {"x": 0, "y": 120, "width": 360, "height": 100},
                "endRect": {"x": 0, "y": 120 + max(1, int(v.cls * 200)), "width": 360, "height": 100}}])}
        self._vital("cls", v.cls, pg["zero"] + shift, cls_body, extra)
        if pg["clicks"] and self.caps["inp"]:
            click = max(pg["clicks"], key=lambda c: c["duration"])
            value, parts = v.inp_parts()
            body = dict(interactionTarget=click["selector"], interactionType="pointer",
                        interactionTime=self.p(click["time"] - pg["zero"]),
                        nextPaintTime=self.p(click["time"] - pg["zero"] + value), **parts, loadState="complete")
            script = self._slow_script()
            extra = {"emb.web_vital.attribution.element_type": click["tag"]}
            if self.caps["loaf"]:
                extra["emb.web_vital.attribution.loaf_scripts"] = json.dumps({script: {
                    "total_duration": self.p(parts["processingDuration"] * .8),
                    "style_and_layout_duration": self.p(parts["processingDuration"] * .2), "count": 1}})
            self._vital("inp", value, click["time"], body, extra)

    def _bundle(self, chunk=None):
        return web_sites.bundle_url(self.cdn, self.lob, self.app_version, chunk or self.page["def"].chunk)

    def _bundle_ids(self):
        if not self.web.get("symbolication"):
            return "{}"
        from .web_symbols import bundle_ids
        urls = [r[0] for r in web_sites.first_party(self.cdn,self.lob,self.app_version,self.page['def']) if r[1]=='script']
        return bundle_ids(urls+[self._bundle(),self._bundle('vendor')],self.app_version,self.caps['engine'])

    def _slow_script(self):
        fault = self._has_fault("third_party_blocking")
        interaction = self._has_fault("interaction_latency")
        if interaction:
            script = interaction["details"].get("script")
            if script:
                return web_sites.THIRD_PARTY_SCRIPTS.get(script, self._bundle(script))
        return (fault["details"].get("url") or "https://www.googletagmanager.com/gtm.js?id=GTM-W7N4K2") if fault else self._bundle()

    def _soft_navigation(self):
        pg, v = self.page, self.page["vitals"]
        pg["interaction_id"] = self.rng.randrange(10, 5000) * 7
        native = self.caps["native_soft_navigation"] and self.last_click is not None
        polyfill = self.caps["soft_navigation_polyfill"] and self.last_click is not None
        if native or polyfill:
            start = self.last_click["time"]
            end = self.t + (max(v.fcp, v.lcp) if self._has_fault("soft_navigation_slowness") else v.fcp) if native else self.t
            a = {"emb.soft_navigation.source": "performance_observer" if native else "polyfill",
                 "emb.soft_navigation.start_time": self.p(start - pg["document_zero"]),
                 "emb.soft_navigation.duration": self.p(end - start)}
            if native:
                a.update({"emb.soft_navigation.navigation_id": pg["navigation_id"],
                          "emb.soft_navigation.interaction_id": pg["interaction_id"],
                          "emb.soft_navigation.paint_time": self.p(end - pg["document_zero"]),
                          "emb.soft_navigation.presentation_time": self.p(end - pg["document_zero"] + 8)})
            elif self.caps["inp"]:
                a["emb.soft_navigation.interaction_id"] = pg["interaction_id"]
            # Correlation arrays are parallel, even when no buffered signals match the window.
            a.update({"emb.soft_navigation." + k: [] for k in ("span_ids", "span_id_types", "log_ids", "log_id_types")})
            self._span("soft", "Soft Navigation", start, end, a)
        if not native:
            pg["vitals_done"] = True
        else:
            v.ttfb = 0
            self._initial_vitals(0, 0, 0, 0)
        self.t = self.p(self.t + max(v.lcp, 50))
        self._load_frames()

    def _load_frames(self):
        pg = self.page
        if pg["nav_type"] != "soft-navigation":
            self.first_frame_seen = False
        if self.caps["loaf"]:
            mix = pg["tbd_mix"]
            band = weighted(list(mix.items()), self.rng)
            blocking = self.rng.uniform(*{"good": (0, 180), "needs-improvement": (210, 590), "poor": (620, 1100)}[band])
            fault = self._has_fault("long_task") or self._has_fault("third_party_blocking")
            if fault:
                blocking = self.rng.uniform(fault["details"].get("min_ms", 400), fault["details"].get("max_ms", 900))
            for duration in (self.rng.uniform(55, 180), 50 + blocking):
                self._frame(duration, False)

    def _frame(self, duration, interaction=False):
        if not self.caps["loaf"] or duration < 50:
            return
        first = not getattr(self, "first_frame_seen", False)
        self.first_frame_seen = True
        self.part.frames.append({"duration": duration, "work": duration * .8, "layout": duration * .2,
                                 "blocking": max(0, duration - 50), "first": first, "interaction": interaction,
                                 "script": self._slow_script()})

    def _loaf_report(self):
        frames = self.part.frames
        if not frames:
            return
        value = round(sum(f["blocking"] for f in frames if not f["first"] and not f["interaction"]))
        a = {"emb.type": "ux.web_vital", "browser.web_vital.id": f"embracev2-{int(self.t)}-{self.rng.randrange(10**12, 10**13)}",
             "browser.web_vital.name": "tbd", "browser.web_vital.value": value,
             "browser.web_vital.rating": web_vitals.rating("tbd", value), "emb.tbd.loaf_count": len(frames)}
        for attr, key in (("total_duration", "duration"), ("work_duration", "work"), ("style_and_layout_duration", "layout")):
            a["emb.tbd.loaf_" + attr] = round(sum(f[key] for f in frames))
        a["emb.tbd.loaf_longest_duration"] = round(max(f["duration"] for f in frames))
        a["emb.tbd.loaf_longest_duration_excluding_first"] = round(max((f["duration"] for f in frames if not f["first"]), default=0))
        self._log("loaf", "emb-loaf-report", a)
        scripts = {}
        for f in frames:
            row = scripts.setdefault(f["script"], {"total_duration": 0, "style_and_layout_duration": 0, "count": 0})
            row["total_duration"] += round(f["work"])
            row["style_and_layout_duration"] += round(f["layout"])
            row["count"] += 1
        self._log("loaf", "emb-loaf-scripts", {"emb.type": "ux.loaf_scripts"}, json.dumps(scripts))

    def _page_state(self):
        pg = self.page
        vw, vh = (int(x) for x in self.persona["viewport"].split("x"))
        height = max(vh, pg["def"].doc_height)
        a = {"emb.type": "emb.otel_log", "dom_state.document_height": height, "dom_state.document_width": vw,
             "dom_state.element_count": self.rng.randint(450, 2400), "dom_state.average_depth": self.rng.uniform(6, 11)}
        if not pg["above_fold"] and pg["loaded"]:
            pg["above_fold"] = True
            a.update({"dom_state.images_above_fold.count": 2 if pg["def"].lcp[2] else 0,
                      "dom_state.images_above_fold.viewport_width": vw, "dom_state.images_above_fold.viewport_height": vh,
                      "dom_state.images_above_fold.timestamp": self.p(pg["zero"] + pg["vitals"].lcp),
                      "dom_state.images_above_fold.capture_time": self.p(pg["zero"] - pg["document_zero"] + pg["vitals"].lcp)})
        self._log("dom", "dom-state", a)
        px = round((height - vh) * self.rng.choice((0, .3, .6, 1)))
        self._log("scroll", "max-scroll-depth", {"emb.type": "emb.otel_log", "max_scroll_depth.pixels": px,
            "max_scroll_depth.did_scroll": px > 0, "max_scroll_depth.percent": round(100 * (px + vh) / height),
            "max_scroll_depth.document_height": height})

    def click(self, element_html, selector=None, breadcrumb=None):
        self._lcp()
        tag_match = re.match(r"<([a-z0-9]+)", element_html)
        tag = tag_match[1] if tag_match else "button"
        label = re.sub(r"<[^>]*>", "", element_html)
        label = label[:30] + "..." if len(label) > 30 else label
        css = web_sites.slug(label)[:64] or "primary"
        selector = selector or f"{tag}.{css}"
        view = f'<{tag} class="{css}">{label}</{tag}>'
        vw, vh = (int(x) for x in self.persona["viewport"].split("x"))
        x, y = self.rng.randint(16, vw - 16), self.rng.randint(80, vh - 40)
        self.part.events.append({"name": "click", "timeUnixNano": self.ns(self.t), "droppedAttributesCount": 0,
                                 "attributes": otlp.attrs({"emb.type": "ux.tap", "view.name": view, "tap.coords": f"{x},{y}"})})
        click = {"time": self.t, "selector": selector, "tag": tag, "x": x, "y": y,
                 "duration": self.page["vitals"].inp, "html": element_html}
        self.page["clicks"].append(click)
        self.last_click = click
        if not self.page.get("first_interaction"):
            self.page["first_interaction"] = True
            self._log("first", "first-interaction", {"emb.type": "emb.otel_log", "first_interaction.interaction_type": "click" if self.persona["kind"] == "desktop" else "tap",
                "first_interaction.element_type": tag, "first_interaction.element_selector": selector,
                "first_interaction.time": self.p(self.t - self.page["document_zero"]),
                "first_interaction.x": x, "first_interaction.y": y})
        if self.rage_window and (self.t - self.rage_window[-1]["time"] > 500 or selector != self.rage_window[-1]["selector"]):
            self._flush_rage()
        self.rage_window.append(click)
        self._frame(click["duration"], True)
        if breadcrumb:
            self.breadcrumb(breadcrumb)
        self.t = self.p(self.t + self.rng.uniform(40, 220))

    def _flush_rage(self):
        if len(self.rage_window) >= 3:
            c = self.rage_window[0]
            self._log("rage", "rage-click", {"emb.type": "emb.otel_log", "rage_click.element_type": c["tag"],
                "rage_click.element_selector": c["selector"], "rage_click.x": c["x"], "rage_click.y": c["y"],
                "rage_click.count": len(self.rage_window), "rage_click.interaction_type": "click"})
        self.rage_window = []

    def breadcrumb(self, message):
        self.part.events.append({"name": "emb-breadcrumb", "timeUnixNano": self.ns(self.t), "droppedAttributesCount": 0,
                                 "attributes": otlp.attrs({"message": message})})

    def wait(self, lo_s, hi_s):
        if getattr(self, "population_settings", {}).get("model") == "varied" and lo_s >= 2 and hi_s >= 5:
            from .population import read
            read(self, max(3, lo_s), max(15, hi_s))
            self.t = self.p(self.t)
            return
        from .modeling import sample
        duration = sample(self, "reading_ms", lambda: self.rng.uniform(lo_s, hi_s) * 1000) if lo_s >= 2 and hi_s >= 5 else self.rng.uniform(lo_s, hi_s) * 1000
        self.t = self.p(self.t + duration)

    def type_text(self, chars):
        from .modeling import sample
        duration = sum(sample(self, "typing_ms", lambda: self.rng.triangular(200, 800, 300)) for _ in range(chars)) if getattr(self, "population_settings", {}).get("model") == "varied" else chars * sample(self, "typing_ms", lambda: self.rng.uniform(140, 320))
        self.t = self.p(self.t + duration)

    def login(self, user_id=None):
        self.user_id = user_id or self.persona.get("user_id") or uuid4(self.rng)
        self.storage["external_user_id"] = self.user_id

    def _page_exceptions(self):
        for f in self.page["faults"]:
            if f["id"] in self.used_faults or f["type"] not in ("js_exception", "chunk_load_failures"):
                continue
            if f["type"] == "chunk_load_failures" and self.page_count == 1:
                continue
            self.used_faults.add(f["id"])
            d = f["details"]
            family = d.get("family", "chunk_load" if f["type"] == "chunk_load_failures" else "property_undefined")
            if family == "chunk_load" and self.lob == "banking":
                family = "webpack_chunk_load"
            self.exception(family=family, handling=d.get("handling"))
            if d.get("error_log_followup"):
                self.log("error", "Unable to render the requested view")
            if d.get("rage_clicks"):
                self._rage_clicks()

    def _rage_clicks(self):
        control = (self.last_click or {}).get('html', '<button class="btn btn-primary">Retry</button>')
        selector = (self.last_click or {}).get('selector')
        for _ in range(3):
            self.click(control, selector)
        self._flush_rage()

    def exception(self, exc_type=None, message=None, handled=False, function=None, family=None, handling=None, **attrs):
        family = family or ("json_html" if handled else "property_undefined")
        ctor, name, msg, stack = web_faults.exception(family, self.caps["engine"], self.app_version,
            self._bundle(), self._bundle("vendor"), self._bundle(), str(int(web_sites.hex_id(self.page["key"], n=4), 16)))
        handling = handling or ("handled" if handled else web_faults.HANDLING.get(family, "unhandled_error"))
        self.exception_number += 1
        a = dict(attrs, **{"emb.type": "sys.exception", "exception.type": ctor, "exception.name": name,
            "exception.message": msg, "exception.stacktrace": stack, "emb.exception_cause": "",
            "emb.exception_handling": handling, "emb.exception_number": self.exception_number,
            "emb.js_file_bundle_ids": self._bundle_ids(), "emb.state": "foreground"})
        if handling != "handled":
            self.part.unhandled += 1
        self._log("logs", "emb-exception", a, msg, severity=17, text="ERROR")

    def log(self, severity, message, **attrs):
        num, text = {"info": (9, "INFO"), "warning": (13, "WARNING"), "error": (17, "ERROR")}[severity]
        a = dict(attrs, **{"emb.type": "sys.log", "emb.state": "foreground"})
        if severity != "info":
            a["emb.stacktrace.js"] = web_faults.exception("property_undefined", self.caps["engine"], self.app_version,
                                                          self._bundle(), self._bundle("vendor"))[3]
            a["emb.js_file_bundle_ids"] = self._bundle_ids()
        self.part.errors += int(severity == "error")
        self._log("logs", "emb-log", a, message, severity=num, text=text)

    def measure(self, name, start, end):
        if name in self.page["measures"]:
            return
        self.page["measures"].add(name)
        for suffix, stamp in (("start", start), ("end", end)):
            self._log("user", "emb-user-timing", {"emb.type":"ux.user_timing", "emb.user_timing.name":name+"."+suffix,
                "emb.user_timing.start_time":self.p(stamp-self.page["document_zero"]),
                "emb.user_timing.duration":0, "emb.user_timing.entry_type":"mark"}, timestamp=stamp)
        self._span("user", name, start, end, {"emb.type": "ux.user_timing", "emb.instrumentation": "user_timing",
            "emb.user_timing.entry_type": "measure", "emb.user_timing.start_time": self.p(start - self.page["document_zero"]),
            "emb.user_timing.duration": self.p(end - start)})

    @contextmanager
    def flow(self, name, **attrs):
        holder, start, page = {"failed": False}, self.t, self.page
        try:
            yield holder
        finally:
            a = dict(attrs, **holder.get("attributes", {}), **{"emb.type": "perf"})
            if holder["failed"]:
                a["emb.error_code"] = "FAILURE"
            self._span("traces", name, start, self.t, a, status=2 if holder["failed"] else 0, page=page)

    def long_task(self, duration, script=None):
        self._frame(duration)
        if script and self.part.frames:
            self.part.frames[-1]["script"] = script
        self.t = self.p(self.t + duration)

    def api(self, key, **params):
        return self.parallel((key, params))[0]

    def parallel(self, *calls):
        start = self.t
        results = [self._request(key, params, start + i * self.rng.uniform(.2, 4)) for i, (key, params) in enumerate(calls)]
        self.t = self.p(max(self.t, max(r.end for r in results)) + lat(15, 90, self.rng))
        return results

    def _request(self, key, params, start):
        if getattr(self, "pending_origin_latency", None):
            params = dict(params, _origin_latency=self.pending_origin_latency)
            self.pending_origin_latency = None
        c = simulate(self, key, params, start, self.rng)
        failed = c.status is None or c.status >= 400
        a = {"http.request.method": c.ep.method, "url.full": c.url, "http.response.status_code": c.status,
             "server.address": self.api_host, "server.port": 443, "emb.type": "perf.network_request"}
        propagated = self.traces and self.api_host in self.web["propagation_allow_list"]
        if propagated:
            a["emb.w3c_traceparent"] = c.traceparent
        if failed:
            a["error.type"] = c.transport or str(c.status)
        scope = "xhr" if self.lob == "banking" and key == "statement" else "fetch"
        self._span(scope, f"HTTP {c.ep.method}" if scope == "xhr" else c.ep.method, start, c.end, a,
                   kind=3, trace_id=c.trace_id, span_id=c.span_id, status=2 if failed else 0,
                   events=self._events([("open", start), ("send", start + .1), ("fetchStart", start + .2), ("responseEnd", c.end)])
                          if scope == "xhr" else self._events([("fetchStart", start), ("responseEnd", c.end)]))
        if propagated:
            deliver_backend(self, c, self.rng)
        self.calls.append((key, c.status, c.trace_id))
        rage = self._has_fault("rage_clicks")
        if rage and rage["id"] not in self.used_faults:
            self.used_faults.add(rage["id"])
            self.t = self.p(c.end)
            self._rage_clicks()
        if c.status is None and not hasattr(self, 'execution'):
            # fetch() rejects when the request fails, so the unhandled rejection follows the span's end.
            self.t = self.p(max(self.t, c.end) + 1)
            self.exception(family="abort" if c.transport == "cancelled" else "failed_to_fetch")
        if c.status is not None and c.status < 400:
            for name in self.page["def"].measures:
                self.measure(name, self.page["zero"], c.end)
        return Result(c.status, c.outcome, c.data, c.trace_id, start, c.end)

    def third_party(self, method, url, status=204, opaque=False):
        start = self.t
        self._span("fetch", method, start, start + self.rng.uniform(40, 160), {
            "http.request.method": method, "url.full": url, "http.response.status_code": None if opaque else status,
            "server.address": urlsplit(url).hostname, "server.port": 443, "emb.type": "perf.network_request"}, kind=3)

    def client_resource_spans(self, c):
        # Explicit diagnostic duplication uses the same client span ID as NSF would forward.
        from .limits import forwarded_service_name
        resource = otlp.attrs({"service.name": forwarded_service_name(self.app_name, "web", self.environment),
            "service.version": self.app_version, "telemetry.sdk.language": "webjs", "telemetry.sdk.name": "opentelemetry"})
        span = {"traceId": c.trace_id, "spanId": c.span_id, "name": f"{c.ep.method} {c.ep.route}", "kind": 3,
                "startTimeUnixNano": self.ns(c.start), "endTimeUnixNano": self.ns(c.end), "flags": 257,
                "status": {"code": 2 if c.status is None or c.status >= 400 else 0},
                "attributes": otlp.attrs({"http.request.method": c.ep.method, "url.full": c.url,
                    "http.response.status_code": c.status, "server.address": self.api_host})}
        return {"resource": {"attributes": resource}, "scopeSpans": [{"scope": SDK_SCOPES["fetch"], "spans": [span]}]}

    def _deliver(self, due, target, kind, payload):
        policy = self.web.get("origin_header", {})
        headers = {"User-Agent": self.persona["ua"]}
        if policy.get("policy", "none") != "none":
            headers["Origin"] = policy.get("value") if policy.get("policy") == "allowed" else self.origin
        self.deliveries.append({"due": due, "target": target, "kind": kind, "payload": payload,
                               "app_id": self.app_id, "device_id": self.device_id, "ua": self.persona["ua"],
                               "origin": self.origin, "headers": headers})

    def _flush_logs(self, hidden=False):
        batches = {}
        for emitted, scope, record in self.pending_logs:
            due = min(self.t, self.part.start + (int((emitted - self.part.start) // LOG_BATCH_MS) + 1) * LOG_BATCH_MS)
            batches.setdefault(due, {}).setdefault(scope, []).append(record)
        for due, scopes in sorted(batches.items()):
            self._deliver(due, "embrace", "logs", {"resourceLogs": [{"resource": self.resource, "scopeLogs": [
                {"scope": SDK_SCOPES[k], "logRecords": records} for k, records in scopes.items()]}]})
        self.pending_logs = []

    def background(self, lo_s=4, hi_s=12):
        self._finish_vitals()
        self._end_part("background")
        self.engaged = False
        self.wait(lo_s, hi_s)
        self.engaged = True
        self._start_part("foreground")

    def leave(self):
        if not self.ended:
            self._finish_vitals()
            self._end_part("background")
            self.ended = True

    def logout(self):
        if not self.ended:
            self._finish_vitals()
            self._end_part("user_session_ended", final="manual")
            self.storage["external_user_id"] = None
            self.ended = True

    def finish(self):
        self.leave()
        self.storage.update(user_session_number=self.user_session_number, session_part_number=self.part_number,
                            previous_user_session_id=self.user_session_id, exception_number=self.exception_number)
        self.end_ms = self.t
        return self.deliveries
