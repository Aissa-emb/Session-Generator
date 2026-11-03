import concurrent.futures
import json
import random
import unittest

from xcorgen import backend as b
from xcorgen.runner import Generator, load_config
from xcorgen.scenarios import Runtime, REQUEST, FEATURES
from xcorgen.profiles.platform import P
from xcorgen.milestones import FLOWS


def attributes(record):
    return {a["key"]: next(iter(a["value"].values())) if isinstance(a["value"], dict) else a["value"]
            for a in record.get("attributes", [])}


def generate(profile, incident=None, primary=None, family=None, phase="incident", seed=14):
    random.seed(seed)
    cfg = load_config(None)
    cfg["embrace"]["apps"]["default"] = "dryrn"
    cfg["generation"] = {"primary_scenario": primary, "crash_family": family, "incident_phase": phase}
    gen = Generator([profile], cfg, incident=incident, persist=False, backend_logs=True)
    for pool in gen.pools.values():
        pool.personas = []
    return gen.session(1793300400000)[0]


def spans(s):
    return [span for d in s.deliveries if d["target"] == "otlp" and d["kind"] == "traces"
            for rs in d["payload"]["resourceSpans"] for ss in rs["scopeSpans"] for span in ss["spans"]]


class ScenarioTests(unittest.TestCase):
    def test_schedule_uses_request_time_and_invalid_budgets_fail_before_generation(self):
        from xcorgen.scenarios import incident_phase_at, validate_settings
        for minute, phase in ((1, "onset"), (3, "incident"), (53, "incident"), (54, "recovery"), (61, "onset")):
            self.assertEqual(incident_phase_at(minute * 60000, {}), phase)
        s = generate("ecommerce", "payments-db-pool")
        s.plan.settings["incident_phase"] = "scheduled"
        s.plan.used = False
        s.t = 54 * 60000
        r = s.plan.request(s.plan.target, {})
        self.assertEqual(r["attributes"]["incident.phase"], "recovery")
        self.assertEqual(r["outcome"], "ok")
        for settings in ({"canary_share": 1}, {"transport_share": float("nan")},
                         {"scenario_weights": {"clean": .1}}, {"incident_cycle_minutes": 5},
                         {"crash_family": 9}, {"journey_weights": {"banking_priority": 2}},
                         {"crash_session_rate": .8, "blockage_session_rate": .3},
                         {"blockage_session_rate": float("nan")}, {"crash_session_rate": -1}):
            with self.assertRaises(ValueError):
                validate_settings(settings)

    def test_verbose_summary_accepts_transport_without_http_response(self):
        from xcorgen.runner import describe
        s = generate("banking", primary="clean")
        s.calls.append(("accounts", None, "1" * 32))
        self.assertIn("None", describe(s, "accounts", "tenant"))

    def test_shared_pool_has_real_holders_and_no_sql_after_failed_acquisition(self):
        paired = [generate(p, "payments-db-pool") for p in ("ecommerce", "travel")]
        occurrences = []
        for s in paired:
            requests = [r for r in s.plan.evidence if r["status"] == 502]
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertGreater(request["end"] - request["start"], 3000)
            trace = [x for x in spans(s) if x["traceId"] == request["trace_id"]]
            wait = next(x for x in trace if x["name"] == "HikariPool.getConnection")
            self.assertEqual((int(wait["endTimeUnixNano"]) - int(wait["startTimeUnixNano"])) / 1e6, 3000)
            self.assertFalse(any(x["name"] == "SELECT payments.payment_tokens" for x in trace))
            a = attributes(wait)
            self.assertEqual((a["pool.active"], a["pool.idle"], a["pool.waiting"]), (50, 0, 32))
            self.assertEqual(a["db.connection.acquired"], False)
            occurrences.append(a["incident.occurrence_id"])
            holders = [x for x in spans(s) if attributes(x).get("db.connection.acquired") is True]
            self.assertEqual(len(holders), 50)
            self.assertTrue(wait["links"])
            for holder in holders:
                self.assertLessEqual(int(holder["startTimeUnixNano"]), int(wait["startTimeUnixNano"]))
                self.assertGreaterEqual(int(holder["endTimeUnixNano"]), int(wait["endTimeUnixNano"]))
        self.assertEqual(*occurrences)

    def test_pool_recovery_restores_same_service_and_confirmation(self):
        for profile, key, end in (("ecommerce", "place_order", "order_confirmation_viewed"),
                                  ("travel", "create_booking", "booking_confirmation_viewed")):
            s = generate(profile, "payments-db-pool", phase="recovery")
            r = next(r for r in s.plan.evidence if r["key"] == key)
            self.assertEqual(r["status"], 201)
            self.assertIn(end, [m["name"] for m in s.milestones])
            self.assertFalse(any(x["name"] == "HikariPool.getConnection" for x in spans(s)))

    def test_processor_and_pnr_timeouts_reconcile_without_duplicate_submission(self):
        for profile, incident, key, timeout, confirmation in (
            ("ecommerce", "payment-timeouts", "place_order", 8000, "order_confirmation_viewed"),
            ("travel", "gds-pnr-timeouts", "create_booking", 5000, "booking_confirmation_viewed")):
            s = generate(profile, incident)
            calls = [r for r in s.plan.evidence if r["key"] == key]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["status"], 504)
            self.assertIn("operation_status", [r["key"] for r in s.plan.evidence])
            self.assertNotIn(confirmation, [m["name"] for m in s.milestones])
            bounded = [x for x in spans(s) if attributes(x).get("request.deadline_ms") == timeout]
            self.assertTrue(bounded)
            self.assertTrue(all(int(x["endTimeUnixNano"]) - int(x["startTimeUnixNano"]) == timeout * 1000000 for x in bounded))

    def test_fallback_keeps_http_success_and_failed_child(self):
        for profile, incident, key, milestone in (
            ("ecommerce", "recs-latency", "get_recommendations", "default_recommendations_displayed"),
            ("banking", "account-read-fallback", "accounts", "cached_account_snapshot_displayed"),
            ("travel", "hotel-supplier-outage", "hotel_search", "cached_hotel_rates_displayed"),
            ("travel", "supplier-timeouts", "flight_search", "partial_flight_results_displayed")):
            s = generate(profile, incident)
            r = next(r for r in s.plan.evidence if r["key"] == key)
            self.assertEqual(r["status"], 200)
            self.assertGreater(r["backend_errors"], 0)
            if profile != "banking":
                self.assertIn(milestone, [m["name"] for m in s.milestones])
            task = next(x for d in s.deliveries if d.get("fmt") == "envelope"
                        for x in d["payload"]["data"].get("spans", [])
                        if attributes(x).get("app.operation") == key)
            self.assertEqual(task["status"], "Unset")
            self.assertEqual(attributes(task)["app.fallback.used"], "true")
            root = next(x for x in spans(s) if x["spanId"] == r["first_backend_span_id"])
            self.assertEqual(root["status"]["code"], 0)

    def test_fraud_caller_deadline_and_residual_server_are_distinct(self):
        s = generate("banking", "fraud-latency")
        r = next(r for r in s.plan.evidence if r["key"] == "create_transfer")
        self.assertEqual(r["status"], 504)
        calls = [x for x in spans(s) if attributes(x).get("rpc.deadline_ms") == 4000]
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(int(call["endTimeUnixNano"]) - int(call["startTimeUnixNano"]), 4000000000)
            child = next(x for x in spans(s) if x.get("parentSpanId") == call["spanId"])
            self.assertTrue(attributes(child)["execution.residual_after_deadline"])

    def test_n_plus_one_count_matches_actual_review_rows(self):
        s = generate("ecommerce", "reviews-n-plus-one")
        trace = next(r["trace_id"] for r in s.plan.evidence if r["key"] == "get_product")
        records = [x for x in spans(s) if x["traceId"] == trace]
        count = next(attributes(x)["reviews.loaded"] for x in records if "reviews.loaded" in attributes(x))
        self.assertEqual(count, sum(x["name"] == "SELECT reviews.review_media" for x in records))

    def test_all_crash_families_terminate_then_use_a_new_process(self):
        for profile in ("ecommerce", "banking", "travel"):
            groups = set()
            for family in range(5):
                s = generate(profile, primary="crash", family=family, seed=family + 23)
                logs = [x for d in s.deliveries if d.get("fmt") == "envelope"
                        for x in d["payload"]["data"].get("logs", []) if attributes(x).get("emb.type") == "sys.android.crash"]
                self.assertEqual(len(logs), 1)
                groups.add(attributes(logs[0])["exception.type"])
                crash_time = logs[0]["time_unix_nano"]
                crash_part = attributes(logs[0])["emb.session_part_id"]
                parts = [x for d in s.deliveries if d.get("fmt") == "envelope"
                         for x in d["payload"]["data"].get("spans", []) if x["name"] == "emb-session"]
                dead = next(p for p in parts if attributes(p)["emb.session_part_id"] == crash_part)
                self.assertTrue(all(e["time_unix_nano"] <= crash_time for e in dead["events"]))
                self.assertEqual(len(parts), 2)
                self.assertNotEqual(attributes(parts[0])["emb.process_identifier"], attributes(parts[1])["emb.process_identifier"])
            self.assertEqual(len(groups), 5)

    def test_blockage_has_sdk_samples_and_no_intervening_ui(self):
        s = generate("travel", primary="blockage")
        records = [x for d in s.deliveries if d.get("fmt") == "envelope" for x in d["payload"]["data"].get("spans", [])]
        block = next(x for x in records if x["name"] == "emb-thread-blockage")
        self.assertTrue(block["events"])
        self.assertEqual(attributes(block)["emb.type"], "perf.thread_blockage")
        self.assertGreaterEqual(block["end_time_unix_nano"] - block["start_time_unix_nano"], 6000000000)
        for part in (x for x in records if x["name"] == "emb-session"):
            for event in part["events"]:
                self.assertFalse(block["start_time_unix_nano"] < event["time_unix_nano"] < block["end_time_unix_nano"])

    def test_request_context_reaches_redis_but_never_resource(self):
        s = generate("ecommerce", "payments-db-pool")
        for request in s.plan.evidence:
            records = [x for x in spans(s) if x["traceId"] == request["trace_id"]]
            for span in records:
                for k, v in request["context"].items():
                    self.assertEqual(attributes(span)[k], v)
            if request["key"] not in FEATURES["ecommerce"]:
                self.assertFalse(any(k.startswith("feature.") for k in request["context"]))
        for d in s.deliveries:
            for rs in d["payload"].get("resourceSpans", []):
                self.assertNotIn("release_ring", attributes(rs["resource"]))

    def test_context_does_not_leak_between_concurrent_requests(self):
        def build(ring):
            token = REQUEST.set({"attributes": {"release_ring": ring}})
            try:
                tree = b.redis(P["api-gateway"], "GET", "token:secret-value", "cache.internal")
                return b.build_trace(tree, "1" * 32, "2" * 16, 1, random.Random(2), REQUEST.get()["attributes"])
            finally:
                REQUEST.reset(token)
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results = list(executor.map(build, ["stable", "canary"] * 20))
        for result, ring in zip(results, ["stable", "canary"] * 20):
            a = attributes(result.spans[0][3])
            self.assertEqual(a["release_ring"], ring)
            self.assertEqual(a["db.query.text"], "GET token:?")
        self.assertIsNone(REQUEST.get())

    def test_cache_expiry_is_stateful(self):
        cache = Runtime()
        self.assertFalse(cache.lookup("key", 1000, initial_hit=0))
        cache.fill("key", 1000, ttl=100)
        self.assertTrue(cache.lookup("key", 1099))
        self.assertFalse(cache.lookup("key", 1100))

    def test_no_backend_or_http_status_for_pre_server_transport_failure(self):
        from xcorgen.calls import simulate
        s = generate("banking", primary="clean")
        for cause in ("dns", "offline", "cancelled", "tls"):
            c = simulate(s, "accounts", {"_transport": cause}, s.t)
            self.assertIsNone(c.status)
            self.assertIsNone(c.trace_build)
            self.assertEqual(c.response_bytes, 0)

    def test_timeout_after_gateway_keeps_backend_trace_without_client_response(self):
        from xcorgen.calls import simulate
        s = generate("banking", primary="clean")
        for cause in ("timeout", "reset"):
            c = simulate(s, "accounts", {"_transport": cause}, s.t)
            self.assertIsNone(c.status)
            self.assertEqual(c.response_bytes, 0)
            self.assertIsNotNone(c.trace_build)
            backend_end = max(int(x["endTimeUnixNano"]) for _, _, _, x in c.trace_build.spans) / 1e6
            self.assertLessEqual(backend_end, c.end)

    def test_idempotency_replay_never_reauthorizes_or_confirms_pending(self):
        from xcorgen.calls import simulate
        for incident, phase, expected in (("payments-db-pool", "recovery", 201), ("payment-timeouts", "incident", 409)):
            s = generate("ecommerce", incident, phase=phase)
            before = dict(s.plan.runtime.operations[s.plan.operation_id])
            call = simulate(s, "place_order", {}, s.t)
            self.assertEqual(call.status, expected)
            self.assertEqual(s.plan.runtime.operations[s.plan.operation_id], before)
            self.assertFalse(any(svc.name == "payment-authorization-service" for svc, _, _, _ in call.trace_build.spans))

    def test_cohort_persists_when_an_anonymous_visitor_logs_in(self):
        from xcorgen.scenarios import Plan
        s = generate("ecommerce", primary="clean")
        old = s.plan.ring
        s.persona["user_id"] = "newly-authenticated-account"
        plan = Plan(s, s.plan.runtime, {"primary_scenario": "clean"})
        self.assertEqual(plan.ring, old)
        self.assertEqual(s.properties["feature_exposed"], "false")

    def test_flow_catalog_timeouts_and_breadcrumb_budgets(self):
        from tools.calibrate import timeline_metrics
        self.assertEqual(sum(len(v) for v in FLOWS.values()), 12)
        for profile in FLOWS:
            for _, start, end, timeout in FLOWS[profile]:
                self.assertNotEqual(start, end)
                self.assertLessEqual(timeout, 300)
            for primary in (None, "clean", "mobile", "backend", "blockage", "crash"):
                for seed in range(20):
                    s = generate(profile, primary=primary, seed=seed)
                    self.assertLessEqual(timeline_metrics(s.deliveries)["max_consecutive_breadcrumbs"], 2)
                    self.assertLessEqual(len(getattr(s, "milestones", [])), 12)

    def test_business_root_spans_match_failed_network_attempts(self):
        from xcorgen.journeys import ROOT_OPERATIONS
        for profile in FLOWS:
            s = generate(profile, primary="mobile")
            tasks = [x for d in s.deliveries if d.get("fmt") == "envelope"
                     for x in d["payload"]["data"].get("spans", []) if attributes(x).get("app.operation")]
            for request in s.plan.evidence:
                if request["key"] not in ROOT_OPERATIONS:
                    continue
                task = next(x for x in tasks if attributes(x)["app.operation"] == request["key"]
                            and abs(x["start_time_unix_nano"] / 1e6 - request["start"]) < 1)
                self.assertIn(task.get("parent_span_id"), (None, "", "0" * 16))
                self.assertEqual(task["status"] == "Error", request["status"] is None or request["status"] >= 400)
                self.assertGreaterEqual(task["end_time_unix_nano"] / 1e6, request["end"])

    def test_completed_tasks_do_not_extend_past_fatal_process_termination(self):
        for profile in FLOWS:
            s = generate(profile, primary="crash", family=0)
            crash = next(x for d in s.deliveries if d.get("fmt") == "envelope"
                         for x in d["payload"]["data"].get("logs", []) if attributes(x).get("emb.type") == "sys.android.crash")
            part = attributes(crash)["emb.session_part_id"]
            for d in s.deliveries:
                if d.get("fmt") == "envelope":
                    for span in d["payload"]["data"].get("spans", []):
                        if attributes(span).get("app.operation") and attributes(span).get("emb.session_part_id") == part:
                            self.assertLessEqual(span["end_time_unix_nano"], crash["time_unix_nano"])


if __name__ == "__main__":
    unittest.main()
