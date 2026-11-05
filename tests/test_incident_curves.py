"""Smooth (bell-shaped) incident curves and baseline rates, as the Customize sliders request them."""
import math
import random
import tempfile
import time
import unittest

from xcorgen import spec as specs
from xcorgen.emitted import affected
from xcorgen.planner import Planner
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun

NOW = time.time() * 1000
START = "2026-10-30T14:00:00Z"


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


def bell_spec(count=400, baseline=.05, peak=.5):
    """A network failure share that rises from a baseline to a peak in the middle of an 8-minute burst."""
    s = specs.default_spec()
    s["traffic"] = {"mode": "burst", "count": count, "window": {"last_minutes": 8}}
    s["client_faults"] = [{"id": "network-android", "type": "network_failure", "platform": "android", "rate": peak,
                           "quota": "exact", "steer_journey": False, "details": {"kinds": ["timeout", "reset"]}}]
    s["incidents"] = [{"id": "network-android-curve", "fault_ref": "network-android", "start": specs.iso(NOW - 6 * 60000),
                       "onset_minutes": 2, "peak_minutes": .1, "recovery_minutes": 2, "peak_rate": peak,
                       "baseline_rate": baseline, "curve": "smooth"}]
    return s


class CurveTests(unittest.TestCase):
    def test_smooth_ramp_is_a_raised_cosine(self):
        w = specs.incident_window({"start": START, "onset_minutes": 10, "peak_minutes": 1, "recovery_minutes": 10,
                                   "curve": "smooth"})
        at = lambda minute: specs.ramp(specs.parse_time(START) + minute * 60000, w)
        self.assertEqual(at(-1), 0)
        self.assertAlmostEqual(at(2.5), (1 - math.cos(math.pi / 4)) / 2)
        self.assertAlmostEqual(at(5), .5)
        self.assertEqual(at(10.5), 1)
        self.assertAlmostEqual(at(16), .5)
        self.assertAlmostEqual(at(18.5), (1 - math.cos(math.pi / 4)) / 2)
        self.assertEqual(at(22), 0)
        # Linear stays the default.
        linear = specs.incident_window({"start": START, "onset_minutes": 10, "peak_minutes": 1, "recovery_minutes": 10})
        self.assertAlmostEqual(specs.ramp(specs.parse_time(START) + 150000, linear), .25)

    def test_repeating_smooth_waves(self):
        w = specs.incident_window({"start": START, "onset_minutes": 3, "peak_minutes": .5, "recovery_minutes": 3,
                                   "repeat_every_minutes": 10, "curve": "smooth"})
        base = specs.parse_time(START)
        for cycle in range(3):
            self.assertAlmostEqual(specs.ramp(base + (cycle * 10 + 1.5) * 60000, w), .5)
            self.assertEqual(specs.ramp(base + (cycle * 10 + 8) * 60000, w), 0)

    def test_baseline_rate_moves_between_baseline_and_peak(self):
        s = specs.normalized(bell_spec())
        fault = Planner(s, random.Random(1)).by_id["network-android"]
        start = specs.parse_time(s["incidents"][0]["start"])
        self.assertAlmostEqual(fault.rate_at(start - 60000), .05)
        self.assertAlmostEqual(fault.rate_at(start + 60000), .05 + .45 * .5)
        self.assertAlmostEqual(fault.rate_at(start + 123000), .5)
        self.assertAlmostEqual(fault.rate_at(start + 30 * 60000), .05)

    def test_validation(self):
        good = bell_spec()
        self.assertEqual(specs.errors(specs.validate(good, NOW)), [])
        high = bell_spec()
        high["incidents"][0]["baseline_rate"] = .6
        self.assertIn("baseline_rate", " ".join(i["path"] for i in specs.errors(specs.validate(high, NOW))))
        wrong = bell_spec()
        wrong["incidents"][0]["curve"] = "gaussian"
        self.assertTrue(specs.errors(specs.validate(wrong, NOW)))
        text = " ".join(specs.explain(good))
        self.assertIn("smooth", text)
        self.assertIn("5% outside the incident", text)

    def test_exact_counts_follow_the_bell(self):
        random.seed(5)
        with tempfile.TemporaryDirectory() as state:
            r = SpecRun(bell_spec(), load_config(None), persist=False, now_ms=NOW, state_dir=state, rng=random.Random(5))
            r.run_batch(Collect(), quiet=True)
        row = next(x for x in r.emitted.summary()["faults"] if x["id"] == "network-android")
        self.assertTrue(row["within_tolerance"], row)
        fault = r.planner.by_id["network-android"]
        peak_at = specs.parse_time(bell_spec()["incidents"][0]["start"]) + 2 * 60000
        near, far = [0, 0], [0, 0]
        for o in r.emitted.observations:
            if o.get("verification_case") or o.get("platform", "android") != "android":
                continue
            bucket = near if abs((o.get("planned_start_ms") or o["start_ms"]) - peak_at) < 60000 else far
            bucket[0] += affected(fault, o)
            bucket[1] += 1
        self.assertGreater(near[1], 20)
        self.assertGreater(far[1], 20)
        # Sessions near the peak fail several times as often as those at the baseline.
        self.assertGreater(near[0] / near[1], 2.5 * far[0] / far[1])


if __name__ == "__main__":
    unittest.main()
