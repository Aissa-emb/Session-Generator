"""Contract tests against the SDK behavior, with no outbound requests."""
import copy
import random
import tempfile
import unittest
from types import SimpleNamespace

from xcorgen import spec, web_vitals
from xcorgen.browser_session import BrowserSession
from xcorgen.emitted import percentile
from xcorgen.linter import lint_payloads
from xcorgen.personas import _new_persona
from xcorgen.profiles import load
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun
from xcorgen.web_contract import check
from xcorgen.web_faults import FAMILIES
from xcorgen.web_observation import attrs, records
from xcorgen.web_session import shift

NOW = 1793376000000


def scenario():
    s = spec.default_spec()
    s.update(targets=[copy.deepcopy(spec.WEB_TARGET)])
    s["population"].update(platform_mix={"vantra-web": 1})
    s["traffic"] = {"mode": "burst", "count": 90, "window": {"last_minutes": 6}}
    s["destinations"] = {"mode": "sessions_only"}
    return s


def browser(lob="retail", family="chrome", seed=37, **kwargs):
    rng = random.Random(seed)
    profile = load({"retail": "ecommerce", "banking": "banking", "travel": "travel"}[lob])
    p = _new_persona(profile.key, rng, {"browser_mix": {family: 1}})
    profile.make_user(p)
    s = BrowserSession(profile, p, "kfs9f", NOW, rng=rng, **kwargs)
    s.plan = SimpleNamespace(web_faults=[])
    return s


def items(session, kind):
    return [i for d in session.deliveries if d["target"] == "embrace" for i in records(d["payload"], kind)]


class BrowserContracts(unittest.TestCase):
    def test_archetypes_and_part_boundaries(self):
        for lob, pages, loads in (("retail", ["home", "cart", "checkout"], 3),
                                 ("banking", ["signin", "dashboard", "transfer"], 1),
                                 ("travel", ["home", "hotels", "checkout"], 2)):
            s = browser(lob)
            for page in pages:
                if s.page:
                    s.click("<button>Continue</button>")
                s.page_view(page, city="Lisbon")
                s.wait(1, 2)
            s.finish()
            spans = items(s, "spans")
            self.assertEqual(sum(x["name"] == "documentLoad" for x in spans), loads, lob)
            for d in s.deliveries:
                problems = []
                check(d["payload"], problems.append)
                self.assertEqual(problems, [], lob)
            parts = [attrs(x) for x in spans if x["name"] == "emb-session-part"]
            self.assertTrue(parts[0]["emb.cold_start"])
            self.assertFalse(any(p.get("emb.is_final_session_part") for p in parts))
            self.assertEqual(len({p["emb.user_session_id"] for p in parts}), 1)

    def test_engines_and_shift_precision(self):
        for family in ("chrome", "safari", "firefox", "samsung_internet"):
            s = browser(family=family)
            s.page_view("home")
            s.click('<button data-testid="action">Continue</button>')
            for f in FAMILIES:
                s.exception(family=f)
            s.finish()
            for d in s.deliveries:
                shift(d["payload"], -123456789)
                issues = []
                check(d["payload"], issues.append)
                self.assertEqual(issues, [], family)
            self.assertEqual(lint_payloads(s.deliveries), [], family)
            logs = items(s, "logs")
            names = {attrs(x).get("browser.web_vital.name") for x in logs}
            if family in ("safari", "firefox"):
                self.assertFalse(names & {"inp", "cls", "tbd"})

    def test_legacy_safari_keeps_one_part_across_routes(self):
        s = browser("banking", "safari")
        s.persona["browser_version"] = "26.1"
        from xcorgen.web_capabilities import capabilities
        s.caps = capabilities(s.persona)
        s.page_view("signin")
        s.click("<button>Sign in</button>")
        s.page_view("dashboard")
        s.finish()
        self.assertEqual(len(s.parts), 1)
        self.assertEqual(sum(x["name"] == "Soft Navigation" for x in items(s, "spans")), 0)
        self.assertTrue(next(attrs(x)["emb.page_load"] for x in items(s, "spans") if x["name"] == "emb-session-part"))

    def test_origin_and_persistent_browser_identity(self):
        s = browser()
        s.page_view("home")
        s.finish()
        self.assertTrue(all("Origin" not in d["headers"] for d in s.deliveries))
        second = BrowserSession(s.profile, s.persona, "kfs9f", NOW + 3600000,
                                web={"origin_header": {"policy": "site"}})
        second.page_view("home")
        second.finish()
        self.assertEqual(second.device_id, s.device_id)
        self.assertEqual(second.previous_user_session_id, s.user_session_id)
        self.assertEqual(second.user_session_number, 2)
        self.assertNotEqual(second.instance_id, s.instance_id)
        self.assertEqual({d["headers"]["Origin"] for d in second.deliveries}, {"https://www.vantra.com"})

    def test_visibility_and_logout(self):
        s = browser()
        s.page_view("home")
        page_id = s.page["id"]
        s.background(2, 2)
        self.assertEqual(s.page["id"], page_id)
        s.logout()
        parts = [attrs(x) for x in items(s, "spans") if x["name"] == "emb-session-part"]
        self.assertEqual(parts[-1]["emb.user_session_termination_reason"], "manual")
        self.assertEqual(parts[-1]["emb.is_final_session_part"], 1)
        self.assertNotIn("emb.page_load", parts[-1])

    def test_loaf_excludes_first_and_interaction_frames(self):
        s = browser()
        s.page_view("home")
        s.part.frames = []
        s.first_frame_seen = False
        s._frame(100)
        s._frame(200, True)
        s._frame(350)
        s.finish()
        loaf = next(attrs(x) for x in items(s, "logs") if x.get("eventName") == "emb-loaf-report")
        self.assertEqual(loaf["browser.web_vital.value"], 300)
        self.assertEqual(loaf["emb.tbd.loaf_total_duration"], 650)

    def test_release_stable_exception_stacks(self):
        s = browser()
        s.page_view("home")
        s.exception(family="property_undefined")
        s.exception(family="property_undefined")
        s.finish()
        exceptions = [attrs(x) for x in items(s, "logs") if x.get("eventName") == "emb-exception"]
        self.assertEqual(exceptions[0]["exception.stacktrace"], exceptions[1]["exception.stacktrace"])
        self.assertEqual(exceptions[1]["emb.exception_number"], exceptions[0]["emb.exception_number"] + 1)

    def test_target_distribution(self):
        rng = random.Random(7331)
        target = {"ttfb": 500, "fcp": 1600, "lcp": 3500, "inp": 320, "cls": .18}
        pages = [web_vitals.PageVitals(target, rng) for _ in range(12000)]
        for metric, desired in target.items():
            actual = percentile([getattr(v, metric) for v in pages], .75)
            self.assertLess(abs(actual / desired - 1), .10, metric)

    def test_exact_quota_from_emitted_payloads(self):
        random.seed(7331)
        s = scenario()
        s["client_faults"] = [{"id": "exception", "type": "js_exception", "rate": 1 / 3,
                               "filters": {"page_groups": ["Checkout"]}, "details": {"family": "property_undefined"}}]
        s["population"]["line_of_business_mix"] = {"retail": 1, "banking": 0, "travel": 0}
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(s, load_config(None), persist=False, state_dir=state, now_ms=NOW)
            skeletons, allocation, _ = run.plan_batch()
            for sk, faults in zip(skeletons, allocation):
                target, session, _, deliveries, _ = run.session(sk["start_ms"], sk, faults)
                run.record(target, session, faults, deliveries)
            row = run.emitted.summary()["faults"][0]
            self.assertEqual(row["emitted_sessions"], 30)

    def test_capability_validation(self):
        s = scenario()
        s["web_performance"] = [{"id": "safari-cls", "browser_families": ["safari"], "targets": {"cls": .2}}]
        self.assertTrue(any(i["code"] == "capability" for i in spec.errors(spec.validate(s, NOW))))
        s["web_performance"][0]["browser_families"] = ["chrome"]
        self.assertEqual(spec.errors(spec.validate(s, NOW)), [])

    def test_rollup_collapses_dynamic_product_and_account_paths(self):
        from xcorgen.url_rollup import preview
        urls = ["https://www.vantra.com/p/alpine-trail-shell/1051027", "https://www.vantra.com/p/ridge-waterproof-jacket/1055601"]
        rolled = preview(urls)
        self.assertEqual(rolled[0]["rollup"], rolled[1]["rollup"])
        self.assertIn("«hex»", preview(["https://app.vantra.com/accounts/abcdef1234567890"])[0]["rollup"])

    def test_bfcache_restores_page_identity_without_document_load(self):
        s = browser(population={"bfcache_share": 1})
        s.page_view("home")
        restored = False
        for _ in range(60):
            old_ids = {p["url"]: p["id"] for p in s.history}
            s.page_view("cart" if s.page["key"] == "home" else "home")
            if s.page["nav_type"] == "back-forward-cache":
                self.assertEqual(s.page["id"], old_ids[s.page["url"]])
                self.assertFalse(s.part.spans.get("doc"))
                self.assertFalse(s.part.cold)
                restored = True
                break
        self.assertTrue(restored)

    def test_web_faults_emit_on_selected_pages(self):
        for kind, detail, predicate in (
                ("js_exception", {"family": "json_html"}, lambda o: len(o["exceptions"]) > 0),
                ("rage_clicks", {}, lambda o: o["rage_clicks"] > 0),
                ("page_abandonment", {}, lambda o: o["abandoned"]),
                ("poor_web_vitals", {"targets": {"lcp": 10000}}, lambda o: any(v["value"] > 2000 for v in o["vitals"] if v["name"] == "lcp")),
                ("lcp_regression", {"cause": "hero_image", "p75_ms": 10000}, lambda o: any(v["value"] > 2000 for v in o["vitals"] if v["name"] == "lcp")),
                ("layout_instability", {"p75": 1}, lambda o: any(v["name"] == "cls" for v in o["vitals"])),
                ("interaction_latency", {"p75_ms": 900}, lambda o: any(v["name"] == "inp" for v in o["vitals"])),
                ("slow_server_response", {"p75_ms": 5000}, lambda o: any(v["value"] > 800 for v in o["vitals"] if v["name"] == "ttfb"))):
            random.seed(37)
            s = scenario()
            s["traffic"]["count"] = 12
            s["population"].update(line_of_business_mix={"retail": 1, "banking": 0, "travel": 0}, web={"browser_mix": {"chrome": 1}},
                                   journey_depth={"retail": 1})
            s["client_faults"] = [{"id": "f", "type": kind, "rate": 1, "details": detail}]
            with tempfile.TemporaryDirectory() as state:
                run = SpecRun(s, load_config(None), persist=False, state_dir=state, now_ms=NOW)
                sks, allocations, _ = run.plan_batch()
                for sk, faults in zip(sks, allocations):
                    target, session, _, deliveries, _ = run.session(sk["start_ms"], sk, faults)
                    run.record(target, session, faults, deliveries)
                self.assertTrue(any(predicate(o) for o in run.emitted.observations), kind)


if __name__ == "__main__":
    unittest.main()
