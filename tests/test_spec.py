import copy
import json
import os
import random
import tempfile
import time
import unittest

from xcorgen import catalog, limits, linter, presets, quota, spec as specs
from xcorgen.emitted import Emitted
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = time.time() * 1000


class Collect:
    """Sender stand-in that keeps deliveries in memory."""

    class _Pool:
        def shutdown(self, wait=True):
            pass

    def __init__(self):
        self.deliveries, self.last_errors = [], []
        self.stats = {"embrace_spans": 0, "embrace_logs": 0, "otlp_traces": 0, "otlp_logs": 0, "errors": 0}
        self.pool = self._Pool()

    def submit(self, d):
        self.deliveries.append(d)


def burst(**overrides):
    s = specs.default_spec()
    s["traffic"] = {"mode": "burst", "count": 300, "window": {"last_minutes": 8}}
    s.update(overrides)
    return s


def run(spec, seed=3):
    random.seed(seed)
    cfg = load_config(None)
    with tempfile.TemporaryDirectory() as state:
        r = SpecRun(spec, cfg, persist=False, now_ms=NOW, state_dir=state, rng=random.Random(seed))
        sender = Collect()
        r.run_batch(sender, quiet=True)
        r.sender = sender
    return r


def attrs(item):
    return {a["key"]: a["value"] for a in item.get("attributes", [])}


def network_spans(deliveries):
    return [s for d in deliveries if d.get("fmt") == "envelope" for s in d["payload"]["data"].get("spans", [])
            if attrs(s).get("emb.type") == "perf.network_request"]


class ValidationTests(unittest.TestCase):
    def test_examples_are_valid(self):
        for name in ("baseline-burst.json", "live-end-to-end.json", "verification-plan.json"):
            issues = specs.validate(specs.load(os.path.join(ROOT, "spec", "examples", name)))
            self.assertEqual(specs.errors(issues), [], name)

    def test_actionable_errors(self):
        cases = {
            "rate": lambda s: s.update(client_faults=[{"id": "c", "type": "jvm_crash", "rate": 1.5}]),
            "unknown field": lambda s: s.update(colour="green"),
            "chronosphere": lambda s: s.update(destinations={"mode": "end_to_end"}),
            "backfill horizon": lambda s: s.update(traffic={"mode": "backfill", "count": 10,
                                                           "window": {"last_minutes": 25 * 60}}),
            "future window": lambda s: s.update(traffic={"mode": "burst", "count": 10, "window": {
                "start": specs.iso(NOW), "end": specs.iso(NOW + 3_600_000)}}),
            "exclusive sum": lambda s: s.update(client_faults=[
                {"id": "a", "type": "jvm_crash", "rate": .7}, {"id": "b", "type": "jvm_crash", "rate": .6}]),
            "filter matches nothing": lambda s: s.update(client_faults=[
                {"id": "a", "type": "jvm_crash", "rate": .1, "filters": {"app_versions": ["9.9.9"]}}]),
            "device and os": lambda s: s.update(client_faults=[
                {"id": "a", "type": "jvm_crash", "rate": .1, "filters": {"device_models": ["SM-F966U"], "os_versions": ["14"]}}]),
            "unknown variant": lambda s: s.update(backend_faults=[{"id": "a", "type": "dependency_5xx", "rate": .1}]),
            "planned type": lambda s: s.update(backend_faults=[{"id": "a", "type": "cache_outage", "rate": .1}]),
            "schedule outside window": lambda s: s.update(
                backend_faults=[{"id": "p", "type": "db_pool_exhaustion", "variant": "payments-db-pool", "rate": .3}],
                incidents=[{"id": "i", "fault_ref": "p", "start": specs.iso(NOW - 6 * 3_600_000), "peak_minutes": 10}]),
            "realism": lambda s: s["targets"][0].update(app_name="Vantra Demo"),
            "rings without rollout": lambda s: s.update(client_faults=[
                {"id": "a", "type": "jvm_crash", "rate": .1, "filters": {"release_rings": ["canary"]}}]),
            "web fault without web target": lambda s: s.update(client_faults=[{"id": "a", "type": "js_exception", "rate": .1}]),
        }
        for name, mutate in cases.items():
            s = burst()
            mutate(s)
            self.assertTrue(specs.errors(specs.validate(s, now_ms=NOW)), name)

    def test_end_to_end_backfill_warns_with_the_caveat(self):
        s = burst(destinations={"mode": "end_to_end"}, chronosphere={"tenant": "acme-prod"},
                  traffic={"mode": "backfill", "count": 10, "window": {"last_minutes": 600}})
        issues = specs.validate(s, now_ms=NOW)
        self.assertEqual(specs.errors(issues), [])
        self.assertIn(limits.TRACE_CAVEAT, [i["message"] for i in issues])
        self.assertIn(limits.TRACE_CAVEAT, specs.explain(s))

    def test_json_patch(self):
        doc = {"a": [1, 2], "b": {"c": 1}}
        out = specs.apply_patch(doc, [{"op": "add", "path": "/a/-", "value": 3}, {"op": "replace", "path": "/b/c", "value": 2},
                                      {"op": "move", "from": "/b/c", "path": "/d"}, {"op": "copy", "from": "/d", "path": "/e"},
                                      {"op": "remove", "path": "/a/0"}, {"op": "test", "path": "/e", "value": 2}])
        self.assertEqual(out, {"a": [2, 3], "b": {}, "d": 2, "e": 2})
        self.assertEqual(doc, {"a": [1, 2], "b": {"c": 1}})
        for bad in ([{"op": "replace", "path": "/x", "value": 1}], [{"op": "test", "path": "/a/0", "value": 9}]):
            with self.assertRaises(ValueError):
                specs.apply_patch(doc, bad)

    def test_presets_validate(self):
        for p in presets.listing(NOW):
            s = presets.apply(specs.default_spec(), p["id"], NOW)
            self.assertEqual(specs.errors(specs.validate(s, now_ms=NOW)), [], p["id"])


class QuotaTests(unittest.TestCase):
    def test_exact_counts_and_time_spread(self):
        rng = random.Random(1)
        sessions = [{"start_ms": i * 2000, "lob": "retail" if i % 3 else "travel"} for i in range(900)]
        faults = [{"id": "a"}, {"id": "b"}]
        assigned, report = quota.allocate_exact(
            sessions, faults, lambda f, s: 1 / 3 if f["id"] == "a" else .25,
            lambda f, s, have: f["id"] == "a" or s["lob"] == "retail",
            lambda f, s, have: "a" not in have, rng)
        a = [i for i, ids in assigned.items() if "a" in ids]
        b = [i for i, ids in assigned.items() if "b" in ids]
        self.assertLessEqual(abs(len(a) - 300), 1)
        self.assertLessEqual(abs(len(b) - 150), 1)
        self.assertFalse(set(a) & set(b))
        halves = sum(i < 450 for i in a)
        self.assertLess(abs(halves - 150), 20)

    def test_streaming_quota_is_exact_at_block_boundaries(self):
        q = quota.StreamingQuota(random.Random(2), block=20)
        hits = sum(q.take(.1) for _ in range(400))
        self.assertLessEqual(abs(hits - 40), 1)

    def test_incident_ramp(self):
        w = specs.incident_window({"start": "2026-10-30T14:00:00Z", "onset_minutes": 10, "peak_minutes": 20,
                                   "recovery_minutes": 10})
        at = lambda minute: specs.ramp(specs.parse_time("2026-10-30T14:00:00Z") + minute * 60000, w)
        self.assertEqual(at(-1), 0)
        self.assertAlmostEqual(at(5), .5)
        self.assertEqual(at(15), 1)
        self.assertAlmostEqual(at(35), .5)
        self.assertEqual(at(45), 0)


class RunTests(unittest.TestCase):
    def test_sessions_only_sends_no_trace_context(self):
        r = run(burst(client_faults=[{"id": "c", "type": "jvm_crash", "rate": .2}]))
        d = r.sender.deliveries
        self.assertFalse([x for x in d if x["target"] == "otlp"])
        self.assertFalse([s for s in network_spans(d) if "emb.w3c_traceparent" in attrs(s)])

    def test_end_to_end_trace_window(self):
        r = run(burst(destinations={"mode": "end_to_end"}, chronosphere={"tenant": "acme-prod"},
                      traffic={"mode": "burst", "count": 200, "window": {"last_minutes": 30}}))
        by_session = {}
        for d in r.sender.deliveries:
            if d.get("fmt") == "envelope" and d["kind"] == "spans":
                for s in d["payload"]["data"]["spans"]:
                    a = attrs(s)
                    if s["name"] == "emb-session":
                        sid = a["emb.user_session_id"]
                        by_session.setdefault(sid, {"start": int(a["emb.user_session_start_ts"]), "tp": set()})
                for s in d["payload"]["data"]["spans"]:
                    if attrs(s).get("emb.type") == "perf.network_request":
                        by_session[attrs(s)["emb.user_session_id"]]["tp"].add("emb.w3c_traceparent" in attrs(s))
        old = [v for v in by_session.values() if NOW - v["start"] > limits.TRACE_ELIGIBLE_MS]
        new = [v for v in by_session.values() if NOW - v["start"] <= limits.TRACE_ELIGIBLE_MS]
        self.assertTrue(old and new)
        self.assertTrue(all(v["tp"] <= {False} for v in old))
        self.assertTrue(all(v["tp"] <= {True} for v in new))
        roots = {}
        for d in r.sender.deliveries:
            if d["target"] == "otlp":
                for rs in d["payload"]["resourceSpans"]:
                    for ss in rs["scopeSpans"]:
                        for span in ss["spans"]:
                            if not span.get("parentSpanId") or span["traceId"] not in roots:
                                roots.setdefault(span["traceId"], set()).add(span["name"] if not span.get("parentSpanId") else None)
        linked = {s["trace_id"] for s in network_spans(r.sender.deliveries) if "emb.w3c_traceparent" in attrs(s)}
        # Every backend trace continues a forwarded request, except the pool's standalone occupant batches.
        for trace_id, names in roots.items():
            if trace_id not in linked:
                self.assertIn("authorization_worker.batch", names)

    def test_copilot_example_crash_after_failed_payment(self):
        """One in three checkout sessions on 10.28.0 crash after the payment fails, Android 16 only."""
        s = burst(traffic={"mode": "burst", "count": 2500, "window": {"last_minutes": 8}})
        s["client_faults"] = [
            {"id": "checkout-502", "type": "http_error", "rate": 1 / 3, "steer_journey": False,
             "filters": {"lines_of_business": ["retail"], "app_versions": ["10.28.0"], "os_versions": ["16"],
                         "endpoints": ["place_order"]}, "details": {"endpoint": "place_order", "status": 502}},
            {"id": "checkout-crash", "type": "jvm_crash", "rate": 1, "details": {
                "with_fault": "checkout-502", "endpoint": "place_order", "on_failure_only": True, "family": 0}}]
        self.assertEqual(specs.errors(specs.validate(s, now_ms=NOW)), [])
        r = run(s, seed=5)
        rows = {row["id"]: row for row in r.emitted.summary()["faults"]}
        self.assertGreater(rows["checkout-502"]["eligible"], 30)
        self.assertTrue(rows["checkout-502"]["within_tolerance"], rows["checkout-502"])
        self.assertEqual(rows["checkout-crash"]["emitted_sessions"], rows["checkout-502"]["emitted_sessions"])
        for o in r.emitted.observations:
            if o["crash"]:
                self.assertEqual((o["lob"], o["app_version"], o["os_version"]), ("retail", "10.28.0", "16"))

    def test_baseline_reproduces_v1_mix(self):
        # End to end, so latency faults are proven by backend spans rather than by client timing alone.
        r = run(presets.apply(burst(traffic={"mode": "burst", "count": 900, "window": {"last_minutes": 8}},
                                    destinations={"mode": "end_to_end"}, chronosphere={"tenant": "acme-prod"}),
                              "baseline", NOW), seed=9)
        rows = r.emitted.summary()["faults"]
        bad = [x for x in rows if x["eligible"] and not x["within_tolerance"]]
        self.assertEqual(bad, [])
        self.assertFalse([x for x in rows if x.get("evidence") == "client latency"])
        crash = next(x for x in rows if x["id"] == "baseline-crash")
        self.assertAlmostEqual(crash["emitted_pct"], 10, delta=.5)

    def test_exact_backend_quotas_reserve_narrow_release_cohorts(self):
        # This clock/seed previously let broad payment stories occupy enough
        # canary sessions to leave only 32 of the requested 33.6 fallbacks.
        # Exercise the CLI's shared RNG and inspect emitted payloads, not plans.
        s = specs.load(os.path.join(ROOT, 'spec/examples/baseline-burst.json'))
        for clock in ('2026-10-31T02:20:05.400Z', '2026-10-31T02:24:05.400Z'):
            with self.subTest(clock=clock), tempfile.TemporaryDirectory() as state:
                random.seed(7331)
                r = SpecRun(s, load_config(None), persist=False, state_dir=state,
                            now_ms=specs.parse_time(clock))
                sender = Collect()
                r.run_batch(sender, quiet=True)
                sender.pool.shutdown(wait=True)
                bad = [row for row in r.emitted.summary()['faults']
                       if row['eligible'] and not row['within_tolerance']]
                self.assertEqual(bad, [])

    def test_new_android_faults(self):
        s = burst(traffic={"mode": "burst", "count": 400, "window": {"last_minutes": 8}},
                  destinations={"mode": "end_to_end"}, chronosphere={"tenant": "acme-prod"})
        s["client_faults"] = [
            {"id": "net", "type": "network_failure", "rate": .3, "filters": {"lines_of_business": ["retail", "banking"]},
             "details": {"kinds": ["timeout", "dns", "tls"]}},
            {"id": "cold", "type": "slow_startup", "rate": 1, "filters": {"device_tiers": ["budget"]},
             "details": {"phase": "sdk_init"}},
            {"id": "slow-search", "type": "slow_root_span", "rate": 1, "filters": {"lines_of_business": ["travel"]},
             "details": {"span": "search_flights", "p50_ms": 3000, "p95_ms": 6000}},
            {"id": "cart-503", "type": "http_error", "rate": .2, "filters": {"lines_of_business": ["retail"]},
             "details": {"endpoint": "add_to_cart", "status": 503}}]
        r = run(s, seed=4)
        rows = {row["id"]: row for row in r.emitted.summary()["faults"]}
        for fid in ("net", "cold", "cart-503"):
            self.assertTrue(rows[fid]["within_tolerance"], rows[fid])
        self.assertAlmostEqual(rows["slow-search"]["emitted_p50_ms"], 3000, delta=600)
        self.assertAlmostEqual(rows["slow-search"]["emitted_p95_ms"], 6000, delta=1800)
        spans = network_spans(r.sender.deliveries)
        failed = [attrs(x) for x in spans if "error.type" in attrs(x) and "http.response.status_code" not in attrs(x)]
        self.assertTrue(failed)
        self.assertTrue({a["error.type"] for a in failed} <= {e for e, _, _ in catalog.NETWORK_FAILURES.values()})
        self.assertTrue(all(a.get("emb.forward_telemetry") == "true" for a in failed if "emb.w3c_traceparent" in a))
        # A 503 from the owning service carries a stack in that service's language.
        cart = [span for d in r.sender.deliveries if d["target"] == "otlp" for rs in d["payload"]["resourceSpans"]
                if "shopping-cart-service" in json.dumps(rs["resource"]) for ss in rs["scopeSpans"] for span in ss["spans"]
                if span.get("events") and "Polly.CircuitBreaker.BrokenCircuitException" in json.dumps(span["events"])]
        self.assertTrue(cart, "cart service failure trace missing")
        self.assertIn(" in /src/", json.dumps(cart[0]["events"]))  # .NET stack frame shape
        self.assertEqual(linter.lint_payloads(r.sender.deliveries), [])

    def test_verification_plan_from_spec(self):
        r = run(specs.load(os.path.join(ROOT, "spec", "examples", "verification-plan.json")), seed=7331)
        cases = [o for o in r.emitted.observations if o.get("verification_case")]
        self.assertEqual(len(cases), 45)
        names = [c["name"] for c in specs.load(os.path.join(ROOT, "spec", "examples", "verification-plan.json"))["verification_cases"]]
        by_name = dict(zip(names, cases))
        statuses = lambda o: {(n["endpoint"], n["status"]) for n in o["net"]}
        self.assertIn(("place_order", 502), statuses(by_name["ecommerce/payments-db-pool"]))
        self.assertIn(("create_booking", 502), statuses(by_name["travel/payments-db-pool"]))
        self.assertIn(("place_order", 504), statuses(by_name["ecommerce/payment-timeouts"]))
        self.assertIn(("create_transfer", 503), statuses(by_name["banking/rails-outage"]))
        self.assertIn(("create_booking", 504), statuses(by_name["travel/gds-pnr-timeouts"]))
        self.assertIn("partial_flight_results_displayed", by_name["travel/supplier-timeouts"]["breadcrumbs"])
        self.assertIn("cached_hotel_rates_displayed", by_name["travel/hotel-supplier-outage"]["breadcrumbs"])
        for name, o in by_name.items():
            if "/crash/" in name:
                self.assertTrue(o["crash"], name)
            if "/blockage/" in name:
                self.assertTrue(o["blockage"], name)
            if name.endswith("/healthy"):
                self.assertFalse(o["crash"] or any(n["status"] is None or n["status"] >= 400 for n in o["net"]), name)
        for name in ("ecommerce/payments-db-pool/onset", "ecommerce/payments-db-pool/recovery"):
            self.assertIn(("place_order", 201), statuses(by_name[name]), name)

    def test_backfill_follows_curve_and_simulated_clock(self):
        s = burst(traffic={"mode": "backfill", "count": 600, "window": {"last_minutes": 24 * 60},
                           "daily_curve": {"kind": "diurnal"}, "throttle_payloads_per_second": 400})
        r = run(s, seed=8)
        starts = sorted(o["start_ms"] for o in r.emitted.observations)
        self.assertGreater(starts[-1] - starts[0], 20 * 3_600_000)
        self.assertFalse([x for x in r.sender.deliveries if x["target"] == "otlp"])
        # The returning-visitor rule (no new session within 30 minutes) runs on the planned clock.
        starts = {}
        for d in r.sender.deliveries:
            if d.get("fmt") == "envelope" and d["kind"] == "spans":
                for span in d["payload"]["data"]["spans"]:
                    if span["name"] == "emb-session":
                        a = attrs(span)
                        starts.setdefault(d["device_id"], set()).add(int(a["emb.user_session_start_ts"]))
        for device, times in starts.items():
            times = sorted(times)
            self.assertTrue(all(b - a > 25 * 60000 for a, b in zip(times, times[1:])), device)


class LiveTests(unittest.TestCase):
    def test_live_stream_on_a_simulated_clock(self):
        clock = [NOW / 1000]
        s = specs.default_spec()
        s.update(destinations={"mode": "end_to_end"}, chronosphere={"tenant": "acme-prod"},
                 traffic={"mode": "live", "rate_per_minute": 60, "duration_minutes": 20, "daily_curve": {"kind": "diurnal"}},
                 client_faults=[{"id": "crash", "type": "jvm_crash", "rate": .2},
                                {"id": "freeze", "type": "main_thread_blockage", "rate": .3, "quota": "exact"}])
        random.seed(6)
        with tempfile.TemporaryDirectory() as state:
            r = SpecRun(s, load_config(None), persist=False, now_ms=NOW, state_dir=state, rng=random.Random(6))
            sender = Collect()
            started = r.run_live(sender, quiet=True, clock=lambda: clock[0],
                                 sleep=lambda x: clock.__setitem__(0, clock[0] + x))
        self.assertGreater(started, 500)
        rows = {x["id"]: x for x in r.emitted.summary()["faults"]}
        self.assertTrue(rows["crash"]["within_tolerance"], rows["crash"])
        self.assertLessEqual(abs(rows["freeze"]["emitted_sessions"] - rows["freeze"]["requested_sessions"]), 2)
        self.assertTrue(all(o["traces"] for o in r.emitted.observations if o["net"]))
        due = max(d["due"] for d in sender.deliveries)
        self.assertLessEqual(due, clock[0] * 1000)


class LinterTests(unittest.TestCase):
    def test_words_and_sdk_terms(self):
        self.assertEqual(linter.forbidden_in("isTestUser"), {"test"})
        self.assertEqual(linter.forbidden_in("demo-checkout"), {"demo"})
        self.assertEqual(linter.forbidden_in("perf.thread_blockage_sample"), set())
        self.assertEqual(linter.forbidden_in("latest attestation contest"), set())
        self.assertEqual(linter.forbidden_in("SampleRate"), {"sample"})

    def test_payload_checks(self):
        bad = [{"target": "otlp", "kind": "traces", "payload": {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "travel-bff"}},
            {"key": "telemetry.sdk.language", "value": {"stringValue": "java"}}]},
            "scopeSpans": [{"scope": {"name": "x"}, "spans": [{"name": "GET /x", "kind": 2, "startTimeUnixNano": "2",
                "endTimeUnixNano": "1", "status": {"code": 0}, "attributes": [
                    {"key": "http.response.status_code", "value": {"intValue": 503}},
                    {"key": "enduser.email", "value": {"stringValue": "jane.doe@example.com"}}],
                "events": [{"name": "exception", "attributes": [
                    {"key": "exception.stacktrace", "value": {"stringValue": "Traceback (most recent call last):"}}]}]}]}]}]}}]
        codes = {i["code"] for i in linter.lint_payloads(bad)}
        self.assertEqual(codes, {"service_name", "duration", "status", "pii", "stack_language"})

    def test_generated_payloads_are_clean(self):
        r = run(presets.apply(burst(destinations={"mode": "end_to_end"}, chronosphere={"tenant": "acme-prod"}),
                              "baseline", NOW), seed=12)
        self.assertEqual(linter.lint_payloads(r.sender.deliveries), [])


if __name__ == "__main__":
    unittest.main()


class ReferenceTests(unittest.TestCase):
    def test_scrubbed_web_sample_has_no_personal_data(self):
        import re
        import sys
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from scrub_web_sample import verify, decoded_contexts, EMAIL
        path = os.path.join(ROOT, "reference", "web-sdk-2.29-sample", "web_2_29_0_sample.scrubbed.jsonl")
        text = open(path).read()
        self.assertEqual(verify(text, set()), [])
        self.assertTrue(all('"anonymous":true' in c for c in decoded_contexts(text)))
        self.assertFalse([e for e in EMAIL.findall(text) if not re.fullmatch(r"[\w.-]+@\d[\d.]*", e)])
        self.assertEqual(len(text.splitlines()), 4)
