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
            "process_start_time": nanos(self.start_ms),
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
        t = self.t
        tid, root = hex_id(16, self.rng), hex_id(8, self.rng)
        self.startup_ms = lat(350, 1100, self.rng) if cold else lat(80, 280, self.rng)
        regression = getattr(getattr(self, "plan", None), "slow_startup", None)
        if regression:
            self.startup_ms = self.rng.uniform(
                regression.get("min_ms", 4200),
                min(9500, regression.get("max_ms", 7500)),
            )
        self.sdk_startup = lat(10, 45, self.rng)
        if cold:
            for name, lo, hi in [
                ("emb-app-pre-main-init", 0, 0.2),
                ("emb-app-startup-app-init", 0.2, 0.65),
                ("emb-app-first-frame-rendered", 0.65, 1),
            ]:
                self._span(
                    name,
                    t + self.startup_ms * lo,
                    t + self.startup_ms * hi,
                    {"emb.type": "sys.startup"},
                    trace_id=tid,
                    parent=root,
                )
            self._span(
                "emb-app-startup",
                t,
                t + self.startup_ms,
                {"emb.type": "sys.startup", "isPrewarmed": False},
                trace_id=tid,
                span_id=root,
            )
            self._span(
                "emb-sdk-setup", t, t + self.sdk_startup, {"emb.type": "sys.startup"}
            )
        self.t += self.startup_ms

    def _screen(self, key, label):
        self._end_screen()
        self.screen_def_key = key
        self.screen_label = label
        name = self.app["screens"][key].replace("Fragment", "ViewController")
        self.screen = {"name": name, "route": key, "start": self.t}
        self._span(
            "emb-view-did-load",
            self.t,
            self.t + lat(12, 40, self.rng),
            {"emb.type": "perf.ui_load", "view.name": name, "view.title": label},
        )

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

    def native_blockage(self, duration):
        start = self.t
        events = [
            {
                "name": "thread_blockage_sample",
                "time_unix_nano": nanos(start + i),
                "attributes": kv(
                    {
                        "emb.type": "perf.thread_blockage_sample",
                        "sample_overhead": 300000,
                        "frame_count": 2,
                    }
                ),
            }
            for i in range(500, int(duration), 500)
        ]
        self._span(
            "emb-thread-blockage",
            start,
            start + duration,
            {
                "emb.type": "perf.thread_blockage",
                "interval_code": 0,
                "thread_priority": 47,
                "last_known_time_unix_nano": nanos(start + duration),
            },
            events=events,
        )

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
