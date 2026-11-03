"""Live and bounded sends obey the same time limits even when stop flushes early."""

import unittest
from xcorgen.runner import fresh_delivery

NOW = 1793376000000


class SendTime(unittest.TestCase):
    def test_trace_expiry_and_future_bound(self):
        for offset, valid in ((-479000, True), (-481000, False), (481000, False)):
            d = {
                "target": "otlp",
                "kind": "traces",
                "payload": {
                    "resourceSpans": [
                        {
                            "scopeSpans": [
                                {
                                    "spans": [
                                        {
                                            "startTimeUnixNano": str(
                                                (NOW + offset) * 1000000
                                            )
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }
            self.assertEqual(fresh_delivery(d, NOW) is not None, valid)

    def test_app_context_is_stripped_without_mutating_frozen_payload(self):
        spans = [
            {
                "start_time_unix_nano": (NOW - 600000) * 1000000,
                "attributes": [
                    {"key": "emb.w3c_traceparent", "value": "context"},
                    {"key": "emb.forward_telemetry", "value": "true"},
                ],
            },
            {
                "start_time_unix_nano": (NOW - 10000) * 1000000,
                "attributes": [{"key": "emb.w3c_traceparent", "value": "recent"}],
            },
        ]
        d = {
            "target": "embrace",
            "kind": "spans",
            "fmt": "envelope",
            "payload": {"data": {"spans": spans}},
        }
        fresh = fresh_delivery(d, NOW)
        self.assertEqual(len(fresh["payload"]["data"]["spans"][0]["attributes"]), 0)
        self.assertEqual(len(fresh["payload"]["data"]["spans"][1]["attributes"]), 1)
        self.assertEqual(len(spans[0]["attributes"]), 2)
        self.assertIsNone(fresh_delivery(d, NOW + 25 * 3600000))
