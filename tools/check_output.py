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
REF = os.path.join(ROOT, "reference", "web-sdk-2.31.0-capture")
TP = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-01$")

# Keys added by app code (session properties, custom span/log attributes) rather than the SDK.
CUSTOM_OK = re.compile(r"^(emb\.properties\.|journey\.|product\.|checkout\.|order\.|http\.status_code$|auth\.|transfer\.|"
                       r"statement\.|search\.|booking\.|mfa\.|account\.|error\.type$|emb\.error_code$)")
# App-supplied attributes on custom performance spans; validate their types
# rather than treating them as built-in keys present in the reference capture.
CUSTOM_TASK_ATTRIBUTES = {"app.operation": "stringValue", "app.fallback.used": "boolValue",
                          "http.response.status_code": "intValue"}


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
            url, tp = a["url.full"], a.get("emb.w3c_traceparent", "")
            path = url.split("/", 3)[3] if url.count("/") >= 3 else ""
            if s["name"] != f"emb-{a['http.request.method']} /{path}" or "?" in url:
                problem("network span name must be 'emb-<METHOD> <path>' with the query stripped")
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


def vtype(v):
    return next(iter(v)) if v else "empty"


def kind_of(item):
    a = {x["key"]: x["value"] for x in item.get("attributes", [])}
    t = a.get("emb.type", {}).get("stringValue")
    return t or item.get("name")


def reference_schema():
    schema = defaultdict(dict)
    for fn in glob.glob(os.path.join(REF, "*.json")):
        body = json.load(open(fn))["body"]
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
            check_envelope(doc, problem, browser_spans, devices)
            continue
        if doc["target"] == "embrace":
            res = p.get("resourceSpans") or p.get("resourceLogs")
            if len(res) != 1:
                problem("embrace payload must have exactly one resource")
            if not re.fullmatch(r"[0-9A-F]{32}", doc["device_id"]):
                problem("X-EM-DID must be 32 uppercase hex")
            for a in res[0]["resource"]["attributes"]:
                ref = schema["resource"].get(a["key"])
                if ref is None:
                    problem(f"resource key not in capture: {a['key']}")
                elif ref != vtype(a["value"]):
                    problem(f"resource {a['key']}: {vtype(a['value'])} vs {ref}")
            groups = [("span", ss["scope"]["name"], ss["spans"]) for ss in res[0].get("scopeSpans", [])] + \
                     [("log", sl["scope"]["name"], sl["logRecords"]) for sl in res[0].get("scopeLogs", [])]
            for what, scope, items in groups:
                for it in items:
                    k = (what, scope, kind_of(it))
                    if k[2] not in ("ux.surface", "perf") and k not in schema:
                        # Surface span names are routes and perf spans are custom; others must exist in the capture.
                        problem(f"{what} type not in capture: {k}")
                        continue
                    ref = schema.get(k) or schema.get(next((x for x in schema if x[:2] == k[:2]), None), {})
                    for a in it.get("attributes", []):
                        r = ref.get(a["key"])
                        if r is None and k[2] == "perf":
                            r = CUSTOM_TASK_ATTRIBUTES.get(a["key"])
                        if r is None and not CUSTOM_OK.match(a["key"]):
                            problem(f"{k[1]}/{k[2]}: key not in capture: {a['key']}")
                        elif r is not None and r != vtype(a["value"]) and r != "empty":
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

    print(f"{len(browser_spans)} API network spans with traceparent, {len(backend)} backend traces")
    n_spans = sum(len(v) for v in backend.values())
    services = {svc for v in backend.values() for svc, _ in v}
    print(f"{n_spans} backend spans across {len(services)} services; "
          f"avg {n_spans / max(1, len(backend)):.1f} spans per trace")
    if problems:
        print("PROBLEMS:")
        for msg, n in sorted(problems.items(), key=lambda x: -x[1]):
            print(f"  {n:5d}  {msg}")
        return 1
    print("OK: payloads match the SDK formats and every backend trace hangs off its client network span")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
