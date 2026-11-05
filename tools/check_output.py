#!/usr/bin/env python3
"""Check a --dry-run output directory.

1. Web payloads use the same scopes, attribute keys and value types as the
   real Web SDK capture in reference/web-sdk-2.31.0-capture. Android envelopes
   use the shapes, keys and string values of the Android SDK (golden files in
   reference/android-sdk-golden plus the SDK source rules below), and every
   session part of a user session is sent with the same device ID.
2. Every API network span carries a valid, sampled traceparent that matches
   the span's own trace and span IDs (what network span forwarding needs).
3. Every backend trace has exactly one root, whose parent is that browser
   span, and no span starts before its parent or outside the browser span
   (Kafka consumers excepted).

Usage: python3 tools/check_output.py DIR
"""

import glob
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from xcorgen.web_contract import COMMON, LOG_COMMON as WEB_LOG_COMMON, EXTRA, compatible, check as check_web
REF = os.path.join(ROOT, "reference", "web-sdk-2.31.0-capture")
TP = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-01$")

# Keys added by app code (session properties, custom span/log attributes) rather than the SDK.
CUSTOM_OK = re.compile(r"^(emb\.properties\.|journey\.|product\.|checkout\.|order\.|http\.status_code$|auth\.|transfer\.|"
                       r"statement\.|search\.|booking\.|mfa\.|account\.|error\.type$|emb\.error_code$)")
# App-supplied attributes on custom performance spans; validate their types
# rather than treating them as built-in keys present in the reference capture.
CUSTOM_TASK_ATTRIBUTES = {"app.operation": "stringValue", "app.fallback.used": "boolValue",
                          "http.response.status_code": "intValue"}
from xcorgen.causal.registry import PROVENANCE, SERVICES
CAUSAL_ATTRIBUTES = {k: 'boolValue' if k in ('demo.synthetic','demo.feature.detail_optional','demo.feature.transfer_view') else 'stringValue' for k in PROVENANCE}
CAUSAL_ATTRIBUTES.update({k:'stringValue' for k in ('response.schema','response.merchant.display_name','view.state','script.origin')})
CAUSAL_ATTRIBUTES['interaction.processing_ms']='number'


GOLDEN = os.path.join(ROOT, "reference", "android-sdk-golden")
HEX32U, HEX32L, HEX16L = re.compile(r"^[0-9A-F]{32}$"), re.compile(r"^[0-9a-f]{32}$"), re.compile(r"^[0-9a-f]{16}$")
ANDROID_RESOURCE = {"app_version": str, "app_framework": int, "build_id": str, "app_ecosystem_id": str,
                    "build_type": str, "build_flavor": str, "environment": str, "bundle_version": str,
                    "sdk_version": str, "sdk_simple_version": int, "device_manufacturer": str, "device_model": str,
                    "device_architecture": str, "jailbroken": bool, "disk_total_capacity": int, "os_type": str,
                    "os_name": str, "os_version": str, "os_code": str, "screen_resolution": str, "num_cores": int,
                    "uses_emmc_storage": bool, "device_soc_model": str, "otel_sdk_mode": str}
ANDROID_METADATA = {"user_id", "email", "username", "personas", "timezone_description", "locale"}
# emb-session keys documented in SessionPartSpanAttrPopulatorImpl / golden files, plus prefixes.
SESSION_EXTRA = {"emb.crash_id", "emb.error_code", "emb.termination_cause", "emb.storage.used", "emb.storage.available",
                 "emb.clock_gnss_drift", "emb.clock_network_drift", "emb.app.version_startup_counter"}
SESSION_PREFIXES = ("emb.properties.", "emb.usage.")
SPAN_COMMON = ("emb.process_identifier", "emb.session_part_id", "emb.user_session_id")
LOG_COMMON = ("emb.session_part_id", "emb.user_session_id", "emb.state", "log.record.uid")
SEVERITY = {9: "INFO", 13: "WARNING", 17: "ERROR"}


def golden_session_keys():
    keys = set()
    for name in ("user_session_basic_span.json", "user_session_jvm_crash_span.json"):
        keys.update(a["key"] for a in json.load(open(os.path.join(GOLDEN, name)))["attributes"])
    return keys


no_forwarding = [0]


def check_envelope(doc, problem, client_spans, devices):
    p = doc["payload"]
    if p.get("version") != "0.1.0" or p.get("type") not in ("spans", "logs"):
        problem("android envelope version/type")
    if doc.get("headers", {}).get("User-Agent", "").split("/")[:2] != ["Embrace", "a"]:
        problem("android User-Agent must be Embrace/a/<version>")
    if not HEX32U.match(doc["device_id"]):
        problem("X-EM-DID must be 32 uppercase hex")
    for k, v in p["resource"].items():
        t = ANDROID_RESOURCE.get(k)
        if t is None:
            problem(f"resource key not in the Android SDK resource: {k}")
        elif type(v) is not t:
            problem(f"resource {k} should be {t.__name__}")
    for k in p["metadata"]:
        if k not in ANDROID_METADATA:
            problem(f"metadata key not in EnvelopeMetadata: {k}")
    items = p["data"].get("spans", []) + p["data"].get("logs", [])
    for it in items:
        for a in it["attributes"]:
            if not isinstance(a.get("value"), str):
                problem("android attribute values must be strings")
    session_keys = golden_session_keys() | SESSION_EXTRA
    for s in p["data"].get("spans", []):
        if not (HEX32L.match(s["trace_id"]) and HEX16L.match(s["span_id"]) and len(s["parent_span_id"]) == 16):
            problem("span id formats")
        if s["status"] not in ("Unset", "Ok", "Error") or not isinstance(s["start_time_unix_nano"], int):
            problem("span status or timestamp type")
        a = {x["key"]: x["value"] for x in s["attributes"]}
        if "emb.type" not in a:
            problem(f"span without emb.type: {s['name']}")
        if s["name"] == "emb-session":
            for k in a:
                if k not in session_keys and not k.startswith(SESSION_PREFIXES):
                    problem(f"emb-session key not in the SDK: {k}")
            devices.setdefault(a["emb.user_session_id"], set()).add(doc["device_id"])
            for e in s["events"]:
                ea = {x["key"] for x in e["attributes"]}
                want = {"emb-ui-tap": {"emb.type", "view.name", "tap.type", "tap.coords"},
                        "emb-breadcrumb": {"emb.type", "message"}}.get(e["name"])
                if want is None or ea != want:
                    problem(f"session event shape: {e['name']}")
        else:
            missing = [k for k in SPAN_COMMON if k not in a]
            if missing:
                problem(f"span missing common attributes {missing}: {s['name']}")
        if a.get("emb.type") == "perf.network_request":
            url, tp = a["url.full"], a.get("emb.w3c_traceparent")
            path = url.split("/", 3)[3] if url.count("/") >= 3 else ""
            if s["name"] != f"emb-{a['http.request.method']} /{path}" or "?" in url:
                problem("network span name must be 'emb-<METHOD> <path>' with the query stripped")
            if (tp is None) != ("emb.forward_telemetry" not in a):
                problem("emb.w3c_traceparent and emb.forward_telemetry are set together, only for forwarded domains")
            if tp is None:
                no_forwarding[0] += 1
                continue
            if a.get("emb.forward_telemetry") != "true":
                problem("emb.forward_telemetry must be the string true when forwarding is on")
            m = TP.match(tp)
            if not m or m.group(1) != s["trace_id"] or m.group(2) != s["span_id"]:
                problem("traceparent does not match the network span IDs")
            code = a.get("http.response.status_code")
            if code is None and not a.get("error.type"):
                problem("request without a response needs a transport error")
            if s["status"] != ("Error" if code is None or int(code) >= 400 else "Unset"):
                problem("network span status must be Unset for 1xx-3xx and Error for 4xx/5xx")
            client_spans[s["trace_id"]] = {"spanId": s["span_id"], "startTimeUnixNano": str(s["start_time_unix_nano"]),
                                           "endTimeUnixNano": str(s["end_time_unix_nano"])}
    for r in p["data"].get("logs", []):
        a = {x["key"]: x["value"] for x in r["attributes"]}
        if SEVERITY.get(r["severity_number"]) != r["severity_text"]:
            problem("log severity text")
        missing = [k for k in LOG_COMMON if k not in a]
        if missing:
            problem(f"log missing {missing}")
        if a.get("emb.type") in ("sys.exception", "sys.android.crash"):
            json.loads(a["exception.stacktrace"])
        if a.get("emb.type") == "sys.android.crash":
            golden = {x["key"] for x in json.load(open(os.path.join(GOLDEN, "user_session_jvm_crash_log.json")))["logs"][0]["attributes"]}
            extra = {k for k in a if k not in golden and not k.startswith(("emb.state.", "emb.properties.")) and k != "user.id"}
            if extra:
                problem(f"crash log keys not in the SDK golden file: {sorted(extra)}")


MS = 1_000_000
JAVA_FRAME = re.compile(r"^[\w$.<>-]+\((Native Method|Unknown Source(:\d+)?|[\w$.@:+-]+(:\d+)?)\)$")
THREAD_STATES = {"NEW", "RUNNABLE", "BLOCKED", "WAITING", "TIMED_WAITING", "TERMINATED"}
ANDROID_BLOCKAGE_KEYS = {"emb.type", "interval_code", "last_known_time_unix_nano", *SPAN_COMMON, "user.id"}
ANDROID_SAMPLE_KEYS = {"emb.type", "sample_overhead", "sample_code"}
ANDROID_THREAD_KEYS = {"jvm.thread.state", "thread_priority", "frame_count", "exception.stacktrace"}
LAUNCH_REASONS = {"alarm", "backup", "boot_complete", "broadcast", "content_provider", "job", "launcher",
                  "launcher_recents", "other", "push", "service", "start_activity"}
UI_LOAD = re.compile(r"^emb-(.+)-(cold|hot)-time-to-initial-display$")
IOS_STARTUP_CHILDREN = {"emb-app-pre-main-init", "emb-app-first-frame-rendered", "emb-app-startup-app-init",
                        "emb-sdk-setup", "emb-sdk-start"}
IOS_VIEW_CHILDREN = ["emb-view-did-load", "emb-view-will-appear", "emb-view-is-appearing", "emb-view-did-appear"]
IOS_FRAME_KEYS = {"a", "m", "o", "p", "s", "so", "u"}


def _attrs(s):
    return {x["key"]: x["value"] for x in s["attributes"]}


def _root(s):
    return s.get("parent_span_id") in (None, "", "0000000000000000")


def check_android_blockage(s, a, problem):
    """ThreadBlockageOtelMapper / ThreadBlockageSampler (9.3.1) shapes and sampling rules."""
    if s["name"] != "emb-thread-blockage" or not _root(s):
        problem("thread blockage span must be the root span emb-thread-blockage")
    if set(a) - ANDROID_BLOCKAGE_KEYS or a.get("interval_code") not in ("0", "1"):
        problem(f"thread blockage span keys: {sorted(set(a) - ANDROID_BLOCKAGE_KEYS)}")
    start, end = s["start_time_unix_nano"], s["end_time_unix_nano"]
    if (end - start) % MS:
        problem("thread blockage interval must be whole milliseconds")
    previous, last_t, stacks = None, None, 0
    for n, e in enumerate(s["events"]):
        ea = _attrs(e)
        if e["name"] != "perf.thread_blockage_sample" or ea.get("emb.type") != "perf.thread_blockage_sample":
            problem("thread blockage sample event name/type")
        keys = set(ea)
        thread = keys & ANDROID_THREAD_KEYS
        if keys - ANDROID_SAMPLE_KEYS - ANDROID_THREAD_KEYS or not ANDROID_SAMPLE_KEYS <= keys or thread not in (set(), ANDROID_THREAD_KEYS):
            problem("thread blockage sample keys")
        if (e["time_unix_nano"] - start) % MS or int(ea.get("sample_overhead", "0")) % MS:
            problem("thread blockage sample times and overhead are whole milliseconds")
        if not start + 1000 * MS < e["time_unix_nano"] <= end:
            problem("thread blockage samples start after the 1 s threshold and stay inside the interval")
        if last_t is not None and e["time_unix_nano"] - last_t < 50 * MS:
            problem("thread blockage samples closer than half the 100 ms interval")
        last_t = e["time_unix_nano"]
        code = ea.get("sample_code")
        if code != ("1" if n >= 80 else "0"):
            problem("sample_code is 0 for the first 80 samples and 1 after the stack limit")
        if not thread:
            continue
        if code != "0":
            problem("samples past the stack limit carry no thread data")
        lines = ea["exception.stacktrace"].split("\n")
        if ea["jvm.thread.state"] not in THREAD_STATES or ea["thread_priority"] != "5":
            problem("main thread sample state/priority")
        if len(lines) > 200 or int(ea["frame_count"]) < len(lines) or not all(JAVA_FRAME.match(x) for x in lines):
            problem("sample stack lines must be StackTraceElement strings within the 200-frame limit")
        if not lines[-1].startswith(("com.android.internal.os.ZygoteInit.main(", "android.app.ActivityThread.main(")):
            problem("main thread stack must end at the process entry point")
        current = (ea["jvm.thread.state"], ea["exception.stacktrace"])
        if current == previous:
            problem("a stack equal to the previous sample is omitted by the SDK")
        previous, stacks = current, stacks + 1
    if s["events"] and not stacks:
        problem("thread blockage interval without any sampled stack")


def check_trace_tree(spans, problem, platform):
    """Startup, UI load and custom child spans: parents in the same payload, children inside their parent."""
    by_id = {s["span_id"]: s for s in spans}
    children = defaultdict(list)
    for s in spans:
        if not _root(s):
            parent = by_id.get(s["parent_span_id"])
            if parent is None:
                if _attrs(s).get("emb.type", "").startswith(("perf", "sys.startup")):
                    problem(f"child span without its parent in the payload: {s['name']}")
                continue
            if parent["trace_id"] != s["trace_id"]:
                problem("child span in a different trace than its parent")
            if not parent["start_time_unix_nano"] <= s["start_time_unix_nano"] <= s["end_time_unix_nano"] <= parent["end_time_unix_nano"]:
                problem(f"child span outside its parent: {parent['name']} > {s['name']}")
            children[parent["span_id"]].append(s)
    for s in spans:
        a, kids = _attrs(s), children[s["span_id"]]
        names = {k["name"] for k in kids}
        if platform == "android" and s["name"] in ("emb-app-startup-cold", "emb-app-startup-warm"):
            render = {"emb-activity-render", "emb-activity-first-draw"} & names
            want = {"emb-activity-init"} | render
            if s["name"].endswith("cold"):
                want |= {"emb-process-init", "emb-embrace-init", "emb-activity-init-delay"}
            if a.get("emb.type") != "perf" or names != want or len(render) != 1 or not a.get("emb.startup_activity"):
                problem(f"{s['name']} children/attributes do not match AppStartupTraceEmitter")
            if a.get("emb.startup_launch_reason") not in (None, *LAUNCH_REASONS):
                problem("emb.startup_launch_reason value")
            k = {x["name"]: x for x in kids}
            last = k.get("emb-activity-render") or k.get("emb-activity-first-draw")
            if last and last["end_time_unix_nano"] != s["end_time_unix_nano"]:
                problem("startup trace ends with the first frame")
            if "emb-process-init" in k:
                p, init, delay = k["emb-process-init"], k.get("emb-embrace-init"), k.get("emb-activity-init-delay")
                if p["start_time_unix_nano"] != s["start_time_unix_nano"] or (delay and delay["start_time_unix_nano"] != p["end_time_unix_nano"]):
                    problem("process-init starts the trace and activity-init-delay follows Application.onCreate")
                if init and not p["start_time_unix_nano"] <= init["start_time_unix_nano"] <= init["end_time_unix_nano"] <= p["end_time_unix_nano"]:
                    problem("embrace-init runs inside Application.onCreate")
        m = UI_LOAD.match(s["name"]) if platform == "android" else None
        if m:
            act, kind = m.groups()
            want = {f"emb-{act}-start", f"emb-{act}-resume"} | ({f"emb-{act}-create"} if kind == "cold" else set())
            if a.get("emb.type") != "perf.ui_load" or names != want or not _root(s):
                problem("UI load trace children/type do not match UiLoadTraceEmitter")
        if platform == "ios" and s["name"] in ("emb-app-startup-cold", "emb-app-startup-warm"):
            pre = a.get("isPrewarmed")
            want = IOS_STARTUP_CHILDREN - ({"emb-app-pre-main-init"} if pre == "true" else set())
            if a.get("emb.type") != "sys.startup" or pre not in ("true", "false") or names != want:
                problem("iOS startup trace children/attributes do not match StartupInstrumentation")
            if any(_attrs(x).get("isPrewarmed") != pre or _attrs(x).get("emb.type") != "sys.startup" for x in kids):
                problem("iOS startup children carry the root's isPrewarmed and sys.startup")
        if platform == "ios" and s["name"].endswith("-time-to-first-render"):
            ordered = [k["name"] for k in sorted(kids, key=lambda x: x["start_time_unix_nano"])]
            if a.get("emb.type") != "perf.ui_load" or ordered != IOS_VIEW_CHILDREN or not a.get("view.name"):
                problem("iOS view load trace does not match UIViewControllerHandler")
        if platform == "ios" and s["name"] in ("emb-view-did-load",) and _root(s):
            problem("emb-view-did-load is a child of the time-to-first-render span")


def check_ios_blockage(s, a, problem):
    """HangCaptureService (6.21.0): one span, at most one sample with base64 JSON frames."""
    import base64
    if set(a) - {"emb.type", "last_known_time_unix_nano", "interval_code", "thread_priority", "session.id", "user.id"}:
        problem("iOS hang span keys")
    if a.get("interval_code") != "0" or a.get("thread_priority") != "0" or int(a.get("last_known_time_unix_nano", 0)) != s["start_time_unix_nano"]:
        problem("iOS hang span attributes")
    if len(s["events"]) > 1:
        problem("iOS hang spans carry at most one sample")
    for e in s["events"]:
        ea = _attrs(e)
        if e["name"] != "thread_blockage_sample" or set(ea) != {"emb.type", "sample_overhead", "frame_count", "emb.stacktrace.ios"}:
            problem("iOS hang sample name/keys")
            continue
        frames = json.loads(base64.b64decode(ea["emb.stacktrace.ios"]))
        if int(ea["frame_count"]) != len(frames) or e["time_unix_nano"] != s["start_time_unix_nano"]:
            problem("iOS hang sample frame count/time")
        for f in frames:
            if set(f) != IOS_FRAME_KEYS or not re.fullmatch(r"0x[0-9a-f]{16}", f["a"]) or f["so"] > f["o"] or f["m"] != f["p"]:
                problem("iOS hang frame shape")
                break


def check_native_traces(doc, problem):
    p = doc["payload"]
    if p.get("type") != "spans":
        return
    platform = "ios" if p.get("resource", {}).get("sdk_platform") == "ios" else "android"
    spans = p["data"].get("spans", [])
    for s in spans:
        a = _attrs(s)
        if a.get("emb.type") == "perf.thread_blockage":
            (check_ios_blockage if platform == "ios" else check_android_blockage)(s, a, problem)
    check_trace_tree(spans, problem, platform)


def vtype(v):
    return next(iter(v)) if v else "empty"


def kind_of(item):
    a = {x["key"]: x["value"] for x in item.get("attributes", [])}
    t = a.get("emb.type", {}).get("stringValue")
    return t or item.get("name")


def reference_schema():
    schema = defaultdict(dict)
    paths = glob.glob(os.path.join(REF, "*.json")) + glob.glob(os.path.join(ROOT, "reference", "web-sdk-golden", "*.json"))
    paths += glob.glob(os.path.join(ROOT, "reference", "web-sdk-2.29-sample", "*.json"))
    documents = [json.load(open(fn)) for fn in paths]
    for fn in glob.glob(os.path.join(ROOT, "reference", "web-sdk-2.29-sample", "*.jsonl")):
        documents.extend(json.loads(line) for line in open(fn) if line.strip())
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        body = doc.get("body", doc.get("payload", doc))
        if not isinstance(body, dict):
            continue
        for rs in body.get("resourceSpans", []) + body.get("resourceLogs", []):
            for a in rs["resource"]["attributes"]:
                schema["resource"][a["key"]] = vtype(a["value"])
            for ss in rs.get("scopeSpans", []):
                for s in ss["spans"]:
                    k = ("span", ss["scope"]["name"], kind_of(s))
                    for a in s["attributes"]:
                        schema[k][a["key"]] = vtype(a["value"])
                    for e in s.get("events", []):
                        for a in e.get("attributes", []):
                            schema[("event", e["name"])][a["key"]] = vtype(a["value"])
            for sl in rs.get("scopeLogs", []):
                for r in sl["logRecords"]:
                    k = ("log", sl["scope"]["name"], kind_of(r))
                    for a in r["attributes"]:
                        schema[k][a["key"]] = vtype(a["value"])
    return schema


def main(d):
    schema = reference_schema()
    problems = defaultdict(int)
    examples = {}
    browser_spans = {}
    backend = defaultdict(list)

    def problem(msg):
        problems[msg] += 1

    files = sorted(glob.glob(os.path.join(d, "*.json")))
    if not files:
        print(f"no payload files in {d}")
        return 1
    devices = {}
    for fn in files:
        doc = json.load(open(fn))
        p = doc["payload"]
        if doc.get("fmt") == "envelope":
            if p.get('resource',{}).get('sdk_platform')=='ios':
                from xcorgen.ios_contract import check as check_ios
                check_ios(doc,problem,browser_spans,devices)
            else:
                check_envelope(doc, problem, browser_spans, devices)
            check_native_traces(doc, problem)
            continue
        if doc["target"] == "embrace":
            # V2 uses the current SDK lifecycle; V1's historical adapter remains checked by its capture.
            resource = (p.get("resourceSpans") or p.get("resourceLogs"))[0]["resource"]
            app_version = next((a["value"].get("stringValue", "") for a in resource["attributes"] if a["key"] == "app_version"), "")
            if app_version.startswith("2026."):
                check_web(p, problem)
            res = p.get("resourceSpans") or p.get("resourceLogs")
            if len(res) != 1:
                problem("embrace payload must have exactly one resource")
            if not re.fullmatch(r"[0-9A-F]{32}", doc["device_id"]):
                problem("X-EM-DID must be 32 uppercase hex")
            for a in res[0]["resource"]["attributes"]:
                ref = schema["resource"].get(a["key"])
                if ref is None:
                    problem(f"resource key not in capture: {a['key']}")
                elif not compatible(vtype(a["value"]), ref):
                    problem(f"resource {a['key']}: {vtype(a['value'])} vs {ref}")
            groups = [("span", ss["scope"]["name"], ss["spans"]) for ss in res[0].get("scopeSpans", [])] + \
                     [("log", sl["scope"]["name"], sl["logRecords"]) for sl in res[0].get("scopeLogs", [])]
            for what, scope, items in groups:
                for it in items:
                    k = (what, scope, kind_of(it))
                    if k[2] not in ("ux.surface", "perf") and k not in schema and scope not in EXTRA and scope != "@opentelemetry/instrumentation-xml-http-request":
                        # Surface span names are routes and perf spans are custom; others must exist in the capture.
                        problem(f"{what} type not in capture: {k}")
                        continue
                    ref = dict(schema.get(k) or schema.get(next((x for x in schema if x[:2] == k[:2]), None), {}))
                    if scope == "@opentelemetry/instrumentation-xml-http-request":
                        ref.update(schema.get(("span", "@opentelemetry/instrumentation-fetch", "perf.network_request"), {}))
                    ref.update(COMMON)
                    if what == "log":
                        ref.update(WEB_LOG_COMMON)
                    ref.update(EXTRA.get(scope, {}))
                    for a in it.get("attributes", []):
                        r = ref.get(a["key"])
                        if r is None and k[2] == "perf":
                            r = CUSTOM_TASK_ATTRIBUTES.get(a["key"])
                        if r is None and k[2] in ('perf','sys.exception'):
                            r = CAUSAL_ATTRIBUTES.get(a['key'])
                        if r is None and not CUSTOM_OK.match(a["key"]):
                            problem(f"{k[1]}/{k[2]}: key not in capture: {a['key']}")
                        elif r is not None and not compatible(vtype(a["value"]), r) and r != "empty":
                            problem(f"{k[1]}/{k[2]}: {a['key']} is {vtype(a['value'])}, capture has {r}")
                    for e in it.get("events", []):
                        er = schema.get(("event", e["name"]))
                        if e.get("attributes") and er is not None:
                            for a in e["attributes"]:
                                if a["key"] not in er:
                                    problem(f"event {e['name']}: key not in capture: {a['key']}")
                    if what == "span" and kind_of(it) == "perf.network_request":
                        a = {x["key"]: next(iter(x["value"].values())) for x in it["attributes"]}
                        tp = a.get("emb.w3c_traceparent")
                        if tp:
                            m = TP.match(tp)
                            if not m or m.group(1) != it["traceId"] or m.group(2) != it["spanId"]:
                                problem("traceparent does not match the network span IDs")
                            browser_spans[it["traceId"]] = it
        elif doc["kind"] == "traces":
            for rs in p["resourceSpans"]:
                svc = next(a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name")
                for ss in rs["scopeSpans"]:
                    for s in ss["spans"]:
                        backend[s["traceId"]].append((svc, s))

    for usid, devs in devices.items():
        if len(devs) != 1:
            problem("parts of one user session were sent with different device IDs")
    for tid, spans in backend.items():
        b = browser_spans.get(tid)
        if not b:
            roots = [s for _, s in spans if not s.get("parentSpanId")]
            all_nodes={(t,s['spanId']):s for t,group in backend.items() for _,s in group}
            if roots and all(svc in SERVICES for svc,_ in spans) and all(s.get('links') and s['kind'] in (1,5) for s in roots):
                ids={s['spanId']:s for _,s in spans}
                for _,s in spans:
                    a={x['key']:next(iter(x['value'].values())) for x in s['attributes']}
                    if a.get('demo.synthetic') is not True: problem('async trace lacks synthetic provenance')
                    parent=ids.get(s.get('parentSpanId'))
                    if s.get('parentSpanId') and not parent: problem('async trace has missing parent')
                    if int(s['endTimeUnixNano']) < int(s['startTimeUnixNano']): problem('async negative duration')
                    if parent and not (int(parent['startTimeUnixNano'])<=int(s['startTimeUnixNano'])<=int(s['endTimeUnixNano'])<=int(parent['endTimeUnixNano'])):
                        problem('async child outside its synchronous parent')
                    for link in s.get('links',[]):
                        cause=all_nodes.get((link['traceId'],link['spanId']))
                        if not cause or int(cause['endTimeUnixNano']) > int(s['startTimeUnixNano']):problem('async link missing or precedes cause')
                continue
            if len(roots) == 1 and roots[0]["name"] == "authorization_worker.batch":
                a = {x["key"]: next(iter(x["value"].values())) for x in roots[0]["attributes"]}
                if a.get("workload.source") == "scheduled_authorizations" and len(spans) == 52:
                    continue
            problem("backend trace without a matching browser network span")
            continue
        ids = {s["spanId"]: s for _, s in spans}
        roots = [s for _, s in spans if s.get("parentSpanId") not in ids]
        if len(roots) != 1 or roots[0].get("parentSpanId") != b["spanId"]:
            problem("backend trace root is not a single child of the browser span")
        consumers = set()
        for _, s in spans:
            node, chain = s, []
            while node is not None:
                chain.append(node)
                if node["kind"] == 5:
                    consumers.update(x["spanId"] for x in chain)
                    break
                node = ids.get(node.get("parentSpanId"))
        for svc, s in spans:
            st, en = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
            if en < st:
                problem("span ends before it starts")
            parent = ids.get(s.get("parentSpanId"))
            if parent and st < int(parent["startTimeUnixNano"]):
                problem("span starts before its parent")
            if s["spanId"] not in consumers and not (int(b["startTimeUnixNano"]) <= st):
                problem("backend span starts before the browser request")
        root = roots[0] if roots else None
        if root and int(root["endTimeUnixNano"]) > int(b["endTimeUnixNano"]):
            problem("backend root ends after the browser span")

    print(f"{len(browser_spans)} API network spans with traceparent, {len(backend)} backend traces"
          + (f", {no_forwarding[0]} network spans without forwarding (sessions only)" if no_forwarding[0] else ""))
    n_spans = sum(len(v) for v in backend.values())
    services = {svc for v in backend.values() for svc, _ in v}
    print(f"{n_spans} backend spans across {len(services)} services; "
          f"avg {n_spans / max(1, len(backend)):.1f} spans per trace")
    if problems:
        print("PROBLEMS:")
        for msg, n in sorted(problems.items(), key=lambda x: -x[1]):
            print(f"  {n:5d}  {msg}")
        return 1
    print("OK: SDK payloads and client/worker trace relationships validated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
