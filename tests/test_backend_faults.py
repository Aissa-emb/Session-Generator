import random
import tempfile
import unittest

from xcorgen import catalog, spec
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun
from xcorgen.linter import lint_payloads

NOW = 1793376000000


def build(variant, details=None, rollouts=None, count=60):
    random.seed(91)
    s = spec.default_spec()
    s["traffic"] = {"mode": "burst", "count": count, "window": {"last_minutes": 6}}
    s["destinations"] = {"mode": "end_to_end", "backend_logs": True}
    s["chronosphere"] = {"tenant": "demo-sandbox", "token_ref": "env:CHRONOSPHERE_API_TOKEN"}
    s["backend_faults"] = [{"id": variant, "type": catalog.BACKEND_VARIANTS[variant]["type"],
                            "variant": variant, "rate": .5, "details": details or {}}]
    s["rollouts"] = rollouts or []
    with tempfile.TemporaryDirectory() as state:
        run = SpecRun(s, load_config(None), persist=False, state_dir=state, now_ms=NOW)
        skeletons, assignments, allocation = run.plan_batch()
        sessions = []
        for sk, faults in zip(skeletons, assignments):
            target, session, _, deliveries, _ = run.session(sk["start_ms"], sk, faults)
            run.record(target, session, faults, deliveries)
            sessions.append((sk, session, deliveries))
    return run, sessions, allocation


def traces(deliveries):
    for d in deliveries:
        if d["target"] == "otlp" and d["kind"] == "traces":
            for rs in d["payload"]["resourceSpans"]:
                resource = {a["key"]: next(iter(a["value"].values())) for a in rs["resource"]["attributes"]}
                for scope in rs["scopeSpans"]:
                    for span in scope["spans"]:
                        yield resource, span, {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


class BackendFaultTests(unittest.TestCase):
    def test_each_new_builder_matches_allocated_quota(self):
        for name in catalog.NEW_BACKEND_VARIANTS:
            run, sessions, allocation = build(name)
            row = run.emitted.summary()["faults"][0]
            self.assertEqual(row["emitted_sessions"], allocation[name]["assigned"], name)
            for _, _, deliveries in sessions:
                self.assertEqual(lint_payloads(deliveries), [], name)

    def test_cache_fallback_and_fail_closed(self):
        for fallback in (True, False):
            run, sessions, _ = build("catalog-cache-outage", {"fallback": fallback})
            affected = [d for _, s, d in sessions if s.plan.backend_fault]
            self.assertTrue(affected)
            for d in affected:
                spans = list(traces(d))
                failing = [s for _, s, a in spans if a.get("circuit_breaker.state") == "open"]
                self.assertTrue(failing)
                self.assertTrue(all(s["status"]["code"] == 2 for s in failing))
                if fallback:
                    self.assertTrue(any(s["name"] == "SELECT catalog.products" for _, s, _ in spans))
            self.assertTrue(run.emitted.summary()["faults"][0]["within_tolerance"])

    def test_rate_limit_has_no_experience_child_and_retries_after_delay(self):
        _, sessions, _ = build("gateway-rate-limited", {"retry_after_seconds": 5})
        for _, s, ds in sessions:
            for request in s.plan.evidence:
                if request["status"] != 429:
                    continue
                spans = [(r, sp, a) for r, sp, a in traces(ds) if sp["traceId"] == request["trace_id"]]
                self.assertTrue(all(r["service.name"] == "api-gateway" for r, _, _ in spans))
                root = next(a for _, sp, a in spans if sp["kind"] == 2)
                self.assertEqual(root["http.response.header.retry_after"], {"values": [{"stringValue": "5"}]})
                retries = [r for r in s.plan.evidence if r["key"] == request["key"] and r["start"] > request["start"]]
                self.assertEqual(len(retries), 1)
                self.assertGreaterEqual(retries[0]["start"] - request["end"], 5000)

    def test_lag_and_pending_status(self):
        _, sessions, _ = build("fulfillment-consumer-lag", {"lag_seconds": 120})
        found = 0
        for _, session, ds in sessions:
            spans = {s["spanId"]: s for _, s, _ in traces(ds)}
            for _, consumer, a in traces(ds):
                if "messaging.kafka.consumer.lag" not in a:
                    continue
                found += 1
                producer = spans[consumer["parentSpanId"]]
                delta = (int(consumer["startTimeUnixNano"]) - int(producer["endTimeUnixNano"])) / 1e6
                self.assertAlmostEqual(delta, 120000, places=3)
            if session.plan.backend_fault:
                self.assertTrue(any(r["key"] == "operation_status" and r["data"]["state"] == "pending" for r in session.plan.evidence))
        self.assertGreater(found, 0)

    def test_canary_version_and_rollback(self):
        rollback = NOW - 180000
        run, sessions, allocation = build("catalog-deploy-regression", rollouts=[{
            "id": "catalog", "kind": "service_version", "subject": "product-catalog-service",
            "canary_share": .5, "rollback_at": spec.iso(rollback)}], count=300)
        errors = 0
        for sk, _, ds in sessions:
            for r, span, _ in traces(ds):
                if r["service.name"] != "product-catalog-service":
                    continue
                if r["service.version"] == "5.23.0":
                    self.assertLess(sk["start_ms"], rollback)
                if span.get("events"):
                    errors += 1
                    self.assertEqual(r["service.version"], "5.23.0")
        self.assertGreater(errors, 0)
        self.assertEqual(run.emitted.summary()["faults"][0]["emitted_sessions"], allocation["catalog-deploy-regression"]["assigned"])

    def test_lag_limit_is_enforced(self):
        s = spec.default_spec()
        s["backend_faults"] = [{"id": "lag", "type": "kafka_consumer_lag", "details": {"lag_seconds": 600}}]
        self.assertTrue(any(i["path"].endswith("lag_seconds") for i in spec.errors(spec.validate(s, NOW))))
