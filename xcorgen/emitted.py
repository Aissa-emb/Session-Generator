"""Requested versus emitted: measure what a run actually produced, from its payloads.

Every number here is read from generated Embrace envelopes and OTLP traces:
session attributes from the session span and resource, outcomes from crash
and exception logs, blockage spans, network spans and backend spans. The plan
that produced the payloads is never consulted, so a builder that fails to
emit a planned outcome shows up as a shortfall.
"""
import glob
import json
import math
import os
import re

from . import catalog
from .personas import DEVICE_TIERS
from .spec import normalized

_ROUTES = None


def routes():
    """Network span path -> endpoint key, per line of business."""
    global _ROUTES
    if _ROUTES is None:
        from .operations import install
        from .profiles import load
        _ROUTES = {}
        for lob, key in catalog.PROFILE_FOR.items():
            profile = load(key)
            install(profile)
            _ROUTES[lob] = [(re.compile("^" + re.sub(r":[A-Za-z]+", "[^/]+", ep.route) + "$"), ep.method, name)
                            for name, ep in profile.endpoints.items()]
    return _ROUTES


def endpoint_of(lob, method, path):
    return next((name for rx, m, name in routes().get(lob, []) if m == method and rx.match(path)), None)


def _attrs(item):
    return {a["key"]: a["value"] for a in item.get("attributes", [])}


def observe(platform, envelopes, traces):
    """One session's measurable facts. envelopes: Android Embrace envelopes; traces: trace_id -> spans."""
    obs = {"platform": platform, "net": [], "crash": False, "handled": set(), "blockage": False, "cold_start_ms": None,
           "roots": {}, "signed_in": False, "traces": False, "breadcrumbs": set(), "logs": []}
    first = None
    for env in envelopes:
        data = env["data"]
        for span in data.get("spans", []):
            a = _attrs(span)
            dur = (span["end_time_unix_nano"] - span["start_time_unix_nano"]) / 1e6
            if span["name"] == "emb-session":
                obs["breadcrumbs"].update(_attrs(e).get("message") for e in span.get("events", [])
                                          if e["name"] == "emb-breadcrumb")
                if first is None or span["start_time_unix_nano"] < first[0]:
                    first = (span["start_time_unix_nano"], env)
                obs.update(lob=a.get("emb.properties.business_unit"), ring=a.get("emb.properties.release_ring", "stable"),
                           session_id=a.get("emb.user_session_id"), start_ms=int(a.get("emb.user_session_start_ts", 0)))
            elif a.get("emb.type") == "perf.thread_blockage":
                obs["blockage"] = True
            elif span["name"] == "emb-app-startup-cold":
                obs["cold_start_ms"] = dur
            elif a.get("emb.type") == "perf.network_request":
                method, _, path = span["name"][4:].partition(" ")
                obs["traces"] |= "emb.w3c_traceparent" in a
                obs["net"].append({"method": method, "path": path, "status": int(a["http.response.status_code"])
                                   if a.get("http.response.status_code") else None, "error_type": a.get("error.type"),
                                   "ms": dur, "trace_id": span["trace_id"], "start_ms": span["start_time_unix_nano"] / 1e6})
            elif a.get("app.operation"):
                obs["roots"].setdefault(span["name"], []).append(dur)
        for log in data.get("logs", []):
            a = _attrs(log)
            obs["crash"] |= a.get("emb.type") == "sys.android.crash"
            if a.get("emb.type") == "sys.exception" and a.get("emb.exception_handling") == "handled":
                obs["handled"].add(a.get("exception.type"))
            elif a.get("emb.type") == "sys.log":
                obs["logs"].append(log.get("body", ""))
        r = env["resource"]
        obs.update(app_version=r.get("app_version"), os_version=r.get("os_version"), model=r.get("device_model"))
    if first:
        obs["signed_in"] = bool(first[1]["metadata"].get("user_id"))
    obs["tier"] = DEVICE_TIERS.get(obs.get("model"), "mid")
    for n in obs["net"]:
        n["endpoint"] = endpoint_of(obs.get("lob"), n["method"], n["path"])
        n["spans"] = traces.get(n["trace_id"], [])
    obs["endpoints"] = {n["endpoint"] for n in obs["net"]}
    return obs


def _backend_hit(rule, n):
    spans = n["spans"]
    if "span_error" in rule:
        return any(s["name"] == rule["span_error"] and s.get("status", {}).get("code") == 2 for s in spans)
    if "error_contains" in rule:
        return any(rule["error_contains"] in json.dumps(s.get("events", [])) for s in spans)
    if "repeated" in rule:
        return sum(s["name"] == rule["repeated"] for s in spans) >= rule["min"]
    if "slow_span" in rule:
        for s in spans:
            a = {x["key"]: next(iter(x["value"].values()), None) for x in s.get("attributes", [])}
            if s["name"] == rule["slow_span"] and (not rule.get("peer") or a.get("peer.service") == rule["peer"]):
                if (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6 >= rule["min_ms"]:
                    return n["status"] is not None and n["status"] < 400
        return False
    if rule.get("error_ok_client"):
        return n["status"] is not None and n["status"] < 400 and any(s.get("status", {}).get("code") == 2 for s in spans)
    return False


def _client_hit(rule, n):
    if "status" in rule:
        return n["status"] == rule["status"]
    if "status_at_least" in rule:
        return n["status"] is not None and n["status"] >= rule["status_at_least"]
    if "slow_ms" in rule:
        # A slow successful response; transport timeouts and error responses are other faults.
        return n["status"] is not None and n["status"] < 400 and n["ms"] >= rule["slow_ms"]
    return False


def affected(fault, obs):
    """Whether a session's payloads show the fault."""
    d, t = fault.details, fault.type
    if fault.variant:
        endpoint = fault.meta["endpoints"].get(obs.get("lob"))
        ev = fault.meta["evidence"]
        client = ev.get("client", {})
        if "breadcrumb" in client and not any(n["spans"] for n in obs["net"] if n["endpoint"] == endpoint):
            return client["breadcrumb"] in obs["breadcrumbs"]
        if "log_contains" in client and not any(n["spans"] for n in obs["net"] if n["endpoint"] == endpoint):
            return any(client["log_contains"] in body for body in obs["logs"])
        if client.get("unless_breadcrumb") in obs["breadcrumbs"]:
            return False
        for n in obs["net"]:
            if n["endpoint"] != endpoint:
                continue
            if "backend" in ev and n["spans"]:
                if _backend_hit(ev["backend"], n):
                    return True
            elif "client" in ev and _client_hit(ev["client"], n):
                return True
        return False
    if t == "jvm_crash":
        return obs["crash"]
    if t == "main_thread_blockage":
        return obs["blockage"]
    if t == "handled_exception":
        expected = (d.get("exception") or "java.lang.NumberFormatException").rsplit(".", 1)[-1]
        return expected in obs["handled"]
    if t == "network_failure":
        return any(n["status"] is None and n["error_type"] and (not d.get("endpoint") or n["endpoint"] == d["endpoint"])
                   for n in obs["net"])
    if t == "slow_startup":
        return (obs["cold_start_ms"] or 0) >= min(4000, d.get("min_ms", 4000))
    if t == "slow_root_span":
        return any(ms >= d["p50_ms"] * .5 for ms in obs["roots"].get(d["span"], []))
    if t == "http_error":
        return any(n["endpoint"] == d["endpoint"] and n["status"] == d["status"] for n in obs["net"])
    return False


def eligible(fault, obs, by_id, results):
    if obs.get("lob") not in fault.lobs:
        return False
    flt = fault.filters
    for dim, key in (("app_versions", "app_version"), ("os_versions", "os_version"), ("device_models", "model"),
                     ("device_tiers", "tier"), ("release_rings", "ring")):
        if flt.get(dim) and obs.get(key) not in flt[dim]:
            return False
    if flt.get("signed_in") is not None and flt["signed_in"] != obs["signed_in"]:
        return False
    if fault.with_fault and not results.get(fault.with_fault, {}).get(obs["session_id"]):
        return False
    if not fault.steer:
        endpoint = fault.endpoint(obs["lob"])
        needed = set(flt.get("endpoints") or []) | ({endpoint} if endpoint not in (None, "content", "startup") else set())
        if not needed <= obs["endpoints"]:
            return False
    return True


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(q * len(values)) - 1))]


class Emitted:
    def __init__(self, spec):
        from .planner import Planner
        self.spec = normalized(spec)
        self.planner = Planner(self.spec)
        self.observations = []

    def add(self, target, session, deliveries):
        envelopes = [d["payload"] for d in deliveries if d.get("fmt") == "envelope"]
        traces = {}
        for d in deliveries:
            if d["target"] == "otlp" and d["kind"] == "traces":
                for rs in d["payload"]["resourceSpans"]:
                    for ss in rs["scopeSpans"]:
                        for span in ss["spans"]:
                            traces.setdefault(span["traceId"], []).append(span)
        obs = observe(target["platform"], envelopes, traces)
        obs["verification_case"] = getattr(session, "verification_case", None)
        # The spec's rate applies at the session's planned time (it differs only for sessions moved back
        # so they had finished by send time).
        obs["planned_start_ms"] = getattr(session, "planned_start_ms", obs.get("start_ms"))
        self.observations.append(obs)

    def summary(self):
        """Per fault: requested and emitted share of eligible sessions, from payloads only."""
        population = [o for o in self.observations if not o.get("verification_case")]
        results, rows = {}, []
        for f in self.planner.faults:
            elig = [o for o in population if eligible(f, o, self.planner.by_id, results)]
            hits = {o["session_id"]: affected(f, o) for o in elig}
            results[f.id] = hits
            requested = sum(f.requested_at(o.get("planned_start_ms") or o["start_ms"]) for o in elig)
            count = sum(hits.values())
            row = {"id": f.id, "type": f.type, "variant": f.variant, "quota": f.quota, "eligible": len(elig),
                   "requested_sessions": round(requested, 2), "emitted_sessions": count,
                   "requested_pct": round(100 * requested / len(elig), 2) if elig else None,
                   "emitted_pct": round(100 * count / len(elig), 2) if elig else None}
            if elig:
                row["delta_pp"] = round(row["emitted_pct"] - row["requested_pct"], 2)
                exact = f.quota == "exact" or (f.quota == "auto" and self.spec["traffic"]["mode"] != "live")
                ev = (f.meta.get("evidence") or {}) if f.variant else {}
                latency_only = "slow_ms" in ev.get("client", {}) and not (
                    "backend" in ev and any(o["traces"] for o in elig))
                if latency_only:
                    # Without backend traces a slow request is the only evidence, and natural latency overlaps it.
                    row["evidence"] = "client latency"
                    row["within_tolerance"] = abs(row["delta_pp"]) <= 2
                elif exact:
                    row["within_tolerance"] = abs(count - requested) <= 1.5
                else:
                    sigma = math.sqrt(max(requested * (1 - requested / len(elig)), 1e-9))
                    row["within_tolerance"] = abs(count - requested) <= max(3 * sigma, .02 * len(elig))
            if f.type == "slow_root_span":
                spans = [ms for o in elig if hits[o["session_id"]] for ms in o["roots"].get(f.details["span"], [])]
                row.update(requested_p50_ms=f.details["p50_ms"], requested_p95_ms=f.details["p95_ms"],
                           emitted_p50_ms=round(percentile(spans, .5) or 0), emitted_p95_ms=round(percentile(spans, .95) or 0))
            rows.append(row)
        sessions = len(population)
        return {"sessions": sessions, "verification_sessions": len(self.observations) - sessions,
                "sessions_with_traces": sum(o["traces"] for o in population), "faults": rows}

    def table(self):
        s = self.summary()
        lines = [f"Requested versus emitted ({s['sessions']} sessions, {s['sessions_with_traces']} with traces"
                 + (f", {s['verification_sessions']} verification sessions excluded" if s["verification_sessions"] else "") + ")",
                 f"  {'fault':<28} {'type':<22} {'eligible':>8} {'requested':>10} {'emitted':>9} {'delta':>7}  result"]
        for r in s["faults"]:
            if r["eligible"]:
                verdict = "ok" if r.get("within_tolerance") else "OUT OF TOLERANCE"
                lines.append(f"  {r['id']:<28} {(r['variant'] or r['type']):<22} {r['eligible']:>8} {r['requested_pct']:>9.2f}% "
                             f"{r['emitted_pct']:>8.2f}% {r['delta_pp']:>+6.2f}  {verdict}")
                if "emitted_p50_ms" in r:
                    lines.append(f"  {'':<28} p50 {r['emitted_p50_ms']} ms (requested {r['requested_p50_ms']}), "
                                 f"p95 {r['emitted_p95_ms']} ms (requested {r['requested_p95_ms']})")
            else:
                lines.append(f"  {r['id']:<28} {(r['variant'] or r['type']):<22} {0:>8}  no eligible sessions")
        return "\n".join(lines)


def from_dry_run(directory, spec):
    """Rebuild per-session observations from a dry-run directory."""
    report = Emitted(spec)
    by_session, traces, trace_owner = {}, {}, {}
    for fn in sorted(glob.glob(os.path.join(directory, "*.json"))):
        doc = json.load(open(fn))
        if doc.get("fmt") == "envelope":
            env = doc["payload"]
            sid = None
            for item in env["data"].get("spans", []) + env["data"].get("logs", []):
                sid = _attrs(item).get("emb.user_session_id")
                if sid:
                    break
            by_session.setdefault(sid, []).append(env)
        elif doc["target"] == "otlp" and doc["kind"] == "traces":
            for rs in doc["payload"]["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    for span in ss["spans"]:
                        traces.setdefault(span["traceId"], []).append(span)
    cases, planned = set(), {}
    manifest = os.path.join(directory, "manifest.jsonl")
    if os.path.exists(manifest):
        for line in open(manifest):
            rec = json.loads(line)
            if rec.get("verification_case"):
                cases.add(rec["session_id"])
            if rec.get("planned_start_ms"):
                planned[rec["session_id"]] = rec["planned_start_ms"]
    for sid, envelopes in by_session.items():
        obs = observe("android", envelopes, traces)
        obs["verification_case"] = sid if sid in cases else None
        obs["planned_start_ms"] = planned.get(sid, obs.get("start_ms"))
        report.observations.append(obs)
    return report
