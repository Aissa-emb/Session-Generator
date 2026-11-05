"""Execute a ScenarioSpec: live streams, bursts and backfills over the V1 engine.

Time model: every session has a planned start time. A session dated more than
eight minutes before it is sent carries no traceparent and produces no
backend trace (limits.trace_eligible), on every platform, so a backfill never
creates "View trace" links that open empty traces.
"""
import copy
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
from .journal import RunJournal, digest, tuples
from .android_session import shift_envelope
from .emitted import Emitted
from .planner import Planner
from .runner import DIURNAL, ROOT, Generator, Sender, append_manifest, describe
from .scenarios import Runtime
from .spec import errors, normalized, traffic_window, validate
from .util import hex_id
from .web_session import shift

TRACE_KEYS = ("emb.w3c_traceparent", "emb.forward_telemetry")


def app_overrides(target, spec):
    """Android app definition for each line of business, from the target."""
    if target["platform"] == "web":
        return {catalog.PROFILE_FOR[lob]: {"api_host": host} for lob, host in target["api_domains"].items()}
    a = target["ios"] if target["platform"]=="ios" else target["android"]
    package=a.get("bundle_id") or a.get("package")
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
            "package": package, "app_name": name, "activity": f"{package}.ui.MainActivity",
            "versions": versions, "ui": a.get("ui_toolkit", "compose"),
            "user_agent": name.replace(" ", "") + ("/{version} ({model}; iOS {os})" if target["platform"]=="ios" else "/{version} (Android {os}; {model}) okhttp/4.12.0"),
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
        if self.spec.get("causal") or self.spec.get("modeling"):
            import uuid
            self.run_id = self.spec.get("causal", {}).get("replay_namespace") or uuid.uuid4().hex
        self._causal_index = 0
        self._model_index = 0
        self.causal_evidence = []
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
                                          "android_overrides": pop.get("android"), "ios_overrides":pop.get("ios"), "web_overrides": pop.get("web"),
                                          "population": pop}, runtime=runtime,
                            state_dir=state_dir, web_options={"web": target["web"], "performance": self.spec["web_performance"],
                                "population": pop.get("web", {}), "app_name": target["app_name"]} if target["platform"] == "web" else None)
            gen.cross_platform_customers = len({t["platform"] for t in self.spec["targets"]}) > 1
            gen.population_settings = pop
            gen.signed_in_share = pop["signed_in_share"]
            gen.membership_weights = list(pop["session_properties"]["membership_tier"].items())
            gen.verification_queue = [self._case(c) for c in self.spec["verification_cases"]
                                      if c.get("target", target["id"]) == target["id"]]
            self.generators[target["id"]] = (target, gen)
        mix = pop["platform_mix"]
        self.target_weights = [(tid, mix.get(tid, 0)) for tid in self.generators]
        self.emitted = Emitted(self.spec, streaming=self.spec["traffic"]["mode"] == "live" or self.spec["traffic"].get("count", 0) > 2000)
        from .backend_metrics import REDMetrics
        self.metrics = REDMetrics()
        self.manifest = None
        self.journal = None
        self.resuming = False
        self.session_index = 0

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
        if self.spec.get("causal"):
            from .causal.adapter import create
            targets = list(self.generators)
            tid = skel["target"] if skel else targets[self._causal_index % len(targets)]
            target = self.generators[tid][0]
            session, journey, deliveries = create(target, self.spec, start_ms, self._causal_index, self.run_id)
            self._causal_index += 1
            if len(self.causal_evidence) < 120:
                self.causal_evidence.append(session.causal_evidence)
            if not limits.trace_eligible(start_ms, self.now_ms):
                deliveries = strip_traces(session, deliveries)
            return target, session, journey, deliveries, []
        if skel is None:
            target, gen = self._pick_target()
            skel = self.planner.skeleton(gen, target, start_ms, self.now_ms, self.mode)
            fault_ids = self.planner.allocate_one(skel)
        target, gen = self.generators[skel["target"]]
        assignment = self.planner.assignment(skel, fault_ids)
        gen.model_context = (self.spec.get("modeling"), self.spec["seed"] if self.spec.get("seed") is not None else 7331, self._model_index, target["id"])
        self._model_index += 1
        if self.spec.get("modeling"):
            from .distributions import rng_for
            from .util import identity_scope
            with identity_scope(rng_for("emission", self.run_id, self._model_index - 1)):
                s, journey, deliveries = gen.session(start_ms, skeleton=skel, assignment=assignment, settings=skel["settings"])
        else:
            s, journey, deliveries = gen.session(start_ms, skeleton=skel, assignment=assignment, settings=skel["settings"])
        return target, s, journey, deliveries, fault_ids

    def record(self, target, s, fault_ids, deliveries):
        if self.spec["destinations"].get("backend_metrics") and s.traces:
            payload = self.metrics.collect(deliveries, self.run_id)
            if payload["resourceMetrics"]:
                due = max((d['due'] for d in deliveries), default=s.end_ms)
                metric_delivery = dict(target="otlp", kind="metrics", payload=payload, due=due,
                    app_id=s.app_id, device_id=s.persona['device_id'],
                    _metrics_policy=self.spec['destinations'].get('metrics_policy','aggregated'))
                from .linter import lint_payloads
                if errors(lint_payloads([metric_delivery])):
                    raise ValueError("Realism lint failed for backend metrics; this session was not sent.")
                deliveries.append(metric_delivery)
        self.emitted.add(target, s, deliveries)
        if self.manifest:
            trace_ids = expected_trace_ids(s, deliveries)
            record = {
                "index": self.session_index, "run_id": self.run_id, "dry_run": self.dry_run, "target": target["id"], "platform": target["platform"], "app_id": s.app_id, "session_id": s.user_session_id,
                "device_id": getattr(s, "device_id", s.persona["device_id"]), "start_ms": s.start_ms, "end_ms": s.end_ms,
                "planned_start_ms": getattr(s, "planned_start_ms", s.start_ms),
                "line_of_business": s.properties.get("business_unit"), "faults": fault_ids,
                "verification_case": s.verification_case, "traces": bool(s.traces), "trace_ids": trace_ids,
                "sdk_sessions": self.emitted.last_observation.get("sdk_sessions", []),
                "part_ids": sorted({_part_id(d) for d in deliveries
                                    if d.get("fmt") == "envelope" and d["kind"] == "spans"} - {None})
                            + [p.id for p in getattr(s, "parts", [])]}
            if hasattr(s, "causal_evidence"):
                record["causal"] = s.causal_evidence
                record["trace_ids"] = sorted({sp["traceId"] for d in deliveries if d["target"] == "otlp" and d["kind"] == "traces"
                    for rs in d["payload"]["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]})
            if hasattr(s, "performance_model"):
                record["modeling"] = s.performance_model.evidence()
            if self.journal:
                self.journal.plan_session(self.session_index, record, deliveries)
            else:
                append_manifest(self.manifest, record)
            self.session_index += 1

    def open_manifest(self, directory=None, resume=False, dry=False):
        self.dry_run = dry
        if self.spec.get("causal", {}).get("replay_namespace") and not dry:
            raise ValueError("Exact-ID replay is restricted to local dry runs.")
        directory = directory or os.path.join(ROOT, ".runs", self.run_id)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.manifest = os.path.join(directory, "manifest.jsonl")
        if self.spec["traffic"]["mode"] == "live":
            if resume:
                raise ValueError("A live stream starts a new segment; only bounded jobs resume a frozen plan.")
            append_manifest(self.manifest, {"provenance": "generated", "run_id": self.run_id,
                            "spec": self.spec, "issues": self.issues, "created_ms": self.now_ms})
            return self.manifest
        self.journal = RunJournal(directory)
        meta = self.journal.get("plan")
        try:
            if resume:
                if not meta:
                    raise ValueError("No resumable plan exists in this run directory.")
                if meta["spec_version"] != self.spec["spec_version"] or meta["spec_hash"] != digest(self.spec):
                    raise ValueError("Spec or spec version differs from the original run; resume refused.")
                if meta["dry"] != dry:
                    raise ValueError("A dry run cannot resume as a live run, or the reverse.")
                self.journal.assert_resumable()
                self.run_id, self.now_ms = meta["run_id"], meta["now_ms"]
                random.setstate(tuples(meta["global_rng"]))
                self.rng.setstate(tuples(meta["rng"]))
                for tid, (_, gen) in self.generators.items():
                    for key, pool in gen.pools.items():
                        pool.personas = copy.deepcopy(meta["pools"][tid][key])
                self.resuming = True
            else:
                if meta:
                    raise ValueError("This directory already contains a run; use --resume or choose a new directory.")
                meta = {"run_id": self.run_id, "now_ms": self.now_ms, "spec_version": self.spec["spec_version"],
                        "spec_hash": digest(self.spec), "dry": dry, "rng": self.rng.getstate(),
                        "global_rng": random.getstate(), "pools": {tid: {key: pool.personas for key, pool in gen.pools.items()}
                        for tid, (_, gen) in self.generators.items()}}
                self.journal.put("plan", meta)
                append_manifest(self.manifest, {"provenance": "generated", "run_id": self.run_id,
                                "spec": self.spec, "issues": self.issues, "created_ms": self.now_ms})
        except BaseException:
            self.journal.close()
            raise
        return self.manifest

    def _acknowledge(self, session_index=None):
        # A crash between append and acknowledgement must not duplicate the session receipt.
        if not hasattr(self, '_acknowledged'):
            with open(self.manifest) as stream:
                self._acknowledged = {r["index"] for line in stream if (r := json.loads(line)).get("stage") in ("sent", "expired", "partially_expired")}
        recorded = self._acknowledged
        for index, record in self.journal.completed(session_index):
            if index not in recorded:
                receipts=[json.loads(row[0]) for row in self.journal.db.execute('SELECT receipt FROM deliveries WHERE session_idx=?',(index,)) if row[0]]
                expired=[r for r in receipts if r['status']=='expired']
                trace_ids=set(record['trace_ids'])-{tid for r in expired for tid in r.get('trace_ids',[])}
                app=[r for r in receipts if r['target']=='embrace']
                stage='expired' if app and all(r['status']=='expired' for r in app) else 'partially_expired' if any(r['status']=='expired' for r in app) else 'sent'
                append_manifest(self.manifest, dict(record, stage=stage, trace_ids=sorted(trace_ids), traces=bool(trace_ids), expired_payloads=len(expired), sent_ms=time.time()*1000))
            self.journal.acknowledge_session(index)
            recorded.add(index)

    def _deliver(self, sender, delivery):
        if not self.journal:
            sender.submit(delivery)
            return
        ident = delivery["_delivery_id"]
        if not self.journal.begin(ident):
            return
        if not getattr(sender, "dry", True):
            starts = [int(sp.get("startTimeUnixNano", "0")) / 1e6
                      for rs in delivery["payload"].get("resourceSpans", [])
                      for ss in rs["scopeSpans"] for sp in ss["spans"]]
            record = json.loads(self.journal.db.execute("SELECT record FROM sessions WHERE idx=?", (int(ident.split(":")[0]),)).fetchone()[0])
            now = time.time() * 1000
            age = now - record["start_ms"]
            expired = age > 24 * 3600000 or (delivery["target"] != "embrace" and
                      (age > limits.TRACE_ELIGIBLE_MS or any(now - t > limits.TRACE_ELIGIBLE_MS for t in starts)))
            if expired:
                receipt = {"status": "expired", "target": delivery["target"], "kind": delivery["kind"],
                           "http_status": None, "accepted": 0, "rejected": 0, "reason": "outside_ingest_window",
                           "trace_ids":sorted({sp['traceId'] for rs in delivery['payload'].get('resourceSpans',[]) for ss in rs['scopeSpans'] for sp in ss['spans']})}
                self.journal.receipt(ident, receipt)
                append_manifest(self.manifest + ".receipts", dict(receipt, delivery_id=ident))
                self._acknowledge(int(ident.split(":")[0]))
                return
            if delivery["target"] == "embrace" and age > limits.TRACE_ELIGIBLE_MS:
                payload = copy.deepcopy(delivery["payload"])
                groups = [payload.get("data", {}).get("spans", [])] if delivery.get("fmt") == "envelope" else [
                    ss["spans"] for rs in payload.get("resourceSpans", []) for ss in rs["scopeSpans"]]
                for spans in groups:
                    for span in spans:
                        span["attributes"] = [a for a in span["attributes"] if a["key"] not in TRACE_KEYS]
                delivery = dict(delivery, payload=payload)
        sender.strict_receipts = True
        receipt = sender.submit(delivery).result()
        self.journal.receipt(ident, receipt)
        append_manifest(self.manifest + ".receipts", dict(receipt, delivery_id=ident))
        self._acknowledge(int(ident.split(":")[0]))

    def save(self):
        for _, gen in self.generators.values():
            gen.save()

    # --- bounded jobs ----------------------------------------------------------------------------------------

    def plan_batch(self):
        if self.spec.get("causal"):
            start_ms, end_ms = traffic_window(self.spec, self.now_ms)
            starts = planned_starts(self.spec, self.spec["traffic"]["count"], start_ms, end_ms, self.rng)
            targets = list(self.generators)
            skeletons = [{"target": targets[i % len(targets)], "start_ms": t} for i, t in enumerate(starts)]
            return skeletons, [[] for _ in starts], {"causal_scenario": self.spec["causal"]["scenario"], "sessions": len(starts)}
        start_ms, end_ms = traffic_window(self.spec, self.now_ms)
        starts = planned_starts(self.spec, self.spec["traffic"]["count"], start_ms, end_ms, self.rng)
        skeletons = []
        for t in starts:
            target, gen = self._pick_target()
            skeletons.append(self.planner.skeleton(gen, target, t, self.now_ms, self.mode))
        allocations, report = self.planner.allocate_batch(skeletons)
        return skeletons, allocations, report

    def run_batch(self, sender, quiet=False, pace=None):
        """Plan exact quotas, preflight all payloads on disk, then send backend first."""
        from .payload_spool import PayloadSpool
        from .linter import lint_payloads
        skeletons, allocations, report = self.plan_batch()
        n_cases = sum(len(g.verification_queue) for _, g in self.generators.values())
        window = traffic_window(self.spec, self.now_ms)
        case_starts = planned_starts(self.spec, n_cases, window[0], window[1], self.rng) if n_cases else []
        def generated():
            for t in case_starts:
                out = self._verification(t)
                if out:
                    yield out
            for skel, fault_ids in zip(skeletons, allocations):
                yield self.session(skel["start_ms"], skel, fault_ids)
        count = 0
        with PayloadSpool() as spool:
            for target, s, journey, deliveries, fault_ids in generated():
                if hasattr(self, 'check_control'):
                    self.check_control()
                # Sessions keep their planned time; one that would still be running at send time moves back just
                # far enough to have finished, so planned incident phases and trace eligibility stay accurate.
                s.planned_start_ms = s.start_ms
                delta_ms = min(0, self.now_ms - 15000 - max((d["due"] for d in deliveries), default=self.now_ms))
                for d in deliveries:
                    (shift_envelope if d.get("fmt") == "envelope" else shift)(d["payload"], int(delta_ms * 1_000_000))
                    d["due"] += delta_ms
                s.start_ms += delta_ms
                s.end_ms += delta_ms
                if hasattr(s, "causal_evidence"):
                    s.causal_evidence["base_ms"] += delta_ms
                    for request in s.causal_evidence["frontend_requests"]:
                        request["start"] += delta_ms
                        request["end"] += delta_ms
                if s.traces and not limits.trace_eligible(s.start_ms, self.now_ms):
                    deliveries = strip_traces(s, deliveries)

                blocking = errors(lint_payloads(deliveries))
                if blocking:
                    raise ValueError("Realism lint failed; no payloads were sent: " + "; ".join(x["message"] for x in blocking[:5]))
                self.record(target, s, fault_ids, deliveries)
                for d in deliveries:
                    spool.add(d)
                count += 1
                if not quiet:
                    print(describe(s, journey, (self.spec.get("chronosphere") or {}).get("tenant") if s.traces else None))
            self.save()
            if self.journal:
                self._acknowledge()
            rate = pace or self.spec["traffic"].get("throttle_payloads_per_second") or limits.DEFAULT_BACKFILL_PAYLOADS_PER_SECOND
            backend_drained = False
            for i, d in enumerate(spool):
                if sender.stats["errors"] >= 20:
                    break
                if d['target'] == 'embrace' and not backend_drained:
                    sender.pool.shutdown(wait=True)
                    sender.pool = ThreadPoolExecutor(max_workers=8)
                    backend_drained = True
                if rate and i and not getattr(sender, "dry", True):
                    time.sleep(1 / rate)
                self._deliver(sender, d)
        return count, report

    # --- live streams -------------------------------------------------------------------------------------------

    def run_live(self, sender, quiet=False, status_every_s=600, clock=time.time, sleep=time.sleep):
        from .linter import lint_payloads
        t = self.spec["traffic"]
        heap, seq = [], itertools.count()
        duration = t.get("duration_minutes", 0)
        stop_at = clock() + duration * 60 if duration else None
        next_start, started, last_save, last_status = clock(), 0, clock(), clock()
        tenant = (self.spec.get("chronosphere") or {}).get("tenant")
        queued_bytes, missed = 0, 0
        try:
            while True:
                now = clock()
                count = 0
                while (stop_at is None or clock() < stop_at) and clock() >= next_start and count < 32:
                    now = clock()
                    rate = t["rate_per_minute"] * curve_factor(self.spec, next_start)
                    if now - next_start > 30:
                        missed += max(1, round((now - next_start) * rate / 60))
                        next_start = now
                    if queued_bytes >= 256 * 1024**2:
                        break
                    next_start += self.rng.expovariate(max(1e-6, rate) / 60.0)
                    count += 1
                    self.now_ms = now * 1000
                    out = self._verification(now * 1000) or self.session(now * 1000)
                    target, s, journey, deliveries, fault_ids = out
                    blocking = errors(lint_payloads(deliveries))
                    if blocking:
                        raise ValueError("Realism lint failed; this session was not sent.")
                    size = sum(len(json.dumps(d, separators=(',', ':'))) for d in deliveries)
                    if queued_bytes + size > 256 * 1024**2:
                        missed += 1
                        continue
                    self.record(target, s, fault_ids, deliveries)
                    started += 1
                    queued_bytes += size
                    for d in deliveries:
                        heapq.heappush(heap, (d["due"], next(seq), d, size / max(1, len(deliveries))))
                    if not quiet:
                        print(describe(s, journey, tenant if s.traces else None), flush=True)
                while heap and heap[0][0] <= clock() * 1000:
                    # Collectors used by previews/tests need not implement backpressure.
                    future = sender.submit(heap[0][2], block=False) if isinstance(sender, Sender) else sender.submit(heap[0][2])
                    if future is None and hasattr(sender, 'pending_slots'):
                        break
                    queued_bytes -= heapq.heappop(heap)[3]
                if now - last_save > 5:
                    self.save()
                    last_save = now
                if now - last_status > status_every_s:
                    st = sender.stats
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] sessions {started}, Embrace payloads "
                          f"{st['embrace_spans'] + st['embrace_logs']}, backend exports {st['otlp_traces']}, "
                          f"errors {st['errors']}, queued {len(heap)}, missed arrivals (estimated) {missed}", flush=True)
                    last_status = now
                if stop_at is not None and now >= stop_at and not heap:
                    break
                wake = [next_start] if (stop_at is None or now < stop_at) else []
                if heap:
                    wake.append(heap[0][0] / 1000)
                sleep(max(.01, min(.2, min(wake) - clock())) if wake else .2)
        except KeyboardInterrupt:
            print(f"\nStopping: flushing {len(heap)} pending payloads now.", file=sys.stderr)
            while heap:
                sender.submit(heapq.heappop(heap)[2])
        finally:
            self.save()
        return started


def expected_trace_ids(session, deliveries):
    """Only network requests with an emitted backend; offline/DNS calls are ineligible."""
    requests={r['trace_id'] for r in session.plan.evidence if r.get('first_backend_span_id')}
    exported={sp['traceId'] for d in deliveries if d['target']=='otlp' and d['kind']=='traces'
              for rs in d['payload']['resourceSpans'] for ss in rs['scopeSpans'] for sp in ss['spans']}
    return sorted(exported if hasattr(session, 'causal_evidence') else requests & exported) if session.traces else []


def _part_id(delivery):
    span = delivery["payload"]["data"]["spans"][0]
    return next((a["value"] for a in span["attributes"] if a["key"] in ("emb.session_part_id","session.id")), None)


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
