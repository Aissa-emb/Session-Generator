"""Statistical contracts and executed journey evidence, with no remote traffic."""
import copy
import random
import tempfile
import unittest
from collections import Counter

from xcorgen import spec, web_vitals
from xcorgen.emitted import Emitted
from xcorgen.linter import lint_payloads
from xcorgen.personas import PersonaPool, _new_persona, browser_weights
from xcorgen.population import INTENTS, shape
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun
from xcorgen.web_contract import check

NOW = 1793638800000
TARGETS = {"android": spec.default_spec()["targets"][0], "ios": spec.IOS_TARGET, "web": spec.WEB_TARGET}


def scenario(platform="android", lob="retail", **population):
    s = spec.default_spec()
    t = copy.deepcopy(TARGETS[platform])
    s["targets"] = [t]
    s["population"].update(model="varied", platform_mix={t["id"]: 1},
                           line_of_business_mix={k: int(k == lob) for k in INTENTS}, **population)
    s["traffic"] = {"mode": "burst", "count": 40, "window": {"last_minutes": 5}}
    return s


class PopulationTests(unittest.TestCase):
    def test_network_baseline_emits_actual_failures_without_changing_visit_intents(self):
        # Inspect serialized requests, not assignment labels. The live policy is
        # balanced in shuffled blocks, so it cannot silently yield 250 clean visits.
        for platform in TARGETS:
            for mode in ("burst", "live"):
                with self.subTest(platform=platform, mode=mode), tempfile.TemporaryDirectory() as state:
                    random.seed(7331)
                    s = scenario(platform)
                    s["population"]["line_of_business_mix"] = {lob: 1 for lob in INTENTS}
                    s["traffic"] = ({"mode": "burst", "count": 300, "window": {"last_minutes": 5}}
                                    if mode == "burst" else {"mode": "live", "rate_per_minute": 20, "duration_minutes": 20})
                    s["client_faults"] = [{"id": "network", "type": "network_failure", "rate": .15,
                                           "quota": "exact", "steer_journey": False,
                                           "details": {"kinds": ["timeout", "reset"]}}]
                    run = SpecRun(s, load_config(None), persist=False, now_ms=NOW, state_dir=state)
                    if mode == "burst":
                        skeletons, allocations, _ = run.plan_batch()
                    else:
                        target, gen = next(iter(run.generators.values()))
                        skeletons = [run.planner.skeleton(gen, target, NOW - 240000 + i * 500,
                                                         NOW, run.mode) for i in range(300)]
                        allocations = [run.planner.allocate_one(sk) for sk in skeletons]
                    affected = requests = failures = 0
                    for sk, faults in zip(skeletons, allocations):
                        original_shape = copy.deepcopy(sk["shape"])
                        target, session, _, deliveries, _ = run.session(sk["start_ms"], sk, faults)
                        self.assertEqual(sk["shape"], original_shape)
                        run.record(target, session, faults, deliveries)
                        net = run.emitted.observations[-1]["net"]
                        bad = [n for n in net if n["status"] is None and n["error_type"]]
                        self.assertEqual(len(bad), int("network" in faults))
                        for failure in bad:
                            self.assertTrue(any(n["endpoint"] == failure["endpoint"] and n["status"] == 200
                                                and n["start_ms"] > failure["start_ms"] for n in net))
                        self.assertEqual(lint_payloads(deliveries), [])
                        affected += bool(bad)
                        failures += len(bad)
                        requests += len(net)
                    self.assertEqual(affected, 45)
                    self.assertEqual(failures, 45)
                    self.assertGreater(requests, failures)
                    self.assertTrue(run.emitted.summary()["faults"][0]["within_tolerance"])

    def test_browser_margins_are_shares_not_multipliers(self):
        rng = random.Random(91)
        options = {"browser_mix": {"chrome": 50, "safari": 30, "edge": 10, "firefox": 5, "samsung_internet": 5},
                   "device_type_mix": {"desktop": 60, "mobile": 40}}
        people = [_new_persona("ecommerce", rng, options) for _ in range(12000)]
        for key, mix in (("browser", options["browser_mix"]), ("kind", options["device_type_mix"])):
            counts = Counter(p[key] for p in people)
            for name, expected in mix.items():
                self.assertAlmostEqual(counts[name] / len(people) * 100, expected, delta=1.5)
        with self.assertRaises(ValueError):
            browser_weights("ecommerce", (("firefox", 1),), (("mobile", 1),))

    def test_full_pool_keeps_new_visitors_and_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            pool = PersonaPool("ecommerce", d, size=150, returning_share=.45, rng=random.Random(44))
            returned = []
            for i in range(2000):
                p = pool.pick(lambda _: None, now=2000000 + i * 2000)
                if i >= 500:
                    returned.append(p["returning_visit"])
            self.assertEqual(len(pool.personas), 150)
            self.assertAlmostEqual(sum(returned) / len(returned), .45, delta=.05)
            ids = [pool.pick(lambda _: None, now=10000000)["device_id"] for _ in range(400)]
            self.assertEqual(len(set(ids)), 400)

    def test_intent_probabilities_all_domains(self):
        rng = random.Random(411)
        for lob, mix in INTENTS.items():
            counts = Counter(shape(lob, {}, rng)["intent"] for _ in range(15000))
            for intent, weight in mix.items():
                self.assertAlmostEqual(counts[intent] / 150, weight, delta=1.3, msg=lob + intent)

    def test_executed_intents_on_every_platform(self):
        for platform in TARGETS:
            for lob, mix in INTENTS.items():
                for intent in mix:
                    with self.subTest(platform=platform, intent=intent), tempfile.TemporaryDirectory() as d:
                        random.seed(42)
                        s = scenario(platform, lob, intent_mix={lob: {intent: 1}}, native_background_share=0)
                        run = SpecRun(s, load_config(None), persist=False, now_ms=NOW, state_dir=d)
                        t, session, _, ds, faults = run.session(NOW - 240000)
                        run.record(t, session, faults, ds)
                        o = run.emitted.observations[0]
                        self.assertEqual(o["visit_intent"], intent)
                        self.assertEqual(lint_payloads(ds), [])
                        if platform == "web":
                            problems = []
                            for delivery in ds:
                                if delivery["target"] == "embrace":
                                    check(delivery["payload"], problems.append)
                            self.assertEqual(problems, [])
                        if intent == "bouncer":
                            self.assertEqual(o["endpoints"], {"home"})
                            self.assertFalse(any(v["name"] == "inp" for v in o.get("vitals", [])))
                        if intent in ("browser", "searcher", "cart_abandoner") and lob == "retail":
                            self.assertNotIn("place_order", o["endpoints"])
                        if intent == "cart_abandoner":
                            self.assertIn("add_to_cart", o["endpoints"])
                        if intent == "quick_check":
                            self.assertNotIn("transactions", o["endpoints"])
                            self.assertNotIn("create_transfer", o["endpoints"])
                        if intent == "transfer_abandoner":
                            self.assertIn("payees", o["endpoints"])
                            self.assertNotIn("create_transfer", o["endpoints"])
                        if intent == "booking_abandoner":
                            self.assertIn("offer", o["endpoints"])
                            self.assertNotIn("create_booking", o["endpoints"])
                        if platform != "web":
                            self.assertFalse(o.get("vitals"))
                        self.assertGreater(run.emitted.summary()["population"][0]["duration_seconds"]["p50"], 0)

    def test_returning_native_process_and_session_identity(self):
        for platform in ("android", "ios"):
            with tempfile.TemporaryDirectory() as d:
                random.seed(51)
                s = scenario(platform, "banking", native_cold_start_share=0, native_background_share=0,
                             intent_mix={"banking": {"quick_check": 1}})
                run = SpecRun(s, load_config(None), persist=False, now_ms=NOW, state_dir=d)
                t, gen = next(iter(run.generators.values()))
                sk = run.planner.skeleton(gen, t, NOW - 3600000, NOW, "sessions_only")
                _, first, _, ds, fs = run.session(sk["start_ms"], sk, [])
                run.record(t, first, fs, ds)
                again = dict(sk, start_ms=NOW - 60000, visit=dict(sk["visit"], returning_visit=True))
                _, second, _, ds, fs = run.session(again["start_ms"], again, [])
                run.record(t, second, fs, ds)
                a, b = run.emitted.observations
                self.assertTrue(a["cold_start"])
                self.assertFalse(b["cold_start"])
                self.assertEqual(first.process_id, second.process_id)
                self.assertNotEqual(a["session_id"], b["session_id"])
                self.assertEqual(b["visitor_type"], "returning")
                self.assertEqual(first.persona["device_id"], second.persona["device_id"])

    def test_batch_visit_conditions_are_frozen(self):
        with tempfile.TemporaryDirectory() as d:
            run = SpecRun(scenario(), load_config(None), persist=False, now_ms=NOW, state_dir=d)
            t, gen = next(iter(run.generators.values()))
            sk = run.planner.skeleton(gen, t, NOW - 300000, NOW, "sessions_only")
            saved = sk["visit"]["connection"]
            sk["persona"].update(connection="slow-3g", returning_visit=True)
            _, x, _, _, _ = run.session(sk["start_ms"], sk, [])
            self.assertEqual(x.properties["network_profile"], saved)
            self.assertEqual(x.properties["visitor_type"], "new")

    def test_network_affects_load_not_synchronous_interaction(self):
        fast = dict(population_model="1", rtt=25, mbps=30, cpu_factor=1)
        slow = dict(fast, rtt=400, mbps=.5)
        a, _ = web_vitals.targets([], "Home", "mobile", "chrome", fast)
        b, _ = web_vitals.targets([], "Home", "mobile", "chrome", slow)
        self.assertGreater(b["lcp"], a["lcp"])
        self.assertGreater(b["ttfb"], a["ttfb"])
        self.assertEqual(b["inp"], a["inp"])
        c, _ = web_vitals.targets([], "Home", "mobile", "chrome", dict(fast, cpu_factor=1.9))
        self.assertGreater(c["inp"], a["inp"])

    def test_invalid_mixes_rejected(self):
        for changes in ({"intent_mix": {"banking": {"buyer": 1}}}, {"network_mix": {"wifi": 0}},
                        {"web": {"browser_mix": {}}}, {"geo_mix": {"mars": 1}},
                        {"web": {"browser_mix": {"firefox": 1}, "device_type_mix": {"mobile": 1}}}):
            self.assertTrue(spec.errors(spec.validate(scenario(**changes), now_ms=NOW)))

    def test_selected_faults_keep_exact_quota_with_varied_intents(self):
        for platform in TARGETS:
            with tempfile.TemporaryDirectory() as d:
                random.seed(72)
                s = scenario(platform)
                s["traffic"]["count"] = 120
                s["backend_faults"] = [{"id": "payment", "type": "db_pool_exhaustion",
                                        "variant": "payments-db-pool", "rate": .2}]
                run = SpecRun(s, load_config(None), persist=False, now_ms=NOW, state_dir=d, rng=random.Random(72))
                skeletons, allocations, _ = run.plan_batch()
                for sk, faults in zip(skeletons, allocations):
                    t, session, _, ds, fs = run.session(sk["start_ms"], sk, faults)
                    run.record(t, session, fs, ds)
                measured = run.emitted.summary()["faults"][0]
                self.assertEqual(measured["requested_sessions"], 24)
                self.assertEqual(measured["emitted_sessions"], 24)
