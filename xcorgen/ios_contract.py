"""Apple SDK envelope checks from payload models and SessionSpanUtils tests."""

import json
import re
from .ios_devices import IOS_DEVICES


def check(doc, problem, clients=None, devices=None):
    p = doc["payload"]
    r = p.get("resource", {})
    if p.get("version") != "1.0" or p.get("type") not in ("spans", "logs"):
        problem("Apple envelope version/type")
    if (
        r.get("sdk_platform") != "ios"
        or r.get("os_name") != "ios"
        or r.get("device_manufacturer") != "Apple"
    ):
        problem("Apple resource platform")
    if (r.get("device_model"), r.get("os_version")) not in {
        (d[0], v) for d in IOS_DEVICES for v in d[1]
    }:
        problem("Apple device/OS combination")
    if any(
        k in r
        for k in (
            "os_code",
            "sdk_simple_version",
            "app_ecosystem_id",
            "uses_emmc_storage",
        )
    ):
        problem("Android fields in Apple resource")
    if doc.get("headers", {}).get("User-Agent") != "Embrace/i/" + r.get(
        "sdk_version", ""
    ):
        problem("Apple upload User-Agent")
    for span in p.get("data", {}).get("spans", []):
        a = {x["key"]: x["value"] for x in span["attributes"]}
        if span.get("status") not in ("unset", "ok", "error") or not isinstance(
            span.get("links"), list
        ):
            problem("Apple span status/links")
        if not re.fullmatch(
            "[0-9a-f]{32}", span.get("trace_id", "")
        ) or not re.fullmatch("[0-9a-f]{16}", span.get("span_id", "")):
            problem("Apple span IDs")
        if any(not isinstance(x["value"], str) for x in span["attributes"]):
            problem("Apple attributes must be strings")
        if not a.get("session.id"):
            problem("Apple session identity missing")
        if any(
            k.startswith(
                ("emb.android.", "emb.user_session", "emb.session_part", "jvm.")
            )
            for k in a
        ):
            problem("Android identity in Apple span")
        if span["name"] == "emb-session":
            if span.get("parent_span_id") is not None:
                problem("Apple session has no parent")
            for key in (
                "emb.state",
                "emb.clean_exit",
                "emb.cold_start",
                "emb.terminated",
                "emb.heartbeat_time_unix_nano",
                "emb.session_number",
            ):
                if key not in a:
                    problem("Apple session missing " + key)
            if devices is not None:
                devices.setdefault(a["session.id"], set()).add(doc["device_id"])
        if a.get("emb.type") == "perf.network_request":
            tp = a.get("emb.w3c_traceparent")
            if tp and tp != "00-" + span["trace_id"] + "-" + span["span_id"] + "-01":
                problem("Apple traceparent IDs")
            if tp and clients is not None:
                clients[span["trace_id"]] = {
                    "spanId": span["span_id"],
                    "startTimeUnixNano": str(span["start_time_unix_nano"]),
                    "endTimeUnixNano": str(span["end_time_unix_nano"]),
                }
    for row in p.get("data", {}).get("logs", []):
        if (
            not isinstance(row["time_unix_nano"], str)
            or not row["time_unix_nano"].isdigit()
        ):
            problem("Apple log time must be string nanoseconds")
        a = {x["key"]: x["value"] for x in row["attributes"]}
        if not a.get("session.id"):
            problem("Apple log session.id missing")
        if a.get("emb.type") == "sys.ios.crash":
            if a.get("emb.provider") != "kscrash":
                problem("Apple crash provider")
            try:
                crash = json.loads(a["emb.payload"])
                if not crash["crash"]["threads"][0]["crashed"]:
                    problem("Apple crash missing crashed thread")
            except (ValueError, KeyError, IndexError):
                problem("Apple crash report shape")
