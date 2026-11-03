"""Apple SDK contracts, session accounting and deterministic fault emission."""

import copy
import json
import random
import tempfile
import unittest
from xcorgen import spec
from xcorgen.ios_contract import check
from xcorgen.linter import lint_payloads
from xcorgen.runner import Sender, load_config
from xcorgen.spec_runner import SpecRun, expected_trace_ids
from xcorgen.emitted import from_dry_run
from xcorgen.android_session import shift_envelope

NOW = 1793376000000


def scenario(count=60):
    s = spec.default_spec()
    s["targets"] = [copy.deepcopy(spec.IOS_TARGET)]
    s["population"]["platform_mix"] = {"vantra-ios": 1}
    s["traffic"] = {"mode": "burst", "count": count, "window": {"last_minutes": 6}}
    s["destinations"] = {"mode": "end_to_end"}
    s["chronosphere"] = {"tenant": "acme-prod"}
    return s


class AppleContracts(unittest.TestCase):
    def test_faults_and_source_shapes(self):
        random.seed(7331)
        s = scenario()
        s["client_faults"] = [
            {"id": kind, "type": kind, "rate": 0.2, "platform": "ios"}
            for kind in (
                "ios_crash",
                "slow_startup",
                "main_thread_blockage",
                "network_failure",
            )
        ]
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(
                s, load_config(None), persist=False, state_dir=state, now_ms=NOW
            )
            skeletons, allocation, _ = run.plan_batch()
            for sk, faults in zip(skeletons, allocation):
                target, session, _, deliveries, _ = run.session(
                    sk["start_ms"], sk, faults
                )
                expected = expected_trace_ids(session, deliveries)
                for request in session.plan.evidence:
                    self.assertEqual(
                        request["trace_id"] in expected,
                        bool(request["first_backend_span_id"]),
                    )
                run.record(target, session, faults, deliveries)
                self.assertEqual(lint_payloads(deliveries), [])
                for d in deliveries:
                    if d["target"] != "embrace":
                        continue
                    problems = []
                    check(d, problems.append)
                    self.assertEqual(problems, [])
                    text = json.dumps(d["payload"])
                    for forbidden in (
                        "retrofit2",
                        "java.lang",
                        "emb.forward_telemetry",
                        "emb.user_session_id",
                    ):
                        self.assertNotIn(forbidden, text)
                    before = d["payload"]["resource"]["process_start_time"]
                    shift_envelope(d["payload"], -1000000)
                    self.assertEqual(
                        d["payload"]["resource"]["process_start_time"], before - 1000000
                    )
            report = run.emitted.summary()
            self.assertGreaterEqual(report["sdk_sessions"], report["sessions"])
            for row in report["faults"]:
                self.assertTrue(row["within_tolerance"], row)
                self.assertEqual(row["emitted_sessions"], 12, row)

    def test_saved_report_preserves_journeys_and_native_sessions(self):
        random.seed(81)
        s = scenario(10)
        with tempfile.TemporaryDirectory() as directory:
            run = SpecRun(
                s, load_config(None), persist=False, state_dir=directory, now_ms=NOW
            )
            run.open_manifest(directory, dry=True)
            sender = Sender(load_config(None), directory)
            run.run_batch(sender, quiet=True)
            sender.close()
            rebuilt = from_dry_run(directory, s)
            self.assertEqual(rebuilt.summary()["sessions"], 10)
            self.assertEqual(
                rebuilt.summary()["sdk_sessions"], run.emitted.summary()["sdk_sessions"]
            )
            self.assertEqual({o["platform"] for o in rebuilt.observations}, {"ios"})
            run.journal.close()

    def test_history_has_no_trace_context(self):
        random.seed(51)
        s = scenario(3)
        s["traffic"]["window"] = {"last_minutes": 60}
        s["destinations"] = {"mode": "sessions_only"}
        with tempfile.TemporaryDirectory() as directory:
            run = SpecRun(
                s, load_config(None), persist=False, state_dir=directory, now_ms=NOW
            )
            target, session, _, deliveries, _ = run.session(NOW - 3500000)
            self.assertTrue(all(d["target"] == "embrace" for d in deliveries))
            self.assertNotIn("emb.w3c_traceparent", json.dumps(deliveries))
