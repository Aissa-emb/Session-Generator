"""Execute a ScenarioSpec: live streams, bursts and backfills over the V1 engine.

Time model: every session has a planned start time. A session dated more than
eight minutes before it is sent carries no traceparent and produces no
backend trace (limits.trace_eligible), on every platform, so a backfill never
creates "View trace" links that open empty traces.
"""
import heapq
import itertools
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import catalog, limits
from .android_session import shift_envelope
from .emitted import Emitted
from .planner import Planner
from .runner import DIURNAL, ROOT, Generator, append_manifest, describe
from .scenarios import Runtime
from .spec import errors, normalized, traffic_window, validate
from .util import hex_id
from .web_session import shift

TRACE_KEYS = ("emb.w3c_traceparent", "emb.forward_telemetry")


def app_overrides(target, spec):
    """Android app definition for each line of business, from the target."""
    if target["platform"] == "web":
        return {catalog.PROFILE_FOR[lob]: {"api_host": host} for lob, host in target["api_domains"].items()}
    a = target["android"]
    name = target.get("app_name") or "Vantra"
    versions = []
    for v in a["versions"]:
        code = v.get("version_code")
        if not code:
            parts = [int(x) for x in v["version"].split(".")] + [0, 0]
            code = str(parts[0] * 100000 + parts[1] * 1000 + parts[2] * 10)
        versions.append((v["version"], code, v.get("sdk_version") or a.get("sdk_version", "9.3.1"), v["share"] * 100))
    props = spec["population"]["session_properties"]
    out = {}
    for lob in catalog.LINES_OF_BUSINESS:
        out[catalog.PROFILE_FOR[lob]] = {
            "package": a["package"], "app_name": name, "activity": f"{a['package']}.ui.MainActivity",
            "versions": versions, "ui": a.get("ui_toolkit", "compose"),
            "user_agent": name.replace(" ", "") + "/{version} (Android {os}; {model}) okhttp/4.12.0",
            "api_host": target["api_domains"].get(lob),
            "markets": list(props["market"].items()), "entry_points": list(props["entry_point"].items())}
    return out


def curve_factor(spec, t_s):
    c = spec["traffic"].get("daily_curve") or {"kind": "flat"}
    if c["kind"] == "flat":
        return 1.0
    weights = DIURNAL if c["kind"] == "diurnal" else c["hourly_weights"]
    hour = datetime.fromtimestamp(t_s, ZoneInfo(c.get("time_zone", "America/New_York"))).hour
    return weights[hour] / (sum(weights) / 24)


def planned_starts(spec, count, start_ms, end_ms, rng):
    """Session start times over a window, following the daily curve, in chronological order."""
    grid = max(1, int((end_ms - start_ms) // 60_000))
    weights = [curve_factor(spec, (start_ms + i * 60_000) / 1000) for i in range(grid)]
    minutes = rng.choices(range(grid), weights=weights, k=count)
    return sorted(min(end_ms, start_ms + m * 60_000 + rng.uniform(0, 60_000)) for m in minutes)


class SpecRun:
    def __init__(self, spec, cfg, persist=True, now_ms=None, state_dir=None, rng=random):
        issues = validate(spec, now_ms)
        if errors(issues):
            raise ValueError("Spec is invalid:\n" + "\n".join(f"  {i['path']}: {i['message']}" for i in errors(issues)))
        self.issues = issues
        self.spec = normalized(spec)
        self.cfg, self.rng = cfg, rng
        self.now_ms = now_ms if now_ms is not None else time.time() * 1000
        self.planner = Planner(self.spec, rng)
        self.mode = self.spec["destinations"]["mode"]
        self.run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + hex_id(3, rng)
        runtime = Runtime()
        pop = self.spec["population"]
        keys = [catalog.PROFILE_FOR[lob] for lob in catalog.LINES_OF_BUSINESS if pop["line_of_business_mix"].get(lob, 0) > 0]
        self.generators = {}
        for target in self.spec["targets"]:
            tcfg = dict(cfg, embrace=dict(cfg.get("embrace", {}), apps={"default": target["embrace_app_id"]}),
                        generation={})
            gen = Generator(keys, tcfg, emit_client_spans=self.spec["destinations"].get("emit_client_spans", False),
                            backend_logs=self.spec["destinations"].get("backend_logs", False), persist=persist,
                            platform=target["platform"], shared=True, app_overrides=app_overrides(target, self.spec),
                            pool_options={"returning_share": pop["returning_share"],
                                          "android_overrides": pop.get("android"), "web_overrides": pop.get("web")}, runtime=runtime,
                            state_dir=state_dir, web_options={"web": target["web"], "performance": self.spec["web_performance"],
                                "population": pop.get("web", {}), "app_name": target["app_name"]} if target["platform"] == "web" else None)
            gen.signed_in_share = pop["signed_in_share"]
            gen.membership_weights = list(pop["session_properties"]["membership_tier"].items())
            gen.verification_queue = [self._case(c) for c in self.spec["verification_cases"]
                                      if c.get("target", target["id"]) == target["id"]]
            self.generators[target["id"]] = (target, gen)
        mix = pop["platform_mix"]
        self.target_weights = [(tid, mix.get(tid, 0)) for tid in self.generators]
        self.emitted = Emitted(self.spec)
        self.manifest = None

    @staticmethod
    def _case(case):
        settings = {k: case[k] for k in ("incident_phase", "primary_scenario", "crash_family", "rollout_phase",
                                         "blockage_cause") if k in case}
        if case.get("full_journey"):
            settings["journey_weights"] = {f"{p}_priority": 1 for p in catalog.PROFILE_FOR.values()}
        variant = catalog.BACKEND_VARIANTS.get(case.get("backend_fault"), {})
        return {"name": case["name"], "profile": catalog.PROFILE_FOR[case["line_of_business"]],
                "incident": variant.get("v1_incident"), "settings": settings}

    def _pick_target(self):
        tid = self.rng.choices([t for t, _ in self.target_weights], weights=[w for _, w in self.target_weights])[0]
        return self.generators[tid]

    # --- one session ---------------------------------------------------------------------------------------

    def _verification(self, start_ms):
        for target, gen in self.generators.values():
            if gen.verification_queue:
                s, journey, deliveries = gen.session(start_ms)
                if self.mode == "sessions_only" or not limits.trace_eligible(start_ms, self.now_ms):
                    deliveries = strip_traces(s, deliveries)
                return target, s, journey, deliveries, []
        return None

    def session(self, start_ms, skel=None, fault_ids=None):
        if skel is None:
            target, gen = self._pick_target()
            skel = self.planner.skeleton(gen, target, start_ms, self.now_ms, self.mode)
            fault_ids = self.planner.allocate_one(skel)
        target, gen = self.generators[skel["target"]]
        assignment = self.planner.assignment(skel, fault_ids)
        s, journey, deliveries = gen.session(start_ms, skeleton=skel, assignment=assignment, settings=skel["settings"])
        return target, s, journey, deliveries, fault_ids

    def record(self, target, s, fault_ids, deliveries):
        self.emitted.add(target, s, deliveries)
        if self.manifest:
            trace_ids = sorted({r["trace_id"] for r in s.plan.evidence}) if s.traces else []
            append_manifest(self.manifest, {
                "run_id": self.run_id, "target": target["id"], "app_id": s.app_id, "session_id": s.user_session_id,
                "device_id": getattr(s, "device_id", s.persona["device_id"]), "start_ms": s.start_ms, "end_ms": s.end_ms,
                "planned_start_ms": getattr(s, "planned_start_ms", s.start_ms),
                "line_of_business": s.properties.get("business_unit"), "faults": fault_ids,
                "verification_case": s.verification_case, "traces": bool(s.traces), "trace_ids": trace_ids,
                "part_ids": sorted({_part_id(d) for d in deliveries
                                    if d.get("fmt") == "envelope" and d["kind"] == "spans"} - {None})
                            + [p.id for p in getattr(s, "parts", [])]})

    def open_manifest(self, directory=None):
        directory = directory or os.path.join(ROOT, ".runs", self.run_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.manifest = os.path.join(directory, "manifest.jsonl")
        append_manifest(self.manifest, {"provenance": "generated", "run_id": self.run_id, "spec": self.spec,
                                        "issues": self.issues, "created_ms": self.now_ms})
        return self.manifest

    def save(self):
        for _, gen in self.generators.values():
            gen.save()

    # --- bounded jobs ----------------------------------------------------------------------------------------

    def plan_batch(self):
        start_ms, end_ms = traffic_window(self.spec, self.now_ms)
        starts = planned_starts(self.spec, self.spec["traffic"]["count"], start_ms, end_ms, self.rng)
        skeletons = []
        for t in starts:
            target, gen = self._pick_target()
            skeletons.append(self.planner.skeleton(gen, target, t, self.now_ms, self.mode))
        allocations, report = self.planner.allocate_batch(skeletons)
        return skeletons, allocations, report

    def run_batch(self, sender, quiet=False, pace=None):
        """Burst or backfill: plan every session, generate them in order, then send."""
        skeletons, allocations, report = self.plan_batch()
        sessions, all_deliveries = [], []
        n_cases = sum(len(g.verification_queue) for _, g in self.generators.values())
        window = traffic_window(self.spec, self.now_ms)
        for t in planned_starts(self.spec, n_cases, window[0], window[1], self.rng) if n_cases else []:
            out = self._verification(t)
            if out:
                sessions.append(out)
                all_deliveries.extend(out[3])
        for skel, fault_ids in zip(skeletons, allocations):
            out = self.session(skel["start_ms"], skel, fault_ids)
            sessions.append(out)
            all_deliveries.extend(out[3])
        kept = []
        for target, s, journey, deliveries, fault_ids in sessions:
            # Sessions keep their planned time; one that would still be running at send time moves back just
            # far enough to have finished, so planned incident phases and trace eligibility stay accurate.
            s.planned_start_ms = s.start_ms
            delta_ms = min(0, self.now_ms - 15000 - max((d["due"] for d in deliveries), default=self.now_ms))
            for d in deliveries:
                (shift_envelope if d.get("fmt") == "envelope" else shift)(d["payload"], int(delta_ms * 1_000_000))
                d["due"] += delta_ms
            s.start_ms += delta_ms
            s.end_ms += delta_ms
            if s.traces and not limits.trace_eligible(s.start_ms, self.now_ms):
                deliveries = strip_traces(s, deliveries)
            self.record(target, s, fault_ids, deliveries)
            kept.extend(deliveries)
            if not quiet:
                print(describe(s, journey, (self.spec.get("chronosphere") or {}).get("tenant") if s.traces else None))
        self.save()
        kept.sort(key=lambda d: d["due"])
        backend = [d for d in kept if d["target"] != "embrace"]
        for d in backend:
            sender.submit(d)
        sender.pool.shutdown(wait=True)
        sender.pool = ThreadPoolExecutor(max_workers=8)
        rate = pace or self.spec["traffic"].get("throttle_payloads_per_second") or (
            limits.DEFAULT_BACKFILL_PAYLOADS_PER_SECOND if self.spec["traffic"]["mode"] == "backfill" else None)
        for i, d in enumerate(x for x in kept if x["target"] == "embrace"):
            if rate and i:
                time.sleep(1 / rate)
            sender.submit(d)
            if rate and sender.stats["errors"] >= 20:
                print("Stopping: repeated delivery failures.", file=sys.stderr)
                break
        return len(sessions), report

    # --- live streams -------------------------------------------------------------------------------------------

    def run_live(self, sender, quiet=False, status_every_s=600, clock=time.time, sleep=time.sleep):
        t = self.spec["traffic"]
        heap, seq = [], itertools.count()
        duration = t.get("duration_minutes", 0)
        stop_at = clock() + duration * 60 if duration else None
        next_start, started, last_save, last_status = clock(), 0, clock(), clock()
        tenant = (self.spec.get("chronosphere") or {}).get("tenant")
        try:
            while True:
                now = clock()
                if (stop_at is None or now < stop_at) and now >= next_start:
                    self.now_ms = now * 1000
                    out = self._verification(now * 1000) or self.session(now * 1000)
                    target, s, journey, deliveries, fault_ids = out
                    self.record(target, s, fault_ids, deliveries)
                    started += 1
                    for d in deliveries:
                        heapq.heappush(heap, (d["due"], next(seq), d))
                    if not quiet:
                        print(describe(s, journey, tenant if s.traces else None), flush=True)
                    rate = t["rate_per_minute"] * curve_factor(self.spec, now)
                    next_start = now + self.rng.expovariate(max(1e-6, rate) / 60.0)
                while heap and heap[0][0] <= clock() * 1000:
                    sender.submit(heapq.heappop(heap)[2])
                if now - last_save > 60:
                    self.save()
                    last_save = now
                if now - last_status > status_every_s:
                    st = sender.stats
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] sessions {started}, Embrace payloads "
                          f"{st['embrace_spans'] + st['embrace_logs']}, backend exports {st['otlp_traces']}, "
                          f"errors {st['errors']}, queued {len(heap)}", flush=True)
                    last_status = now
                if stop_at is not None and now >= stop_at and not heap:
                    break
                wake = [next_start] if (stop_at is None or now < stop_at) else []
                if heap:
                    wake.append(heap[0][0] / 1000)
                sleep(max(0.05, min(1.0, min(wake) - clock())) if wake else 0.2)
        except KeyboardInterrupt:
            print(f"\nStopping: flushing {len(heap)} pending payloads now.", file=sys.stderr)
            while heap:
                sender.submit(heapq.heappop(heap)[2])
        self.save()
        return started


def _part_id(delivery):
    span = delivery["payload"]["data"]["spans"][0]
    return next((a["value"] for a in span["attributes"] if a["key"] == "emb.session_part_id"), None)


def strip_traces(session, deliveries):
    """Remove trace context from a session that Cortex XCOR can no longer ingest; drop its backend data."""
    session.traces = False
    kept = []
    for d in deliveries:
        if d["target"] != "embrace":
            continue
        if d.get("fmt") == "envelope":
            for span in d["payload"]["data"].get("spans", []):
                span["attributes"] = [a for a in span["attributes"] if a["key"] not in TRACE_KEYS]
        else:
            for rs in d["payload"].get("resourceSpans", []):
                for ss in rs["scopeSpans"]:
                    for span in ss["spans"]:
                        span["attributes"] = [a for a in span["attributes"] if a["key"] not in TRACE_KEYS]
        kept.append(d)
    session.deliveries = kept
    return kept
