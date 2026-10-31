"""RED counts come from actual server spans and stale data never reaches transport."""

import copy
import random
import tempfile
import unittest
from unittest.mock import patch
from xcorgen import spec, otlp
from xcorgen.backend_metrics import REDMetrics, points, timely
from xcorgen.runner import Sender, load_config
from xcorgen.spec_runner import SpecRun

NOW = 1793376000000


class BackendMetrics(unittest.TestCase):
    def test_counts_histogram_and_cumulative_reset(self):
        random.seed(822)
        s = spec.default_spec()
        s["destinations"] = {"mode": "end_to_end", "backend_metrics": True}
        s["chronosphere"] = {"tenant": "acme-prod"}
        with tempfile.TemporaryDirectory() as directory:
            run = SpecRun(
                s, load_config(None), persist=False, state_dir=directory, now_ms=NOW
            )
            total = errors = 0
            for i in range(3):
                target, session, _, deliveries, faults = run.session(
                    NOW - 60000 + i * 1000
                )
                server = [
                    sp
                    for d in deliveries
                    if d["target"] == "otlp" and d["kind"] == "traces"
                    for rs in d["payload"]["resourceSpans"]
                    for ss in rs["scopeSpans"]
                    for sp in ss["spans"]
                    if sp["kind"] == 2
                ]
                total += len(server)
                errors += sum(sp.get("status", {}).get("code") == 2 for sp in server)
                run.record(target, session, faults, deliveries)
                payload = next(
                    d["payload"] for d in deliveries if d["kind"] == "metrics"
                )
                for rm in payload["resourceMetrics"]:
                    metrics = rm["scopeMetrics"][0]["metrics"]
                    histogram = metrics[2]["histogram"]["dataPoints"][0]
                    self.assertEqual(
                        sum(map(int, histogram["bucketCounts"])),
                        int(histogram["count"]),
                    )
                    self.assertEqual(
                        int(metrics[0]["sum"]["dataPoints"][0]["asInt"]),
                        int(histogram["count"]),
                    )
            self.assertEqual(
                sum(r["count"] for r in run.metrics.services.values()), total
            )
            self.assertEqual(
                sum(r["errors"] for r in run.metrics.services.values()), errors
            )
            self.assertTrue(otlp.encode_metrics(payload))
            self.assertEqual(REDMetrics().services, {})

    def test_limits_receipts_and_partial_success(self):
        p = {
            "resourceMetrics": [
                {
                    "resource": {},
                    "scopeMetrics": [
                        {
                            "scope": {},
                            "metrics": [
                                {
                                    "name": "http.server.request.count",
                                    "sum": {
                                        "dataPoints": [
                                            {"timeUnixNano": str(int(NOW * 1e6))}
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        self.assertTrue(timely(p, NOW + 120000))
        self.assertFalse(timely(p, NOW + 120001))
        self.assertTrue(timely(p, NOW + 7200000, "raw"))
        self.assertFalse(timely(p, NOW + 7200001, "raw"))
        self.assertFalse(timely(p, NOW - 60001))
        sender = Sender(load_config(None))
        try:
            with patch(
                "xcorgen.runner.time.time", return_value=(NOW + 120001) / 1000
            ), patch.object(sender, "_post_otlp") as post:
                receipt = sender._send(
                    {"target": "otlp", "kind": "metrics", "payload": p}
                )
                self.assertEqual(receipt["status"], "expired")
                post.assert_not_called()
        finally:
            sender.close()
        self.assertEqual(
            otlp.partial_success(b'{"partialSuccess":{"rejectedDataPoints":3}}', True),
            (3, ""),
        )

    def test_sessions_only_omits_metrics(self):
        random.seed(883)
        s = spec.default_spec()
        s["destinations"] = {"mode": "sessions_only", "backend_metrics": True}
        with tempfile.TemporaryDirectory() as directory:
            run = SpecRun(
                s, load_config(None), persist=False, state_dir=directory, now_ms=NOW
            )
            target, session, _, deliveries, faults = run.session(NOW)
            run.record(target, session, faults, deliveries)
            self.assertFalse(any(d["target"] == "otlp" for d in deliveries))
