"""Realism linter: nothing that reaches Cortex XCOR RUM or Cortex XCOR may reveal generated data.

lint_spec checks every spec field that is emitted (app names, packages,
domains, origins, bundle paths, session-property values, exception text).
Fault IDs, names and notes stay local and are not checked. lint_payloads
checks sampled generated payloads: forbidden words in any string, service
naming, exception language versus the service runtime, durations, HTTP
versus span status, device and OS combinations, and PII-looking values.
A lint error blocks a run.
"""
import json
import re

from .personas import ANDROID_DEVICES
from .ios_devices import IOS_DEVICES

FORBIDDEN = {"demo", "demos", "fake", "fakes", "faked", "test", "tests", "testing", "tester", "testers", "testid",
             "sample", "samples", "synthetic", "synthetics", "mock", "mocks", "mocked", "mocking", "dummy", "dummies",
             "placeholder", "lorem", "ipsum", "foobar", "sandbox", "simulated", "simulation", "generated", "generator",
             "fixture", "fixtures", "staging", "qa"}
# Names defined by the SDKs themselves; real production payloads contain them.
SDK_TERMS = {"thread_blockage_sample","perf.thread_blockage_sample", "sample_code", "sample_overhead"}
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]*-(experience-api|service|orchestrator|gateway|worker)$")
# The platform service names a generated backend may emit besides the conventions above.
from .causal.registry import SERVICES, PROVENANCE
SERVICE_EXCEPTIONS = {"api-gateway", *SERVICES}
PROVENANCE_KEYS = PROVENANCE | {"emb.properties." + k for k in PROVENANCE}

def permitted_provenance(path, text):
    if text in PROVENANCE_KEYS:
        return True
    return (text == "synthetic" and ".deployment.environment.name" in path or
            text == "session-generator" and ".telemetry.sdk.name" in path)
# Split words on separators and camelCase; identifiers such as 78S7QA stay one token.
_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\d.])\+?1?[ (-]*\d{3}[) -]+\d{3}[ -]\d{4}(?![\d.])")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD = re.compile(r"(?<![\w.])(?:[3-6]\d{3}(?:[ -]?\d{4}){3}|3[47]\d{2}[ -]?\d{6}[ -]?\d{5})(?![\w.])")
# Fields that hold timestamps, identifiers or sizes rather than free text.
NUMERIC_FIELDS = re.compile(r"(UnixNano|_unix_nano|Id|_id|\.id|identifier|traceparent|uid|_ts|\.size|\.offset|"
                            r"\.(stringValue|intValue|doubleValue))$")
STACK_SHAPE = {"java": re.compile(r"\n\tat [\w.$<>]+\("), "go": re.compile(r"goroutine \d+ \["),
               "python": re.compile(r"^Traceback \(most recent call last\):"), "dotnet": re.compile(r"\n   at .+ in .+:line \d+"),
               "nodejs": re.compile(r"\n    at .+ \(.+:\d+\)")}


def tokens(text):
    return [t.lower() for t in _SPLIT.split(text) if t]


def forbidden_in(text):
    if not isinstance(text, str) or text in SDK_TERMS:
        return set()
    return {t for t in tokens(text) if t in FORBIDDEN}


def _luhn(number):
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def _issue(severity, path, message, code="realism"):
    return {"severity": severity, "path": path, "message": message, "code": code}


def _strings(node, path="$"):
    if isinstance(node, dict):
        if isinstance(node.get("key"), str) and "value" in node:
            # Attribute lists: name the path after the attribute key so identifier fields can be told apart.
            yield f"{path}.<key>", node["key"]
            yield from _strings(node["value"], f"{path}.{node['key']}")
            return
        for k, v in node.items():
            yield from _strings(k, f"{path}.<key>") if isinstance(k, str) else ()
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


EMITTED_SPEC_FIELDS = ("targets", "population.session_properties", "client_faults.details", "backend_faults.details")


def lint_spec(spec):
    issues = []
    roots = [("$.targets", spec.get("targets", [])),
             ("$.population.session_properties", spec.get("population", {}).get("session_properties", {}))]
    for kind in ("client_faults", "backend_faults"):
        for i, f in enumerate(spec.get(kind, [])):
            detail = {k: v for k, v in f.get("details", {}).items() if k != "with_fault"}
            roots.append((f"$.{kind}[{i}].details", detail))
    for base, node in roots:
        for path, text in _strings(node, base):
            if path.endswith(".id") or path.endswith(".embrace_app_id") or ".region" in path or path.endswith(".platform"):
                continue
            bad = forbidden_in(text)
            if bad:
                issues.append(_issue("error", path, f"\"{text}\" would be emitted and contains {', '.join(sorted(bad))}; "
                                                    "nothing sent to Cortex XCOR RUM or Cortex XCOR may reveal generated data."))
    for i, f in enumerate(spec.get("backend_faults", [])):
        svc = f.get("service")
        if svc and not (SERVICE_NAME.match(svc) or svc in SERVICE_EXCEPTIONS):
            issues.append(_issue("error", f"$.backend_faults[{i}].service",
                                 "Service names follow <name>-experience-api, -service, -orchestrator, -gateway or -worker."))
    return issues


def _identifier(path):
    """Identifier and timestamp fields: hex and numeric IDs can look like card or phone numbers."""
    field = re.sub(r"\.(stringValue|intValue|doubleValue|value)$", "", path)
    return bool(NUMERIC_FIELDS.search(field))


def _vals(attributes):
    out = {}
    for a in attributes or []:
        v = a.get("value")
        out[a["key"]] = next(iter(v.values()), None) if isinstance(v, dict) and v else v
    return out


def lint_payloads(deliveries, max_issues=50):
    """Lint generated deliveries (dicts with target/kind/payload). Returns issues."""
    issues, seen = [], set()

    def add(severity, path, message, code):
        key = (code, message)
        if key in seen or len(issues) >= max_issues:
            return
        seen.add(key)
        issues.append(_issue(severity, path, message, code))

    devices = {(d[1], v) for d in ANDROID_DEVICES for v in d[2]}
    for d in deliveries:
        payload = d["payload"]
        for path, text in _strings(payload, f"$.{d['target']}.{d['kind']}"):
            bad = set() if permitted_provenance(path, text) else forbidden_in(text)
            if bad:
                add("error", path, f"Emitted value \"{text[:80]}\" contains {', '.join(sorted(bad))}.", "forbidden_word")
            if _identifier(path):
                continue
            if EMAIL.search(text):
                add("error", path, "Emitted value contains an email address.", "pii")
            if SSN.search(text) or PHONE.search(text):
                add("error", path, "Emitted value looks like a phone or social security number.", "pii")
            for m in CARD.finditer(text):
                if _luhn(m.group()):
                    add("error", path, "Emitted value contains a card number.", "pii")
        if d.get("fmt") == "envelope":
            r = payload["resource"]
            if r.get('sdk_platform')=='ios':
                from .ios_contract import check
                check(d,lambda message:add('error','$.ios',message,'ios_contract'))
                allowed={(x[0],v) for x in IOS_DEVICES for v in x[1]}
            else:
                allowed=devices
            if (r.get("device_model"), r.get("os_version")) not in allowed:
                add("error", "$.resource", f"{r.get('device_model')} never shipped Android {r.get('os_version')}.", "device_os")
            for span in payload["data"].get("spans", []):
                a = _vals(span["attributes"])
                if a.get("emb.type") == "perf.network_request":
                    if span["end_time_unix_nano"] <= span["start_time_unix_nano"]:
                        add("error", span["name"], "Network request with zero or negative duration.", "duration")
                    code = a.get("http.response.status_code")
                    failed = code is None or int(code) >= 400
                    if (span["status"].lower() == "error") != failed:
                        add("error", span["name"], "Network span status does not match its HTTP status.", "status")
        elif d["target"] == "embrace":
            from .web_contract import check
            check(payload, lambda message: add("error", "$.embrace." + d["kind"], message, "web_contract"))
        elif d["target"] == "otlp" and d["kind"] == "traces":
            for rs in payload["resourceSpans"]:
                res = _vals(rs["resource"]["attributes"])
                svc, lang = res.get("service.name"), res.get("telemetry.sdk.language")
                if svc and not (SERVICE_NAME.match(svc) or svc in SERVICE_EXCEPTIONS):
                    add("error", svc, f"Service name {svc} does not follow the enterprise naming conventions.", "service_name")
                for ss in rs["scopeSpans"]:
                    for span in ss["spans"]:
                        if int(span["endTimeUnixNano"]) < int(span["startTimeUnixNano"]):
                            add("error", span["name"], "Span ends before it starts.", "duration")
                        a = _vals(span["attributes"])
                        status = a.get("http.response.status_code")
                        if span["kind"] == 2 and isinstance(status, int) and status >= 500 and span["status"]["code"] != 2:
                            add("error", span["name"], "Server span with a 5xx status is not marked as an error.", "status")
                        for e in span.get("events", []):
                            ea = _vals(e.get("attributes"))
                            stack = ea.get("exception.stacktrace")
                            shape = STACK_SHAPE.get(lang)
                            if stack and shape and not shape.search(stack):
                                add("error", span["name"], f"{svc} runs {lang} but its stack trace is not {lang}-shaped.", "stack_language")
    return issues


def lint_dry_run(directory, limit=400):
    import glob
    import os
    files = sorted(glob.glob(os.path.join(directory, "*.json")))
    step = max(1, len(files) // limit)
    return lint_payloads([json.load(open(f)) for f in files[::step]])
