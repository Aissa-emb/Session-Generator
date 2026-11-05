"""Apple SDK 6.21 envelopes; scheduling/call simulation reuse the native adapter.

Source contract: EmbraceCore/Payload, SessionSpanUtils, EmbraceSemantics and
EmbraceUpload in the Apple SDK. Native session IDs are session.id, status is
lowercase, logs have string nanoseconds, and envelope version is 1.0.
"""

import hashlib
import json
from .android_session import (
    AndroidSession,
    Part,
    SessionEnded,
    compose_label,
    kv,
    nanos,
)
from .calls import Result, deliver_backend, release_backend, simulate
from .util import emb_id, hex_id, lat

# Hardware identifiers with compatible iOS versions, screen pixels and storage.
from .ios_devices import IOS_DEVICES

ERRORS = {
    "timeout": (-1001, "The request timed out."),
    "reset": (-1005, "The network connection was lost."),
    "dns": (-1003, "A server with the specified hostname could not be found."),
    "offline": (-1009, "The Internet connection appears to be offline."),
    "tls": (
        -1200,
        "An SSL error has occurred and a secure connection to the server cannot be made.",
    ),
    "cancelled": (-999, "cancelled"),
}


class IOSSession(AndroidSession):
    platform = "ios"

    def _resource(self):
        p = self.persona
        if len(self.process_id) != 32:
            self.process_id = emb_id(self.rng)
        return {
            "os_name": "ios",
            "os_type": "darwin",
            "os_alternate_type": "iPadOS" if p["model"].startswith("iPad") else "iOS",
            "os_version": p["os_version"],
            "sdk_platform": "ios",
            "sdk_version": self.sdk_version,
            "device_manufacturer": "Apple",
            "device_model": p["model"],
            "device_architecture": "arm64",
            "screen_resolution": p["screen"],
            "disk_total_capacity": p["disk_total"],
            "jailbroken": False,
            "app_bundle_id": self.app["package"],
            "app_version": self.app_version,
            "build": self.version_code,
            "build_id": hashlib.md5(
                (self.app["package"] + self.app_version).encode(), usedforsecurity=False
            )
            .hexdigest()
            .upper(),
            "app_framework": 1,
            "environment": "prod",
            "launch_count": p["version_starts"],
            "process_identifier": self.process_id,
            "process_start_time": nanos(self.persona.get("native_process_start_ms", self.start_ms)),
            "process_pre_warm": False,
        }

    def _envelope(self, kind, data):
        return {
            "resource": self.resource,
            "metadata": self._metadata(),
            "version": "1.0",
            "type": kind,
            "data": data,
        }

    def _deliver(self, due, target, kind, payload):
        super()._deliver(due, target, kind, payload)
        if target == "embrace":
            self.deliveries[-1]["headers"] = {
                "User-Agent": "Embrace/i/" + self.sdk_version,
                "X-EM-AID": self.app_id,
                "X-EM-DID": self.persona["device_id"],
            }

    def _span_common(self):
        return {"session.id": self.part.id, "user.id": self.user_id}

    def _log_common(self):
        return {
            "session.id": self.part.id,
            "log.record.uid": emb_id(self.rng),
            "user.id": self.user_id,
            **{"emb.properties." + k: v for k, v in self.properties.items()},
        }

    def _span(self, *args, **kwargs):
        span = super()._span(*args, **kwargs)
        span["status"] = span["status"].lower()
        span["links"] = []
        if span.get("parent_span_id") == "0000000000000000":
            span.pop("parent_span_id")
        return span

    def _start_part(self, start_type):
        super()._start_part(start_type)
        if self.part.index == 1:
            self.user_session_id = self.part.id

    def _end_part(self, end_type="state", final=None):
        self._end_screen()
        self.activity_start = None
        p = self.part
        p.end = self.t
        p.end_type = end_type
        a = {
            "emb.type": "ux.session",
            "session.id": p.id,
            "emb.state": "foreground",
            "emb.cold_start": p.cold,
            "emb.terminated": bool(p.crash_id),
            "emb.clean_exit": not bool(p.crash_id),
            "emb.heartbeat_time_unix_nano": nanos(self.t),
            "emb.session_number": p.number,
            **{"emb.properties." + k: v for k, v in self.properties.items()},
        }
        if p.crash_id:
            a["emb.crash_id"] = p.crash_id
        session = {
            "trace_id": p.trace_id,
            "span_id": p.span_id,
            "name": "emb-session",
            "status": "error" if p.crash_id else "ok",
            "start_time_unix_nano": nanos(p.start),
            "end_time_unix_nano": nanos(self.t),
            "attributes": kv(a),
            "events": sorted(p.events, key=lambda e: e["time_unix_nano"]),
            "links": [],
        }
        self._deliver(
            self.t + 300,
            "embrace",
            "spans",
            self._envelope(
                "spans", {"spans": [session] + p.spans, "span_snapshots": []}
            ),
        )
        release_backend(self, self.t, self.rng)
        self._flush_logs(p)
        self.sdk_session_ids = getattr(self, "sdk_session_ids", []) + [p.id]

    def _flush_logs(self, part):
        for _, row in part.logs:
            row["time_unix_nano"] = str(row["time_unix_nano"])
        super()._flush_logs(part)

    def _startup(self, cold):
        """StartupInstrumentation (6.21.0). A new process records ``emb-app-startup-<cold|warm>`` (sys.startup):
        the SDK calls a launch cold only for a first launch, or when both the build UUID and the boot time
        changed since the last launch. Children: emb-app-pre-main-init (not prewarmed), emb-app-first-frame-rendered
        and emb-app-startup-app-init from the constructor closest to main, emb-sdk-setup and emb-sdk-start. Every
        span carries isPrewarmed. A resumed (suspended) app records no startup trace."""
        rng = self.rng
        t = self.t
        self.startup_ms = lat(350, 1100, rng) if cold else lat(80, 280, rng)
        if self.persona.get("population_model"):
            self.startup_ms *= self.persona["cpu_factor"]
        regression = getattr(getattr(self, "plan", None), "slow_startup", None)
        if regression:
            self.startup_ms = rng.uniform(regression.get("min_ms", 4200), min(9500, regression.get("max_ms", 7500)))
        self.sdk_startup = lat(10, 45, rng)
        if cold:
            launched_before = bool(self.persona.get("ios_launched_before") or self.user_session_number > 1
                                   or self.persona.get("returning_visit"))
            updated = self.persona.get("version_starts", 1) <= 1
            # Cold: no stored build UUID/boot time yet, or a new build after a reboot (assumed for 30% of updates).
            kind = "cold" if not launched_before or (updated and rng.random() < .3) else "warm"
            self.persona["ios_launched_before"] = True
            prewarmed = rng.random() < .3
            self.resource["process_pre_warm"] = prewarmed
            flag = {"isPrewarmed": prewarmed}
            tid, root = hex_id(16, rng), hex_id(8, rng)
            total = self.startup_ms
            pre_main = 0.0 if prewarmed else total * rng.uniform(.16, .3)
            constructor = t + pre_main
            launched = constructor + (total - pre_main) * rng.uniform(.5, .72)
            first_frame = t + total
            setup_start = constructor + rng.uniform(4, 30)
            setup_end = setup_start + self.sdk_startup
            start_begin = setup_end + rng.uniform(1, 4)
            start_end = start_begin + lat(6, 24, rng)
            # Embrace.setup and start run inside didFinishLaunching, which ends before the first frame.
            launched = max(launched, start_end + rng.uniform(5, 40))
            first_frame = max(first_frame, launched + rng.uniform(30, 120))
            self.startup_ms = first_frame - t
            span = lambda *a, **k: self._span(*a, trace_id=tid, **k)
            # The root and first-frame span end together; the other children are recorded at launch end.
            span("emb-app-startup-" + kind, constructor if prewarmed else t, first_frame,
                 dict(flag, **{"emb.type": "sys.startup"}), span_id=root)
            if not prewarmed:
                span("emb-app-pre-main-init", t, constructor, dict(flag, **{"emb.type": "sys.startup"}), parent=root)
            span("emb-app-first-frame-rendered", constructor, first_frame, dict(flag, **{"emb.type": "sys.startup"}),
                 parent=root)
            span("emb-app-startup-app-init", constructor, launched, dict(flag, **{"emb.type": "sys.startup"}), parent=root)
            span("emb-sdk-setup", setup_start, setup_end, dict(flag, **{"emb.type": "sys.startup"}), parent=root)
            span("emb-sdk-start", start_begin, start_end, dict(flag, **{"emb.type": "sys.startup"}), parent=root)
        self.t += self.startup_ms

    def _foreground_return(self, background_ms):
        """Returning from the background resumes existing view controllers; no load trace is recorded."""

    def _screen(self, key, label):
        """UIViewControllerHandler (6.21.0, instrumentFirstRender on): emb-<Class>-time-to-first-render
        (perf.ui_load) from viewDidLoad to the end of viewDidAppear, with the four lifecycle children; the
        emb-screen-view visibility span starts when viewDidAppear ends."""
        self._end_screen()
        self.screen_def_key = key
        self.screen_label = label
        name = self.app["screens"][key].replace("Fragment", "ViewController")
        rng, scale = self.rng, self.persona.get("cpu_factor", 1.0)
        tid, root = hex_id(16, rng), hex_id(8, rng)
        attrs = {"emb.type": "perf.ui_load", "view.title": label, "view.name": name}
        t, stages = self.t, []
        for stage, p50, p95, gap in (("emb-view-did-load", 14, 60, (1, 6)), ("emb-view-will-appear", 2, 12, (2, 9)),
                                     ("emb-view-is-appearing", 2, 10, (180, 420)), ("emb-view-did-appear", 1, 6, (0, 0))):
            end = t + lat(p50, p95, rng) * scale
            stages.append((stage, t, end))
            t = end + rng.uniform(*gap)
        for stage, start, end in stages:
            self._span(stage, start, end, attrs, trace_id=tid, parent=root)
        self._span(f"emb-{name}-time-to-first-render", stages[0][1], stages[-1][2], attrs, trace_id=tid, span_id=root)
        self.t = stages[-1][2]
        self.screen = {"name": name, "route": key, "start": self.t}

    def _end_screen(self):
        if self.screen and not self.screen.get("closed"):
            self._span(
                "emb-screen-view",
                self.screen["start"],
                self.t,
                {
                    "emb.type": "ux.view",
                    "view.name": self.screen["name"],
                    "view.title": self.screen_label,
                },
            )
            self.screen["closed"] = True

    def click(self, element_html, selector=None, breadcrumb=None):
        w, h = (int(x) // 3 for x in self.persona["screen"].split("x"))
        self.part.events.append(
            {
                "name": "emb-ui-tap",
                "time_unix_nano": nanos(self.t),
                "attributes": kv(
                    {
                        "emb.type": "ux.tap",
                        "view.name": compose_label(element_html),
                        "tap.coords": f"{self.rng.randint(10,w-10)},{self.rng.randint(40,h-40)}",
                    }
                ),
            }
        )
        if breadcrumb:
            self.breadcrumb(breadcrumb)
        self._think()

    def _request(self, key, params, start):
        c = simulate(self, key, params, start, self.rng)
        url = c.url.split("?")[0]
        path = url.split(self.api_host, 1)[1] or "/"
        a = {
            "emb.type": "perf.network_request",
            "url.full": url,
            "http.request.method": c.ep.method,
            "http.response.status_code": c.status,
            "http.request.body.size": c.request_bytes,
            "http.response.body.size": None if c.transport else c.response_bytes,
        }
        if self.traces:
            a["emb.w3c_traceparent"] = c.traceparent
        if c.transport:
            code, message = ERRORS.get(c.transport, ERRORS["reset"])
            a.update(
                {
                    "error.type": "NSURLErrorDomain",
                    "error.code": code,
                    "error.message": message,
                }
            )
        failed = c.status is None or c.status >= 400
        self._span(
            "emb-" + c.ep.method + " " + path,
            start,
            c.end,
            a,
            status="Error" if failed else "Unset",
            trace_id=c.trace_id,
            span_id=c.span_id,
        )
        if self.traces:
            deliver_backend(self, c, self.rng)
        self.calls.append((key, c.status, c.trace_id))
        return Result(c.status, c.outcome, c.data, c.trace_id, start, c.end)

    def exception(
        self, exc_type, message, handled=False, function="mapResult", **attrs
    ):
        name = "NSInvalidArgumentException"
        reason = "Required response field was unavailable"
        if handled:
            self.log(
                "error",
                reason,
                **{
                    "exception.type": "NSError",
                    "exception.message": reason,
                    "error.domain": "VantraResponseError",
                    "error.code": 1002,
                },
            )
        else:
            self._crash(name, reason, [function])

    def _crash(self, exc_type, message, frames):
        crash_id = emb_id(self.rng)
        self.part.crash_id = crash_id
        image = 0x100000000
        frame = {
            "instruction_addr": image + 0x1450,
            "object_addr": image,
            "object_name": "Vantra",
            "symbol_addr": image + 0x1400,
            "symbol_name": frames[0] if exc_type == "SwiftFatalError" else "-[CheckoutResultMapper mapResponse:]",
        }
        payload = {
            "report": {
                "version": "3.3.0",
                "id": crash_id,
                "process_name": "Vantra",
                "timestamp": int(self.t * 1000),
                "type": "standard",
            },
            "system": {
                "system_name": "iOS",
                "system_version": self.persona["os_version"],
                "machine": self.persona["model"],
                "cpu_arch": "arm64",
            },
            "binary_images": [
                {
                    "image_addr": image,
                    "image_size": 0x400000,
                    "name": "Vantra",
                    "uuid": self.resource["build_id"],
                    "cpu_type": 16777228,
                    "cpu_subtype": 0,
                }
            ],
            "crash": {
                "error": {
                    "type": "nsexception",
                    "nsexception": {
                        "name": "NSInvalidArgumentException",
                        "reason": "Required response field was unavailable",
                    },
                    "signal": {"signal": 6, "name": "SIGABRT"},
                    "mach": {"exception": 10, "exception_name": "EXC_CRASH"},
                },
                "threads": [
                    {
                        "index": 0,
                        "crashed": True,
                        "current_thread": True,
                        "name": "main",
                        "backtrace": {"contents": [frame], "skipped": 0},
                    }
                ],
            },
            "user": {"session_id": self.part.id},
        }
        if exc_type == "SwiftFatalError":
            payload["crash"]["error"] = {
                "type": "mach", "reason": message,
                "signal": {"signal": 5, "name": "SIGTRAP"},
                "mach": {"exception": 6, "exception_name": "EXC_BREAKPOINT"},
            }
        attrs = dict(
            self._log_common(),
            **{
                "emb.type": "sys.ios.crash",
                "emb.provider": "kscrash",
                "emb.payload": json.dumps(payload),
                "log.record.uid": crash_id,
            },
        )
        self.part.logs.append(
            (
                self.t,
                {
                    "time_unix_nano": str(nanos(self.t)),
                    "severity_number": 17,
                    "severity_text": "ERROR",
                    "body": "",
                    "attributes": kv(attrs),
                },
            )
        )
        self.crash_evidence = {
            "record_id": crash_id,
            "part_id": self.part.id,
            "time_ms": self.t,
            "exception_type": exc_type,
            "message": message,
            "frames": [frame["symbol_name"]],
        }
        self._end_part()
        self.ended = True
        # Persisted crash data is uploaded at the next process launch.
        for d in self.deliveries:
            if d["target"] == "embrace":
                d["due"] = max(d["due"], self.t + 10000)
        raise SessionEnded()

    def native_blockage(self, duration, family=None):
        """HangCaptureService (6.21.0): one span per hang with the first in-hang stack as its only sample."""
        from . import hang_stacks
        rng, start = self.rng, self.t
        story = hang_stacks.choose(family or "", rng)
        images = hang_stacks.Images(self.persona["os_version"], self.app["app_name"], self.resource["build_id"],
                                    self.process_id, self.persona["device_id"])
        screen = (self.screen or {}).get("name") or "HomeViewController"
        attrs = hang_stacks.sample(story, self.profile.key, screen, images, rng)
        self._span(
            "emb-thread-blockage",
            start,
            start + duration,
            {"emb.type": "perf.thread_blockage", "last_known_time_unix_nano": nanos(start), "interval_code": 0,
             "thread_priority": 0},
            events=[{"name": "thread_blockage_sample", "time_unix_nano": nanos(start), "attributes": kv(attrs)}],
        )
        return story

    def client_resource_spans(self, c):
        row = super().client_resource_spans(c)
        replacements = {
            "service.name": self.app["app_name"].lower() + "-ios",
            "telemetry.sdk.language": "swift",
            "os.name": "iOS",
            "device.manufacturer": "Apple",
        }
        from .otlp import attrs

        row["resource"]["attributes"] = [
            a for a in row["resource"]["attributes"] if a["key"] not in replacements
        ] + attrs(replacements)
        row["scopeSpans"][0]["scope"]["name"] = "URLSessionInstrumentation"
        return row
