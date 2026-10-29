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
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import otlp
from .personas import PersonaPool
from .profiles import load
from .util import uuid4, weighted
from .android_session import AndroidSession, SessionEnded, shift_envelope
from .calls import FORWARD_DELAY_MS
from .web_session import WebSession, shift

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path):
    cfg = {"platform": "android", "embrace": {"apps": {}, "data_url": None},
           "chronosphere": {"tenant": None, "api_token": None},
           "otlp": {"endpoint": None, "headers": {}, "encoding": "protobuf"}}
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
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
    if env.get("OTLP_ENDPOINT"):
        cfg["otlp"]["endpoint"] = env["OTLP_ENDPOINT"]
    if env.get("OTLP_HEADERS"):
        cfg["otlp"]["headers"].update(dict(h.split("=", 1) for h in env["OTLP_HEADERS"].split(",") if "=" in h))
    return cfg


def app_id_for(cfg, profile_key):
    apps = cfg["embrace"]["apps"]
    return apps.get(profile_key) or apps.get("default")


class Sender:
    """Posts Embrace payloads and backend OTLP data; or writes them to disk in dry-run mode."""

    def __init__(self, cfg, dry_run_dir=None, workers=8, verbose=False):
        self.cfg, self.dry, self.verbose = cfg, dry_run_dir, verbose
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.lock = threading.Lock()
        self.stats = {"embrace_spans": 0, "embrace_logs": 0, "otlp_traces": 0, "otlp_logs": 0, "errors": 0}
        self.counter = itertools.count(1)
        self.last_errors = []
        ch = cfg["chronosphere"]
        o = cfg["otlp"]
        if o.get("endpoint"):
            self.otlp_base = o["endpoint"].rstrip("/")
            self.otlp_headers = dict(o.get("headers") or {})
        elif ch.get("tenant"):
            self.otlp_base = f"https://{ch['tenant']}.chronosphere.io/data/opentelemetry"
            self.otlp_headers = {"API-Token": ch.get("api_token") or ""}
        else:
            self.otlp_base, self.otlp_headers = None, {}
        self.encoding = o.get("encoding", "protobuf")
        if dry_run_dir:
            os.makedirs(dry_run_dir, exist_ok=True)

    def submit(self, d):
        self.pool.submit(self._send, d)

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
        except Exception as e:  # keep generating; report at the end
            with self.lock:
                self.stats["errors"] += 1
                self.last_errors = (self.last_errors + [f"{key}: {e}"])[-5:]
            if self.verbose:
                print(f"  ! {key} failed: {e}", file=sys.stderr)

    def _post(self, url, body, headers):
        req = urllib.request.Request(url, data=gzip.compress(body), method="POST",
                                     headers=dict(headers, **{"Content-Encoding": "gzip"}))
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 2:
                    raise RuntimeError(f"HTTP {e.code} from {url}: {e.read()[:300]!r}")
            except OSError:  # URLError, connection resets, timeouts
                if attempt == 2:
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
        rejected, message = otlp.partial_success(resp or b"", json_body=self.encoding == "json")
        if rejected:
            raise RuntimeError(f"backend rejected {rejected} {d['kind']} records: {message}")

    def close(self):
        self.pool.shutdown(wait=True)


class Generator:
    def __init__(self, profile_keys, cfg, incident=None, emit_client_spans=False, backend_logs=False, persist=True,
                 platform="android"):
        self.profiles = [load(k) for k in profile_keys]
        self.cfg, self.incident, self.emit_client_spans = cfg, incident, emit_client_spans
        self.backend_logs, self.persist, self.platform = backend_logs, persist, platform
        self.session_cls = AndroidSession if platform == "android" else WebSession
        state = os.path.join(ROOT, ".state")
        # Profiles that share one Android app ID are lines of business in one super app, with one user base.
        by_app = {}
        for p in self.profiles:
            by_app.setdefault(app_id_for(cfg, p.key), []).append(p)
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
            if not app or not re.fullmatch(r"[A-Za-z0-9]{5}", app):
                raise SystemExit(f"No valid Embrace app ID for profile '{p.key}'. Set embrace.apps.{p.key} in "
                                 f"config.json or EMBRACE_APP_ID_{p.key.upper()}.")
            if self.incident and self.incident not in p.incidents and len(self.profiles) == 1:
                raise SystemExit(f"Unknown incident '{incident}' for {p.key}. Options: {', '.join(p.incidents)}")

    def session(self, start_ms):
        profile = random.choice(self.profiles)
        pool = self.pools[profile.key]
        persona = pool.pick(self._shared_make_user if profile.key in self.shared else profile.make_user)
        journey = weighted(profile.journeys)
        incident = self.incident if self.incident in profile.incidents else None
        kwargs = {"shared": True} if profile.key in self.shared else {}
        s = self.session_cls(profile, persona, app_id_for(self.cfg, profile.key), start_ms, incident,
                             self.emit_client_spans, **kwargs)
        try:
            journey(s)
        except SessionEnded:  # the app crashed; the process is gone
            pass
        deliveries = s.finish()
        if not self.backend_logs:
            deliveries = [d for d in deliveries if not (d["target"] == "otlp" and d["kind"] == "logs")]
        return s, journey.__name__, deliveries

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
    tenant = gen.cfg["chronosphere"].get("tenant")
    for _ in range(count):
        s, journey, deliveries = gen.session(now)
        end_at = now - random.uniform(0.0, spread_min * 60_000) - 15_000
        delta_ms = end_at - s.end_ms
        for d in deliveries:
            (shift_envelope if d.get("fmt") == "envelope" else shift)(d["payload"], int(delta_ms * 1_000_000))
            d["due"] += delta_ms
        s.start_ms += delta_ms
        s.end_ms += delta_ms
        all_deliveries.extend(deliveries)
        if not quiet:
            print(describe(s, journey, tenant))
    gen.save()
    all_deliveries.sort(key=lambda d: d["due"])
    # Sessions go to Embrace first; backend traces follow once Embrace has had time to forward the client spans.
    first = [d for d in all_deliveries if d["target"] == "embrace"]
    for d in first:
        sender.submit(d)
    later = [d for d in all_deliveries if d["target"] != "embrace"]
    if later and not sender.dry and first:
        print(f"Sent {len(first)} Embrace payloads; holding {len(later)} backend exports "
              f"{FORWARD_DELAY_MS[0] // 1000}s so they reach Chronosphere with Embrace's forwarded spans.", flush=True)
        time.sleep(FORWARD_DELAY_MS[0] / 1000)
    for d in later:
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
