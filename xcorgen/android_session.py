"""Simulated Android app session that produces Embrace Android SDK envelopes.

The Android SDK does not send OTLP. It posts gzipped Embrace envelopes:
{"resource": {...}, "metadata": {...}, "version": "0.1.0", "type": "spans"|"logs", "data": {...}}
to https://a-<appId>.data.emb-api.com/v2/spans and /v2/logs. Span and log
shapes, names and attribute keys follow the SDK source and its integration
test golden files (embrace-android-sdk/src/integrationTest/resources).

Each OkHttp request carries a W3C traceparent recorded as emb.w3c_traceparent;
the backend trace continues it, and Embrace's network span forwarding sends
the client span to Chronosphere as the trace root.
"""

import hashlib
import json
import random
import re
from contextlib import contextmanager

from . import otlp
from .calls import Result, deliver_backend, release_backend, simulate
from .catalog import NETWORK_FAILURES
from .profiles import ENTRY_POINTS, SHARED_ANDROID_APP
from .util import emb_id, hex_id, lat, uuid4, weighted

# Business-unit properties that clash with the app-wide ones in a shared app.
SHARED_REPLACED_PROPERTIES = {"currency", "locale", "market", "loyalty_tier", "loyalty.tier"}

SDK_SIMPLE_VERSION = 53
ENVELOPE_VERSION = "0.1.0"
LOG_BATCH_MS = 2000


class SessionEnded(Exception):
    """Raised when the app crashes so the journey stops where the process died."""


def nanos(ms):
    return int(round(ms * 1_000_000))


def kv(d):
    """dict -> Embrace attribute list; the Android SDK serializes every value as a string."""
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        out.append({"key": k, "value": str(v)})
    return out


def _snake(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def compose_label(html):
    """Compose taps report the element's click label, content description or text."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html).replace("&amp;", "&")).strip()
    if text:
        return text
    m = re.search(r'(?:aria-label|placeholder)="([^"]+)"', html)
    return m.group(1) if m else "Unlabeled Compose element"


def view_name(html):
    """Resource entry name of a View-based widget, as getResourceName(view.id) gives it."""
    m = re.search(r'data-testid="([^"]+)"', html) or re.search(r'\bid="([^"]+)"', html)
    if m:
        base = _snake(m.group(1))
    else:
        text = re.sub(r"<[^>]+>", " ", html).replace("&amp;", "and")
        cls = re.search(r'class="([^"]+)"', html)
        if cls and "nav" in cls.group(1):
            return "nav_" + _snake(text)[:24]
        if cls and any(w in cls.group(1) for w in ("card", "tile", "option", "link", "chip", "tab")):
            base = _snake(cls.group(1).split()[0].replace("__", "_"))
        else:
            base = _snake(text)[:28] or "view"
    tag = re.match(r"<(\w+)", html)
    kind = {"button": "button", "a": "row", "input": "input", "li": "item", "label": "option"}.get(
        tag.group(1) if tag else "", "view")
    return base if base.endswith(kind) else f"{base}_{kind}"


class Part:
    def __init__(self, index, number, start, start_type, cold, rng=random):
        self.id = emb_id(rng)
        self.index, self.number, self.start, self.start_type, self.cold = index, number, start, start_type, cold
        self.end = None
        self.spans, self.events, self.logs = [], [], []
        self.trace_id, self.span_id = hex_id(16, rng), hex_id(8, rng)
        self.error_logs = 0
        self.crash_id = None
        self.end_type = "state"


def pick_version(versions, persona, rng=random):
    """Installed app version: a returning device keeps its version, a new one follows adoption shares."""
    installed = next((x for x in versions if x[0] == persona.get("installed_version")), None)
    return installed[:3] if installed else weighted([((v, c, s), w) for v, c, s, w in versions], rng)


class AndroidSession:
    def __init__(self, profile, persona, app_id, start_ms, incident=None, emit_client_spans=False, rng=random,
                 shared=False, app=None, version=None):
        self.profile, self.persona, self.app_id, self.rng = profile, persona, app_id, rng
        self.incident, self.emit_client_spans = incident, emit_client_spans
        # Whether network requests carry a forwarded traceparent and produce backend traces
        # (end-to-end mode, and only for activity Cortex XCOR can still ingest).
        self.traces = True
        if shared:
            self.app = dict(profile.android, **SHARED_ANDROID_APP)
            self.api_host = profile.android["shared_api_host"]
        else:
            self.app = profile.android
            self.api_host = profile.api_host
        if app:
            self.app = dict(self.app, **app)
            self.api_host = self.app.get("api_host", self.api_host)
        self.t = self.start_ms = start_ms
        self.app_version, self.version_code, self.sdk_version = version or pick_version(self.app["versions"], persona, rng)
        if persona.get("installed_version") != self.app_version:
            persona["installed_version"] = self.app_version
            persona["version_starts"] = 0
        persona["version_starts"] = persona.get("version_starts", 0) + 1
        self.process_id = hex_id(8, rng)
        self.user_session_id = emb_id(rng)
        self.previous_user_session_id = persona["previous_user_session_id"]
        self.user_session_number = persona["user_session_number"] + 1
        self.part_number = persona["session_part_number"]
        self.user_id = None
        self.properties = self._shared_properties() if shared else profile.properties(persona, rng)
        self.connected_hosts = set()
        self.calls, self.deliveries = [], []
        self.usage = {}
        self.part = self.screen = None
        self.activity_start = None
        self.ended = False
        self.exception_number = 0
        self.client_user_agent = self.app["user_agent"].format(
            version=self.app_version, os=persona["os_version"], model=persona["model"])
        self.resource = self._resource()
        if shared and persona.get("user_id"):
            self.login()  # one account across the app: signed-in users stay signed in

    def _shared_properties(self):
        market = weighted(self.app["markets"], self.rng) if self.app.get("markets") else "US"
        props = {"business_unit": self.app["business_unit"], "market": market,
                 "membership_tier": self.persona["traits"].get("membership", "standard"),
                 "entry_point": weighted(self.app.get("entry_points") or ENTRY_POINTS, self.rng)}
        props.update({k: v for k, v in self.profile.properties(self.persona, self.rng).items()
                      if k not in SHARED_REPLACED_PROPERTIES})
        return props

    # --- envelope identity -----------------------------------------------------

    def _resource(self):
        p, a = self.persona, self.app
        # Deterministic SDK build label, never an authentication or integrity check.
        build_id = hashlib.md5(f"{a['package']}:{self.app_version}".encode(), usedforsecurity=False).hexdigest().upper()
        r = {
            "app_version": self.app_version, "app_framework": 1, "build_id": build_id,
            "app_ecosystem_id": a["package"], "build_type": "release", "build_flavor": "production",
            "environment": "prod", "bundle_version": self.version_code, "sdk_version": self.sdk_version,
            "sdk_simple_version": SDK_SIMPLE_VERSION, "device_manufacturer": p["manufacturer"],
            "device_model": p["model"], "device_architecture": "arm64-v8a", "jailbroken": False,
            "disk_total_capacity": p["disk_total"], "os_type": "linux", "os_name": "android",
            "os_version": p["os_version"], "os_code": p["os_code"], "screen_resolution": p["screen"],
            "num_cores": 8, "uses_emmc_storage": False,
        }
        if p.get("soc"):
            r["device_soc_model"] = p["soc"]
        r["otel_sdk_mode"] = "compat"
        return r

    def _metadata(self):
        m = {}
        if self.user_id:
            m["user_id"] = self.user_id
        m["personas"] = ["first_day"] if self.user_session_number <= 2 else []
        m["timezone_description"] = self.persona["timezone"]
        m["locale"] = "en_US"
        return m

    def _envelope(self, kind, data):
        return {"resource": self.resource, "metadata": self._metadata(), "version": ENVELOPE_VERSION,
                "type": kind, "data": data}

    def _deliver(self, due, target, kind, payload):
        d = {"due": due, "target": target, "kind": kind, "payload": payload, "app_id": self.app_id,
             "device_id": self.persona["device_id"], "ua": self.client_user_agent, "origin": None}
        if target == "embrace":
            d["fmt"] = "envelope"
            types = "ux.session" if kind == "spans" else ",".join(sorted({
                next(a["value"] for a in r["attributes"] if a["key"] == "emb.type") for r in payload["data"]["logs"]}))
            d["headers"] = {"Accept": "application/json", "User-Agent": f"Embrace/a/{self.sdk_version}",
                            "X-EM-PAYLOAD-TYPES": types}
        self.deliveries.append(d)

    def _use(self, api):
        self.usage[api] = self.usage.get(api, 0) + 1

    # --- common attributes -------------------------------------------------------

    def _span_common(self):
        return {"emb.process_identifier": self.process_id, "emb.session_part_id": self.part.id,
                "emb.user_session_id": self.user_session_id, "user.id": self.user_id}

    def _log_common(self):
        a = {"emb.session_part_id": self.part.id, "emb.user_session_id": self.user_session_id,
             "emb.state": "foreground", "user.id": self.user_id}
        a.update({f"emb.properties.{k}": v for k, v in self.properties.items()})
        a.update({"emb.state.network": self.persona["network"],
                  "emb.state.screen-automatic": self.screen["route"] if self.screen else "Initializing",
                  "log.record.uid": emb_id(self.rng)})
        return a

    def _span(self, name, start, end, attrs, status="Unset", trace_id=None, span_id=None, parent=None, events=None):
        a = dict(attrs)
        a.update(self._span_common())
        span = {"trace_id": trace_id or hex_id(16, self.rng), "span_id": span_id or hex_id(8, self.rng),
                "parent_span_id": parent or "0000000000000000", "name": name,
                "start_time_unix_nano": nanos(start), "end_time_unix_nano": nanos(end), "status": status,
                "events": events or [], "attributes": kv(a)}
        self.part.spans.append(span)
        return span

    # --- time ----------------------------------------------------------------------

    def wait(self, lo_s, hi_s):
        if getattr(self, "population_settings", {}).get("model") == "varied" and lo_s >= 2 and hi_s >= 5:
            from .population import read
            return read(self, max(3, lo_s), max(15, hi_s))
        from .modeling import sample
        self.t += sample(self, "reading_ms", lambda: self.rng.uniform(lo_s, hi_s) * 1000) if lo_s >= 2 and hi_s >= 5 else self.rng.uniform(lo_s, hi_s) * 1000

    def _think(self):
        self.t += self.rng.uniform(60, 260)

    def type_text(self, chars):
        from .modeling import sample
        if getattr(self, "population_settings", {}).get("model") == "varied":
            self.t += sum(sample(self, "typing_ms", lambda: self.rng.triangular(200, 800, 300)) for _ in range(chars))
        else:
            self.t += chars * sample(self, "typing_ms", lambda: self.rng.uniform(170, 360))

    # --- lifecycle -------------------------------------------------------------------

    def _start_part(self, start_type):
        index = 1 if self.part is None else self.part.index + 1
        self.part_number += 1
        cold = index == 1 and (self.forced_cold_start if hasattr(self, 'forced_cold_start') else
            self.rng.random() < 0.78 or bool(getattr(getattr(self, "plan", None), "slow_startup", None)))
        previous = self.part
        self.part = Part(index, self.part_number, self.t, start_type, cold, self.rng)
        if index == 1:
            self._startup(cold)
        else:
            self._foreground_return(self.t - (previous.end or self.t))

    def _foreground_return(self, background_ms):
        self._ui_load(background_ms)

    def _end_part(self, end_type="state", final=None):
        part = self.part
        self._end_screen()
        self._end_activity()
        part.end, part.end_type = self.t, end_type
        a = {}
        if part.cold and part.index == 1:
            a["emb.app.version_startup_counter"] = self.persona["version_starts"]
        a.update({"emb.clean_exit": True,
             "emb.clock_gnss_drift": self.rng.randint(-40, 40),
             "emb.clock_network_drift": self.rng.randint(-300, 300),
             "emb.cold_start": part.cold and part.index == 1})
        if part.crash_id:
            a["emb.crash_id"] = part.crash_id
        used = int(self.rng.uniform(140, 420) * 1e6)
        a.update({
            "emb.disk_free_bytes": int(self.persona["disk_total"] * self.rng.uniform(0.18, 0.55)),
            "emb.error_log_count": part.error_logs,
            "emb.is_emulator": False, "emb.kotlin_on_classpath": "2.2.21", "emb.okhttp3": True,
            "emb.okhttp3_on_classpath": "4.12.0", "emb.process_identifier": self.process_id,
            "emb.session_end_type": end_type, "emb.session_part_id": part.id,
            "emb.session_part_number": part.number, "emb.session_start_type": part.start_type,
            "emb.startup_duration": self.startup_ms if part.index == 1 and part.cold else 0,
            "emb.state": "foreground", "emb.storage.available": int(self.persona["disk_total"] * 0.31),
            "emb.storage.used": used, "emb.terminated": False, "emb.type": "ux.session"})
        if part.crash_id:
            a["emb.error_code"] = "failure"
            a["emb.termination_cause"] = "crash"
        a.update({f"emb.properties.{k}": v for k, v in self.properties.items()})
        a.update({f"emb.usage.{k}": v for k, v in sorted(self.usage.items())})
        a.update({"emb.user_session_id": self.user_session_id,
                  "emb.user_session_inactivity_timeout_seconds": 1800,
                  "emb.user_session_max_duration_seconds": 43200,
                  "emb.user_session_number": self.user_session_number,
                  "emb.user_session_part_index": part.index,
                  "emb.user_session_start_ts": int(self.start_ms)})
        session = {"trace_id": part.trace_id, "span_id": part.span_id, "parent_span_id": "0000000000000000",
                   "name": "emb-session", "start_time_unix_nano": nanos(part.start),
                   "end_time_unix_nano": nanos(part.end), "status": "Error" if part.crash_id else "Unset",
                   "events": sorted(part.events, key=lambda e: e["time_unix_nano"]), "attributes": kv(a)}
        net = {"trace_id": hex_id(16, self.rng), "span_id": hex_id(8, self.rng), "parent_span_id": "0000000000000000",
               "name": "emb-network-status", "start_time_unix_nano": nanos(part.start),
               "end_time_unix_nano": nanos(part.end), "status": "Unset", "events": [],
               "attributes": kv(dict({"emb.type": "sys.network_status", "network": self.persona["network"]},
                                     **self._span_common()))}
        spans = [session] + part.spans + [net]
        self._deliver(part.end + self.rng.uniform(150, 1200), "embrace", "spans",
                      self._envelope("spans", {"spans": spans, "span_snapshots": []}))
        release_backend(self, part.end, self.rng)
        self._flush_logs(part)

    def _flush_logs(self, part):
        batches = {}
        for t, rec in part.logs:
            due = min(part.end, part.start + (int((t - part.start) // LOG_BATCH_MS) + 1) * LOG_BATCH_MS)
            batches.setdefault(due, []).append(rec)
        for due, recs in sorted(batches.items()):
            self._deliver(due + self.rng.uniform(30, 600), "embrace", "logs", self._envelope("logs", {"logs": recs}))

    def background(self, lo_s=6, hi_s=90):
        """User leaves the app briefly; the part ends and a new one starts when they come back."""
        self._end_part("state")
        self.t += self.rng.uniform(lo_s, hi_s) * 1000
        self._start_part("state")
        self._screen(self.screen_def_key, self.screen_label)

    def leave(self):
        if self.ended:
            return
        self._end_part("state")
        self.ended = True

    def logout(self):
        """App signs the user out and calls Embrace.endSession(); the next session starts fresh."""
        if self.ended:
            return
        self._use("end_session")
        self._end_part("manual")
        self.ended = True

    def login(self, user_id=None):
        if not (user_id or self.persona.get("user_id")):
            self.persona["user_id"] = uuid4(self.rng)  # signed up or signed in for the first time
        self.user_id = user_id or self.persona["user_id"]
        self._use("set_user_identifier")

    # --- startup -----------------------------------------------------------------------

    def _cpu_slowdown(self):
        if self.persona.get("population_model"):
            return self.persona["cpu_factor"]
        mid = self.persona["model"]
        return 1.0 if any(x in mid for x in ("SM-S9", "Pixel 9", "Pixel 10", "SM-F9", "CPH")) else 1.6

    def _local_activity_name(self):
        """Activity.getLocalClassName(): the class name without the application package prefix."""
        act, pkg = self.app["activity"], self.app["package"]
        return act[len(pkg) + 1:] if act.startswith(pkg + ".") else act

    def _launch_reason(self, cold):
        """ProcessInfoImpl (API 35+): the ApplicationStartInfo reason that created this process."""
        if int(self.persona.get("os_code") or 0) < 35:
            return None
        if not cold:
            # A warm start reuses a process the system created earlier without UI.
            return weighted([("job", 45), ("push", 30), ("broadcast", 15), ("service", 10)], self.rng)
        entry = self.properties.get("entry_point")
        if entry == "push_notification":
            return "push"
        if entry in ("deep_link", "widget"):
            return "start_activity"
        return weighted([("launcher", 88), ("launcher_recents", 12)], self.rng)

    def _sdk_init_attributes(self, sdk_ms):
        """embrace-init attributes in 9.3.1: section durations, init resource usage, init environment."""
        rng, ms = self.rng, max(12, int(sdk_ms))
        modules = int(ms * rng.uniform(.38, .5))
        sections = {"modules-init": modules, "persisted-config-load": max(1, int(modules * rng.uniform(.06, .14))),
                    "prefs-first-read": max(1, int(modules * rng.uniform(.08, .22))),
                    "span-service-init": max(1, int(modules * rng.uniform(.05, .12))),
                    "otel-tracer-init": max(1, int(modules * rng.uniform(.04, .1))),
                    "essential-service-init": max(1, int(modules * rng.uniform(.08, .18))),
                    "delivery-init": max(1, int(modules * rng.uniform(.04, .1))),
                    "payload-source-init": max(1, int(modules * rng.uniform(.03, .08))),
                    "post-init": int(ms * rng.uniform(.04, .1)), "load-instrumentation": int(ms * rng.uniform(.14, .24)),
                    "post-services-setup": int(ms * rng.uniform(.08, .16))}
        a = {f"{k}-duration-ms": v for k, v in sections.items()}
        a.update({"init-cpu-pct": rng.randint(52, 96), "init-run-delay-pct": rng.randint(2, 31),
                  "init-disk-read-kb": rng.randint(36, 940), "init-gc-count": rng.choice((0, 0, 0, 1, 1, 2))})
        if rng.random() < .05:
            a["thermal-status"] = rng.choice(("light", "light", "moderate"))
        a["thermal-headroom-pct"] = "-2" if rng.random() < .35 else rng.randint(18, 62)
        days_installed = self.persona.setdefault("install_age_days", rng.randint(3, 900))
        days_updated = min(days_installed, self.persona.get("version_starts", 1) * rng.uniform(.2, 1.4))
        a.update({"seconds-since-install": int(days_installed * 86400 + rng.randint(0, 86399)),
                  "seconds-since-update": int(days_updated * 86400 + rng.randint(0, 3599)),
                  "mem-available-pct": rng.randint(18, 61)})
        if a["mem-available-pct"] < 22 and rng.random() < .4:
            a["low-memory"] = "true"
        recent = self.persona.get("version_starts", 1) <= 2
        a.update({"seconds-since-boot": rng.randint(1800, 1_900_000), "prefs-file-bytes": rng.randint(1800, 46000),
                  "art-compiler-filter": "verify" if recent and rng.random() < .55 else "speed-profile"})
        a["app-image-at-init"] = a["art-compiler-filter"] == "speed-profile" and rng.random() < .9
        return a

    def _startup(self, cold):
        """AppStartupTraceEmitter (9.3.1). Cold: process-init [process start, Application.onCreate end] with
        embrace-init inside it, activity-init-delay [onCreate end, first activity], activity-init, activity-render
        (API 29-30 and 33+; activity-first-draw on 31-32). Warm traces start at the first activity and carry only
        activity-init and activity-render. Times come from the SDK clock in whole milliseconds."""
        rng, slow = self.rng, self._cpu_slowdown()
        t0 = int(self.t)
        tid, root = hex_id(16, rng), hex_id(8, rng)
        api = int(self.persona.get("os_code") or 35)
        render_name = "emb-activity-first-draw" if api in (31, 32) else "emb-activity-render"
        create = lat(90, 260, rng) * slow
        render = lat(120, 420, rng) * slow
        if cold:
            fork = lat(45, 160, rng) * slow       # zygote specialization, bindApplication, content providers
            pre_sdk = lat(3, 22, rng) * slow      # Application.onCreate work before Embrace.start
            sdk = lat(28, 70, rng) * slow         # Embrace.start
            post_sdk = lat(30, 150, rng) * slow   # dependency graph, remote config, work scheduling
            delay = lat(8, 60, rng) * slow
            regression = getattr(getattr(self, "plan", None), "slow_startup", None)
            if regression:
                # The regressed phase absorbs the extra time: application init (process start and
                # Application.onCreate), SDK init, or first render of the launch activity.
                total = rng.uniform(regression.get("min_ms", 4200), regression.get("max_ms", 7500))
                extra = max(0.0, total - (fork + pre_sdk + sdk + post_sdk + delay + create + render))
                phase = regression.get("phase", "application_init")
                if phase == "sdk_init":
                    sdk += extra
                elif phase == "first_render":
                    render += extra * .7
                    create += extra * .3
                else:
                    post_sdk += extra
            sdk_start = round(t0 + fork + pre_sdk)
            sdk_end = round(sdk_start + sdk)
            app_end = round(sdk_end + post_sdk)
            act_start = round(app_end + delay)
        else:
            act_start = t0
        act_end = round(act_start + create)
        end = round(act_end + render)
        root_attrs = {"emb.type": "perf", "emb.startup_activity": self._local_activity_name(),
                      "emb.startup_launch_reason": self._launch_reason(cold)}
        name = "emb-app-startup-cold" if cold else "emb-app-startup-warm"
        self._span(name, t0, end, root_attrs, trace_id=tid, span_id=root)
        if cold:
            self._span("emb-process-init", t0, app_end, {"emb.type": "perf"}, trace_id=tid, parent=root)
            init_attrs = {"emb.type": "perf", "emb.app.version_startup_counter": self.persona["version_starts"]}
            init_attrs.update(self._sdk_init_attributes(sdk_end - sdk_start))
            self._span("emb-embrace-init", sdk_start, sdk_end, init_attrs, trace_id=tid, parent=root)
            self._span("emb-activity-init-delay", app_end, act_start, {"emb.type": "perf"}, trace_id=tid, parent=root)
        self._span("emb-activity-init", act_start, act_end, {"emb.type": "perf"}, trace_id=tid, parent=root)
        self._span(render_name, act_end, end, {"emb.type": "perf"}, trace_id=tid, parent=root)
        self.startup_ms = int(end - t0)
        self.sdk_startup = int(sdk_end - sdk_start) if cold else int(lat(28, 70, rng) * slow)
        self.t = end + rng.uniform(30, 200)

    def _ui_load(self, after_background_ms):
        """UiLoadTraceEmitter (9.3.1, trackRender off): returning to the app restarts the existing activity (hot);
        an activity the system destroyed in the background is recreated (cold). The trace ends at
        onActivityPostResumed; lifecycle stages are children."""
        rng, slow = self.rng, self._cpu_slowdown()
        budget = slow > 1.3
        recreated = after_background_ms > 20000 and rng.random() < (.3 if budget else .14)
        act = self._local_activity_name()
        tid, root = hex_id(16, rng), hex_id(8, rng)
        t = int(self.t)
        stages = []
        if recreated:
            stages.append(("create", t, round(t + lat(60, 210, rng) * slow)))
            t = stages[-1][2] + rng.randint(0, 2)
        stages.append(("start", t, round(t + lat(4, 36, rng) * slow)))
        t = stages[-1][2] + rng.randint(0, 2)
        stages.append(("resume", t, round(t + lat(3, 28, rng) * slow)))
        end = stages[-1][2]
        kind = "cold" if recreated else "hot"
        for stage, start, stop in stages:
            self._span(f"emb-{act}-{stage}", start, stop, {"emb.type": "perf"}, trace_id=tid, parent=root)
        self._span(f"emb-{act}-{kind}-time-to-initial-display", stages[0][1], end, {"emb.type": "perf.ui_load"},
                   trace_id=tid, span_id=root)
        self.t = end + rng.uniform(4, 30)

    # --- screens -----------------------------------------------------------------------

    def page_view(self, key, **params):
        if self.part is None:
            self._start_part("state")
        elif not hasattr(self, "plan") and self.rng.random() < 0.07:
            self._end_screen()
            self.screen_def_key, self.screen_label = key, self.profile.pages[key].label(params)
            self._end_part("state")
            self.t += self.rng.uniform(8, 90) * 1000
            self._start_part("state")
        self._screen(key, self.profile.pages[key].label(params))
        self.t += lat(120, 420, self.rng)

    def _screen(self, key, label):
        self._end_screen()
        self.screen_def_key, self.screen_label = key, label
        name = self.app["screens"][key]
        self._use("start_view")
        route = re.sub(r"(?<!^)(?=[A-Z])", "_", name.replace("Fragment", "")).lower()
        self.screen = {"name": name, "route": route, "start": self.t}
        if self.activity_start is None:
            self.activity_start = self.t

    def _end_screen(self):
        if not self.screen or self.screen.get("closed"):
            return
        self._use("end_view")
        self._span("emb-screen-view", self.screen["start"], self.t,
                   {"emb.type": "ux.view", "view.name": self.screen["name"], "emb.manual_instrumentation": True})
        self.screen["closed"] = True

    def _end_activity(self):
        if self.activity_start is not None:
            self._span("emb-screen-view", self.activity_start, self.t,
                       {"emb.type": "ux.view", "view.name": self.app["activity"]})
            self.activity_start = None

    # --- interactions ----------------------------------------------------------------------

    def click(self, element_html, selector=None, breadcrumb=None):
        w, h = (int(x) for x in self.persona["screen"].split("x"))
        if self.app.get("ui") == "views":
            name = f"{self.app['package']}:id/{view_name(element_html)}"
            # View-based taps report the view's position within its parent.
            x, y = self.rng.choice([0, 0, 48, 64, 96]), self.rng.randint(0, int(h * 0.7))
            tap_type = "long_press" if self.rng.random() < 0.02 else "tap"
        else:
            name = compose_label(element_html)
            x, y = self.rng.randint(40, w - 40), self.rng.randint(int(h * 0.12), int(h * 0.94))
            tap_type = "tap"
        self.part.events.append({"name": "emb-ui-tap", "time_unix_nano": nanos(self.t), "attributes": kv({
            "view.name": name, "tap.type": tap_type, "tap.coords": f"{x},{y}", "emb.type": "ux.tap"})})
        if breadcrumb:
            self.breadcrumb(breadcrumb)
        self._think()

    def breadcrumb(self, message):
        if self.ended:
            raise SessionEnded()
        count = sum(e["name"] == "emb-breadcrumb" for e in self.part.events)
        if count >= 75:
            raise ValueError("Breadcrumb budget exceeded; split the journey at a meaningful lifecycle boundary")
        self._use("add_breadcrumb")
        self.part.events.append({"name": "emb-breadcrumb", "time_unix_nano": nanos(self.t), "attributes": kv({
            "message": message[:253] + "..." if len(message) >= 256 else message, "emb.type": "sys.breadcrumb"})})

    # --- logs ------------------------------------------------------------------------------

    def log(self, severity, message, **attrs):
        sev = {"info": (9, "INFO"), "warning": (13, "WARNING"), "error": (17, "ERROR")}[severity]
        self._use({"info": "log_message", "warning": "log_message", "error": "log_message"}[severity])
        a = dict(attrs)
        a.update({"emb.type": "sys.log"})
        a.update(self._log_common())
        if severity == "error":
            self.part.error_logs += 1
        self.part.logs.append((self.t, {"time_unix_nano": nanos(self.t), "severity_number": sev[0],
                                        "severity_text": sev[1], "body": message, "attributes": kv(a)}))

    def exception(self, exc_type, message, handled=False, function="anonymous", **attrs):
        """JS-style exception from a journey, raised as the equivalent Kotlin failure on Android."""
        jtype, jmsg, frames = self._kotlin_exception(message, function)
        self.exception_number += 1
        if handled:
            a = dict(attrs)
            a.update({"emb.type": "sys.exception", "emb.exception_handling": "handled",
                      "emb.private.send_mode": "IMMEDIATE", "exception.type": jtype.rsplit(".", 1)[-1],
                      "exception.message": jmsg, "exception.stacktrace": json.dumps(frames)})
            a.update(self._log_common())
            self._use("log_exception")
            self.part.logs.append((self.t, {"time_unix_nano": nanos(self.t), "severity_number": 17,
                                            "severity_text": "ERROR", "body": jmsg, "attributes": kv(a)}))
            return
        self._crash(jtype, jmsg, frames)

    def _kotlin_exception(self, message, function):
        pkg = self.app["package"]
        field = re.search(r"reading '(\w+)'", message)
        getter = "get" + (field.group(1)[0].upper() + field.group(1)[1:] if field else "Value")
        feature = self.screen["name"].replace("Fragment", "")
        model = f"{pkg}.{feature.lower()}.{feature}Result"
        jtype = "java.lang.NullPointerException"
        jmsg = (f"Attempt to invoke virtual method 'java.lang.String {model}.{getter}()' on a null object reference")
        vm = f"{pkg}.{feature.lower()}.{feature}ViewModel"
        frames = [f"{vm}.{function}({feature}ViewModel.kt:{self.rng.randint(80, 240)})",
                  f"{vm}.access${function}({feature}ViewModel.kt:{self.rng.randint(30, 79)})",
                  f"{vm}${function}$1.invokeSuspend({feature}ViewModel.kt:{self.rng.randint(80, 240)})",
                  "kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith(ContinuationImpl.kt:33)",
                  "kotlinx.coroutines.DispatchedTask.run(DispatchedTask.kt:100)",
                  "android.os.Handler.handleCallback(Handler.java:995)",
                  "android.os.Handler.dispatchMessage(Handler.java:103)",
                  "android.os.Looper.loopOnce(Looper.java:248)",
                  "android.os.Looper.loop(Looper.java:338)",
                  "android.app.ActivityThread.main(ActivityThread.java:9067)"]
        return jtype, jmsg, frames

    def _crash(self, jtype, jmsg, frames):
        crash_id = emb_id(self.rng)
        self.part.crash_id = crash_id
        self.crash_evidence = {"record_id": crash_id, "part_id": self.part.id, "time_ms": self.t,
                               "exception_type": jtype, "message": jmsg, "frames": frames}
        cause = [{"n": jtype, "m": jmsg, "tt": frames, "length": len(frames)}]
        threads = [{"threadId": 2, "state": "WAITING", "n": "FinalizerDaemon", "p": 5, "fc": 4,
                    "tt": ["java.lang.Object.wait(Native Method)", "java.lang.Object.wait(Object.java:405)",
                           "java.lang.ref.ReferenceQueue.remove(ReferenceQueue.java:207)",
                           "java.lang.Daemons$FinalizerDaemon.runInternal(Daemons.java:324)"]},
                   {"threadId": self.rng.randint(40, 90), "state": "TIMED_WAITING", "n": "OkHttp TaskRunner", "p": 5,
                    "fc": 3, "tt": ["jdk.internal.misc.Unsafe.park(Native Method)",
                                    "java.util.concurrent.locks.LockSupport.parkNanos(LockSupport.java:252)",
                                    "okhttp3.internal.concurrent.TaskRunner$RealBackend.coordinatorWait(TaskRunner.kt:320)"]}]
        a = {"emb.android.crash.exception_cause": json.dumps(cause), "emb.android.crash_number": 1,
             "emb.android.threads": json.dumps(threads), "emb.private.send_mode": "DEFER", "emb.type": "sys.android.crash",
             "exception.message": jmsg, "exception.stacktrace": json.dumps(frames), "exception.type": jtype}
        a.update(self._log_common())
        a["log.record.uid"] = crash_id
        self.part.logs.append((self.t, {"time_unix_nano": nanos(self.t), "severity_number": 17,
                                        "severity_text": "ERROR", "body": "", "attributes": kv(a)}))
        self._end_part("state")
        self.ended = True
        raise SessionEnded()

    # --- custom spans -------------------------------------------------------------------------

    @contextmanager
    def flow(self, name, **attrs):
        """Custom span (Embrace.startSpan) around a user-facing operation."""
        holder = {"failed": False, "trace_id": hex_id(16, self.rng), "span_id": hex_id(8, self.rng)}
        start = self.t
        yield holder
        self._use("start_span")
        a = dict(attrs)
        a.update(holder.get("attributes", {}))
        if holder["failed"]:
            a["emb.error_code"] = "failure"
        a["emb.type"] = "perf"
        self._span(name, start, self.t, a, status="Error" if holder["failed"] else "Unset",
                   trace_id=holder["trace_id"], span_id=holder["span_id"])

    def child_span(self, parent, name, start, end, failed=False, attrs=None):
        """Embrace.startSpan(name, parent): app-defined work inside a business span, in the parent's trace."""
        self._use("start_span")
        a = dict(attrs or {})
        if failed:
            a["emb.error_code"] = "failure"
        a["emb.type"] = "perf"
        self._span(name, start, end, a, status="Error" if failed else "Unset",
                   trace_id=parent["trace_id"], parent=parent["span_id"])

    # --- network --------------------------------------------------------------------------------

    def api(self, key, **params):
        return self.parallel((key, params))[0]

    def parallel(self, *calls):
        start = self.t
        results = [self._request(key, params, start + i * self.rng.uniform(0.5, 6)) for i, (key, params) in enumerate(calls)]
        self.t = max(r.end for r in results) + lat(25, 140, self.rng)
        return results

    def _request(self, key, params, start):
        c = simulate(self, key, params, start, self.rng)
        url = c.url.split("?")[0]
        path = url.split(self.api_host, 1)[1] or ""
        a = {"emb.type": "perf.network_request", "url.full": url, "http.request.method": c.ep.method}
        if self.traces:
            # NetworkRequestDataSourceImpl sets both only for domains with network span forwarding.
            a.update({"emb.w3c_traceparent": c.traceparent, "emb.forward_telemetry": True})
        a.update({"http.response.status_code": c.status, "http.request.body.size": c.request_bytes,
                  "http.response.body.size": None if c.transport else c.response_bytes,
                  "user_agent.name": "okhttp", "user_agent.version": "4.12.0"})
        failed = c.status is None or c.status >= 400
        if c.transport:
            # OkHttpDataSource: error.type is the exception's canonical class name, exception.message its message.
            exc, message, _ = NETWORK_FAILURES[c.transport]
            a["error.type"], a["exception.message"] = exc, message.format(host=self.api_host)
        if failed:
            a["emb.error_code"] = "failure"
        self._span(f"emb-{c.ep.method} {path}", start, c.end, a, status="Error" if failed else "Unset",
                   trace_id=c.trace_id, span_id=c.span_id)
        if self.traces:
            deliver_backend(self, c, self.rng)
        self.calls.append((key, c.status, c.trace_id))
        return Result(c.status, c.outcome, c.data, c.trace_id, start, c.end)

    def client_resource_spans(self, c):
        """Client span in Chronosphere for setups without network span forwarding."""
        res = otlp.attrs({"service.name": f"{self.app['app_name'].lower()}-android", "service.version": self.app_version,
                          "deployment.environment.name": "production", "telemetry.sdk.name": "opentelemetry",
                          "telemetry.sdk.language": "java", "os.name": "Android", "os.version": self.persona["os_version"],
                          "device.model.identifier": self.persona["model"], "device.manufacturer": self.persona["manufacturer"]})
        span = {"traceId": c.trace_id, "spanId": c.span_id, "name": c.ep.method, "kind": 3,
                "startTimeUnixNano": str(nanos(c.start)), "endTimeUnixNano": str(nanos(c.end)), "flags": 257,
                "status": {"code": 2 if c.status is None or c.status >= 400 else 0},
                "attributes": otlp.attrs({"http.request.method": c.ep.method, "url.full": c.url,
                                          "http.response.status_code": c.status, "server.address": self.api_host,
                                          "emb.device_id": self.persona["device_id"], "emb.session_id": self.part.id})}
        return {"resource": {"attributes": res}, "scopeSpans": [{"scope": {"name": "io.opentelemetry.okhttp-3.0"},
                                                                 "spans": [span]}]}

    # --- output ----------------------------------------------------------------------------------

    def finish(self):
        if not self.ended:
            self.leave()
        self.persona["user_session_number"] = self.user_session_number
        self.persona["session_part_number"] = self.part_number
        self.persona["previous_user_session_id"] = self.user_session_id
        self.end_ms = self.t
        return self.deliveries


ENVELOPE_TIME_KEYS = ("start_time_unix_nano", "end_time_unix_nano", "time_unix_nano", "process_start_time")


def shift_envelope(obj, delta_ns):
    """Shift every timestamp in an Embrace envelope (integer nanoseconds; user session start in ms)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ENVELOPE_TIME_KEYS and (isinstance(v,int) or isinstance(v,str) and v.isdigit()):
                obj[k] = str(int(v)+delta_ns) if isinstance(v,str) else v+delta_ns
            elif k == "key" and v == "emb.user_session_start_ts":
                obj["value"] = str(int(obj["value"]) + delta_ns // 1_000_000)
            elif k == "key" and v in ("last_known_time_unix_nano","emb.heartbeat_time_unix_nano"):
                obj["value"] = str(int(obj["value"]) + delta_ns)
            elif k == "key" and v == "account.snapshot_as_of_ms":
                obj["value"] = str(int(obj["value"]) + delta_ns // 1_000_000)
            else:
                shift_envelope(v, delta_ns)
    elif isinstance(obj, list):
        for x in obj:
            shift_envelope(x, delta_ns)
