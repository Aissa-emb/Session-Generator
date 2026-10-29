"""Config, senders and scheduling for generated sessions and traces."""

import gzip
import heapq
import itertools
import json
import os
import random
import re
import sys
import threading
import time
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import otlp
from .personas import PersonaPool
from .profiles import load
from .util import uuid4, weighted
from .security import (SecurityError, TransportError, post, read_token_file,
                       require_private_file, tenant_url, validate_endpoint, validate_headers)
from .android_session import AndroidSession, SessionEnded, shift_envelope
from .web_session import WebSession, shift
from .scenarios import Plan, Runtime
from .journeys import JOURNEYS
from .operations import install

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def append_manifest(path, record):
    """Append bounded verification metadata to an owner-only regular file."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SecurityError("Manifest must be an owner-only regular file.")
        with os.fdopen(fd, "a") as f:
            fd = None
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    finally:
        if fd is not None:
            os.close(fd)


def load_config(path):
    cfg = {"platform": "android", "embrace": {"apps": {}, "data_url": None},
           "chronosphere": {"tenant": None, "api_token": None},  # nosec B105
           "otlp": {"endpoint": None, "headers": {}, "encoding": "protobuf"}}
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
            if user.get("chronosphere", {}).get("api_token") or user.get("otlp", {}).get("headers"):
                require_private_file(f)
        for k, v in user.items():
            if isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
    env = os.environ
    if env.get("XCORGEN_PLATFORM"):
        cfg["platform"] = env["XCORGEN_PLATFORM"]
    for key in ("ecommerce", "banking", "travel"):
        if env.get(f"EMBRACE_APP_ID_{key.upper()}"):
            cfg["embrace"]["apps"][key] = env[f"EMBRACE_APP_ID_{key.upper()}"]
    if env.get("EMBRACE_APP_ID"):
        cfg["embrace"]["apps"].setdefault("default", env["EMBRACE_APP_ID"])
    if env.get("CHRONOSPHERE_TENANT"):
        cfg["chronosphere"]["tenant"] = env["CHRONOSPHERE_TENANT"]
    if env.get("CHRONOSPHERE_API_TOKEN"):
        cfg["chronosphere"]["api_token"] = env["CHRONOSPHERE_API_TOKEN"]
    else:
        token_path = env.get("CHRONOSPHERE_API_TOKEN_FILE") or cfg["chronosphere"].get("api_token_file")
        if token_path:
            if not os.path.isabs(token_path):
                token_path = os.path.join(os.path.dirname(os.path.abspath(path or os.path.join(ROOT, "config.json"))), token_path)
            cfg["chronosphere"]["api_token"] = read_token_file(token_path)
    if env.get("OTLP_ENDPOINT"):
        cfg["otlp"]["endpoint"] = env["OTLP_ENDPOINT"]
    if env.get("OTLP_HEADERS"):
        try:
            cfg["otlp"]["headers"].update(dict(h.split("=", 1) for h in env["OTLP_HEADERS"].split(",")))
        except ValueError:
            raise SecurityError("OTLP_HEADERS must contain comma-separated name=value pairs.") from None
    if cfg["chronosphere"].get("tenant"):
        tenant_url(cfg["chronosphere"]["tenant"])
    validate_headers(cfg["otlp"]["headers"])
    if cfg["chronosphere"].get("api_token"):
        validate_headers({"API-Token": cfg["chronosphere"]["api_token"]})
    if cfg["otlp"].get("endpoint"):
        validate_endpoint(cfg["otlp"]["endpoint"], cfg["otlp"]["headers"])
    if cfg["embrace"].get("data_url"):
        validate_endpoint(cfg["embrace"]["data_url"], {"X-EM-AID": "app-id"})
    return cfg


def app_id_for(cfg, profile_key):
    apps = cfg["embrace"]["apps"]
    return apps.get(profile_key) or apps.get("default")


class Sender:
    """Posts Embrace payloads and backend OTLP data; or writes them to disk in dry-run mode."""

    def __init__(self, cfg, dry_run_dir=None, workers=8, verbose=False):
        self.cfg, self.dry, self.verbose = cfg, dry_run_dir, verbose
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.pending_slots = threading.BoundedSemaphore(workers * 4)
        self.lock = threading.Lock()
        self.stats = {"embrace_spans": 0, "embrace_logs": 0, "otlp_traces": 0, "otlp_logs": 0, "errors": 0}
        self.counter = itertools.count(1)
        self.last_errors = []
        ch = cfg["chronosphere"]
        o = cfg["otlp"]
        if o.get("endpoint"):
            self.otlp_base = validate_endpoint(o["endpoint"], o.get("headers") or {})
            self.otlp_headers = dict(o.get("headers") or {})
        elif ch.get("tenant"):
            self.otlp_base = tenant_url(ch['tenant']) + "/data/opentelemetry"
            self.otlp_headers = {"API-Token": ch.get("api_token") or ""}
        else:
            self.otlp_base, self.otlp_headers = None, {}
        self.encoding = o.get("encoding", "protobuf")
        if dry_run_dir:
            os.makedirs(dry_run_dir, exist_ok=True)

    def submit(self, d):
        self.pending_slots.acquire()
        try:
            future = self.pool.submit(self._send, d)
        except BaseException:
            self.pending_slots.release()
            raise
        future.add_done_callback(lambda _: self.pending_slots.release())

    def _send(self, d):
        key = f"{d['target']}_{d['kind']}"
        try:
            if self.dry:
                n = next(self.counter)
                with open(os.path.join(self.dry, f"{n:05d}-{key}.json"), "w") as f:
                    json.dump({k: v for k, v in d.items() if k != "due"}, f, indent=1)
            elif d["target"] == "embrace":
                self._post_embrace(d)
            else:
                self._post_otlp(d)
            with self.lock:
                self.stats[key] += 1
                if not self.dry and self.cfg.get("generation", {}).get("manifest_path"):
                    ids = [span["traceId"] for rs in d["payload"].get("resourceSpans", [])
                           for scope in rs["scopeSpans"] for span in scope["spans"]]
                    append_manifest(self.cfg["generation"]["manifest_path"] + ".receipts", {
                        "stage": "accepted", "target": d["target"], "kind": d["kind"], "time_ms": time.time() * 1000,
                        "device_id": d["device_id"], "trace_ids": sorted(set(ids))})
        except Exception as e:  # keep generating; report at the end
            with self.lock:
                self.stats["errors"] += 1
                detail = str(e) if isinstance(e, (SecurityError, TransportError)) else "Payload delivery failed (details suppressed)."
                self.last_errors = (self.last_errors + [f"{key}: {detail}"])[-5:]
            if self.verbose:
                print(f"  ! {key} failed: {detail}", file=sys.stderr)

    def _post(self, url, body, headers):
        compressed = gzip.compress(body)
        for attempt in range(3):
            try:
                return post(url, compressed, dict(headers, **{"Content-Encoding": "gzip"}))
            except TransportError as e:
                if (e.status is not None and e.status != 429 and e.status < 500) or attempt == 2:
                    raise
            time.sleep(0.5 * (attempt + 1))

    def _post_embrace(self, d):
        base = self.cfg["embrace"].get("data_url") or f"https://a-{d['app_id']}.data.emb-api.com"
        body = json.dumps(d["payload"], separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-EM-AID": d["app_id"], "X-EM-DID": d["device_id"]}
        headers.update(d.get("headers") or {"User-Agent": d["ua"], "Origin": d["origin"]})
        self._post(f"{base}/v2/{d['kind']}", body, headers)

    def _post_otlp(self, d):
        if not self.otlp_base:
            return
        if self.encoding == "json":
            body, ctype = json.dumps(d["payload"], separators=(",", ":")).encode(), "application/json"
        else:
            enc = otlp.encode_traces if d["kind"] == "traces" else otlp.encode_logs
            body, ctype = enc(d["payload"]), "application/x-protobuf"
        resp = self._post(f"{self.otlp_base}/v1/{d['kind']}", body, dict(self.otlp_headers, **{"Content-Type": ctype}))
        rejected, _ = otlp.partial_success(resp or b"", json_body=self.encoding == "json")
        if rejected:
            raise TransportError(f"Backend rejected {rejected} {d['kind']} records.")

    def close(self):
        self.pool.shutdown(wait=True)


class Generator:
    def __init__(self, profile_keys, cfg, incident=None, emit_client_spans=False, backend_logs=False, persist=True,
                 platform="android"):
        self.profiles = [load(k) for k in profile_keys]
        self.cfg, self.incident, self.emit_client_spans = cfg, incident, emit_client_spans
        self.backend_logs, self.persist, self.platform = backend_logs, persist, platform
        self.session_cls = AndroidSession if platform == "android" else WebSession
        self.runtime = Runtime()
        self.settings = cfg.get("generation", {})
        self.verification_queue = list(self.settings.get("verification_plan", []))
        for profile in self.profiles:
            install(profile)
        state = os.path.join(ROOT, ".state")
        # Profiles that share one Android app ID are lines of business in one super app, with one user base.
        by_app = {}
        for p in self.profiles:
            app = app_id_for(cfg, p.key)
            if not isinstance(app, str) or not re.fullmatch(r"[A-Za-z0-9]{5}", app):
                raise SecurityError("Each selected profile needs a five-character alphanumeric Embrace app ID.")
            by_app.setdefault(app, []).append(p)
        self.shared = {p.key for group in by_app.values() if len(group) > 1 and platform == "android" for p in group}
        self.pools = {}
        for app, group in by_app.items():
            if self.shared and group[0].key in self.shared:
                pool = PersonaPool(f"app-{app}", state, size=4000, platform=platform)
                for p in group:
                    self.pools[p.key] = pool
            else:
                for p in group:
                    self.pools[p.key] = PersonaPool(p.key, state, platform=platform)
        self._make_user = {}
        for p in self.profiles:
            app = app_id_for(cfg, p.key)
            if self.incident and self.incident not in p.incidents and len(self.profiles) == 1:
                raise SystemExit(f"Unknown incident '{incident}' for {p.key}. Options: {', '.join(p.incidents)}")

    def session(self, start_ms):
        selection = self.verification_queue.pop(0) if self.verification_queue else {}
        profile = next((p for p in self.profiles if p.key == selection.get("profile")), None) or random.choice(self.profiles)
        pool = self.pools[profile.key]
        persona = pool.pick(self._shared_make_user if profile.key in self.shared else profile.make_user)
        journey = JOURNEYS[profile.key]
        incident = selection.get("incident") or (self.incident if self.incident in profile.incidents else None)
        kwargs = {"shared": True} if profile.key in self.shared else {}
        s = self.session_cls(profile, persona, app_id_for(self.cfg, profile.key), start_ms, incident,
                             self.emit_client_spans, **kwargs)
        settings = dict(self.settings, **selection.get("settings", {}))
        s.plan = Plan(s, self.runtime, settings, incident)
        s.verification_case = selection.get("name")
        if s.plan.primary == "mobile" and s.plan.ring == "stable" and random.random() < self.settings.get("transport_share", .4) and not incident:
            s.plan.transport = random.choice(("dns", "offline", "reset", "timeout", "cancelled"))
            s.plan.incident = None
        try:
            journey(s)
        except SessionEnded:  # the app crashed; the process is gone
            if self.platform == "android" and s.persona.pop("pending_crash_upload", False):
                due = s.crash_upload_due
                for delivery in s.deliveries:
                    if delivery["target"] == "embrace":
                        delivery["due"] = max(delivery["due"], due)
                s.ended = False
                s.t = due
                from .util import hex_id
                s.process_id = hex_id(8)
                s._start_part("state")
                page = "dashboard" if profile.key == "banking" else "home"
                s._screen(page, profile.pages[page].label({}))
                s.breadcrumb("session_resumed")
                s.wait(2, 5)
                s.leave()
        deliveries = s.finish()
        if not self.backend_logs:
            deliveries = [d for d in deliveries if not (d["target"] == "otlp" and d["kind"] == "logs")]
        return s, journey.__name__, deliveries

    def record(self, session):
        path = self.settings.get("manifest_path")
        if not path:
            return
        s = session
        record = {"provenance": "synthetic", "stage": "generated", "profile": s.profile.key,
                  "verification_case": s.verification_case, "app_id": s.app_id,
                  "session_id": s.user_session_id, "device_id": s.persona["device_id"],
                  "start_ms": s.start_ms, "end_ms": s.end_ms, "properties": s.properties,
                  "primary_scenario": s.plan.primary, "requests": s.plan.evidence,
                  "milestones": getattr(s, "milestones", []), "blockage": getattr(s, "blockage_evidence", None),
                  "crash_family": s.plan.crash_family if s.plan.primary == "crash" else None}
        append_manifest(path, record)

    def _shared_make_user(self, p):
        """One account system across the app: traits for every line of business, mostly signed in."""
        traits = {}
        for prof in self.profiles:
            if prof.key in self.shared:
                prof.make_user(p)
                traits.update(p["traits"])
        traits["membership"] = weighted([("standard", 62), ("plus", 29), ("premium", 9)])
        p["traits"] = traits
        p["user_id"] = uuid4() if random.random() < 0.82 else None

    def save(self):
        if not self.persist:
            return
        for pool in {id(p): p for p in self.pools.values()}.values():
            pool.save()


def describe(s, journey, tenant):
    errors = [c for c in s.calls if c[1] >= 400]
    p = s.persona
    device = p["model"] if p.get("platform") == "android" else p["kind"]
    line = (f"{s.profile.key:<9} {journey:<20} {device:<22} parts={s.part.index:<2} api={len(s.calls):<3} "
            f"errors={len(errors):<2} {s.start_ms / 1000:.0f}->{s.end_ms / 1000:.0f} user_session={s.user_session_id}")
    if tenant and s.calls:
        key, status, tid = (errors or s.calls)[-1]
        line += f"\n          {key} ({status}): https://{tenant}.chronosphere.io/traces/trace-detail/by-id/{tid}"
    return line


def run_burst(gen, sender, count, spread_min, quiet=False):
    """Generate `count` sessions that ended within the last `spread_min` minutes, then send everything."""
    now = time.time() * 1000
    all_deliveries = []
    sessions = []
    tenant = gen.cfg["chronosphere"].get("tenant")
    for _ in range(count):
        s, journey, deliveries = gen.session(now - random.uniform(0, spread_min * 60_000))
        sessions.append((s, journey))
        all_deliveries.extend(deliveries)
    # One shift for the complete batch preserves shared pool-occupant links.
    delta_ms = min(0, now - 15000 - max(d["due"] for d in all_deliveries))
    for d in all_deliveries:
        (shift_envelope if d.get("fmt") == "envelope" else shift)(d["payload"], int(delta_ms * 1_000_000))
        d["due"] += delta_ms
    for s, journey in sessions:
        s.start_ms += delta_ms
        s.end_ms += delta_ms
        if not quiet:
            print(describe(s, journey, tenant))
    gen.save()
    all_deliveries.sort(key=lambda d: d["due"])
    # Backend traces go first so Chronosphere has them before Embrace forwards the client spans.
    backend = [d for d in all_deliveries if d["target"] != "embrace"]
    for d in backend:
        sender.submit(d)
    sender.pool.shutdown(wait=True)
    sender.pool = ThreadPoolExecutor(max_workers=8)
    for d in all_deliveries:
        if d["target"] == "embrace":
            sender.submit(d)
    return len(all_deliveries)


# Share of the average session rate by local hour (US Eastern): quiet overnight, lunch and evening peaks.
DIURNAL = [0.30, 0.22, 0.17, 0.14, 0.17, 0.26, 0.54, 0.90, 1.14, 1.20, 1.26, 1.32, 1.44, 1.38, 1.26, 1.20,
           1.26, 1.38, 1.56, 1.74, 1.80, 1.62, 1.14, 0.66]


def diurnal_factor(now):
    hour = datetime.fromtimestamp(now, ZoneInfo("America/New_York")).hour
    return DIURNAL[hour] / (sum(DIURNAL) / 24)


def run_live(gen, sender, rate_per_min, duration_min, quiet=False, diurnal=False, status_every_s=600):
    """Start sessions in real time and send each payload when the app or service would have sent it."""
    heap, seq = [], itertools.count()
    tenant = gen.cfg["chronosphere"].get("tenant")
    stop_at = time.time() + duration_min * 60 if duration_min else None
    next_start = time.time()
    started = 0
    last_save = last_status = time.time()
    started_at_status = 0
    try:
        while True:
            now = time.time()
            if (stop_at is None or now < stop_at) and now >= next_start:
                s, journey, deliveries = gen.session(now * 1000)
                gen.record(s)
                started += 1
                for d in deliveries:
                    heapq.heappush(heap, (d["due"], next(seq), d))
                if not quiet:
                    print(describe(s, journey, tenant), flush=True)
                rate = rate_per_min * (diurnal_factor(now) if diurnal else 1.0)
                next_start = now + random.expovariate(rate / 60.0)
            while heap and heap[0][0] <= time.time() * 1000:
                sender.submit(heapq.heappop(heap)[2])
            if now - last_save > 60:
                gen.save()
                last_save = now
            if now - last_status > status_every_s:
                st = sender.stats
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] sessions {started} "
                      f"(+{started - started_at_status} in {status_every_s // 60} min), Embrace payloads "
                      f"{st['embrace_spans'] + st['embrace_logs']}, backend exports {st['otlp_traces']}, "
                      f"errors {st['errors']}, queued {len(heap)}"
                      + (f", last error: {sender.last_errors[-1]}" if sender.last_errors else ""), flush=True)
                last_status, started_at_status = now, started
            if stop_at is not None and now >= stop_at and not heap:
                break
            wake = [next_start] if (stop_at is None or now < stop_at) else []
            if heap:
                wake.append(heap[0][0] / 1000)
            time.sleep(max(0.05, min(1.0, min(wake) - time.time())) if wake else 0.2)
    except KeyboardInterrupt:
        print(f"\nStopping: flushing {len(heap)} pending payloads now.", file=sys.stderr)
        while heap:
            sender.submit(heapq.heappop(heap)[2])
    gen.save()
    return started
