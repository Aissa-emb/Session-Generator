"""Per-vital regressions from the Customize sliders: they only worsen pages, and their shares are reported honestly."""
import copy
import random
import tempfile
import time
import unittest
from types import SimpleNamespace

from xcorgen import spec as specs
from xcorgen.browser_session import BrowserSession
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun

NOW = time.time() * 1000
FAULTS = {"lcp": ("lcp_regression", {"p75_ms": 3800, "cause": "hero_image"}), "inp": ("interaction_latency", {"p75_ms": 450}),
          "cls": ("layout_instability", {"p75": .3}), "fcp": ("poor_web_vitals", {"targets": {"fcp": 3200}}),
          "ttfb": ("slow_server_response", {"p75_ms": 2200})}


class Collect:
    class _Pool:
        def shutdown(self, wait=True):
            pass

    def __init__(self):
        self.deliveries, self.last_errors = [], []
        self.stats = {"embrace_spans": 0, "embrace_logs": 0, "otlp_traces": 0, "otlp_logs": 0, "errors": 0}
        self.pool = self._Pool()

    def submit(self, d):
        self.deliveries.append(d)


def targets_after(faults, natural):
    t = dict(natural)
    page = SimpleNamespace(page={"faults": [{"type": kind, "details": details} for kind, details in faults]})
    BrowserSession._fault_targets(page, t)
    return t


class VitalControlTests(unittest.TestCase):
    def test_regressions_never_improve_a_naturally_slow_page(self):
        slow = {"ttfb": 1500, "fcp": 3600, "lcp": 6000, "inp": 700, "cls": .4}
        for kind, details in FAULTS.values():
            with self.subTest(kind=kind):
                t = targets_after([(kind, details)], slow)
                for metric, value in slow.items():
                    self.assertGreaterEqual(t[metric], value, metric)
        fast = {"ttfb": 300, "fcp": 800, "lcp": 1500, "inp": 90, "cls": .02}
        t = targets_after([(kind, details) for kind, details in FAULTS.values()], fast)
        self.assertEqual((t["lcp"], t["inp"], t["cls"], t["ttfb"]), (3800, 450, .3, 2200))
        self.assertEqual(t["fcp"], 3200)
        # An FCP regression still keeps LCP after FCP.
        self.assertGreaterEqual(targets_after([FAULTS["fcp"]], fast)["lcp"], 3350)

    def test_vital_shares_are_reported_with_natural_variation_not_verified(self):
        s = specs.default_spec()
        t = copy.deepcopy(specs.WEB_TARGET)
        t.update(embrace_app_id="kfs9f", app_name="Vantra Web")
        s["targets"] = [t]
        s["population"]["platform_mix"] = {t["id"]: 1}
        s["traffic"] = {"mode": "burst", "count": 120, "window": {"last_minutes": 5}}
        s["client_faults"] = [{"id": k + "-web", "type": kind, "platform": "web", "rate": .25, "quota": "exact", "details": d}
                              for k, (kind, d) in FAULTS.items()]
        random.seed(8)
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(s, load_config(None), persist=False, now_ms=NOW, state_dir=state, rng=random.Random(8))
            run.run_batch(Collect(), quiet=True)
        rows = {r["id"]: r for r in run.emitted.summary()["faults"]}
        for k in FAULTS:
            row = rows[k + "-web"]
            self.assertEqual(row["evidence"], "web vitals")
            self.assertTrue(row["natural_overlap"])
            self.assertIsNone(row["within_tolerance"])
            self.assertEqual(row["requested_pct"], 25.0)
            self.assertIsNotNone(row["emitted_pct"])
        table = run.emitted.table()
        self.assertIn("includes natural variation", table)
        self.assertNotIn("OUT OF TOLERANCE", table)


if __name__ == "__main__":
    unittest.main()
