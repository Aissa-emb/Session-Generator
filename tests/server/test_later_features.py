"""Owner gates, idempotent concurrent starts and local source-map upload protocol."""

import copy
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import tempfile
import unittest
from unittest.mock import patch
from server.service import Service
from server.store import Store
from server import symbols
from xcorgen import spec


class LaterFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.engine.dispose)
        self.svc = Service(self.store, launch_jobs=False)

    def test_ios_dry_preview_and_live_setup_gate(self):
        s = spec.default_spec()
        s["targets"] = [copy.deepcopy(spec.IOS_TARGET)]
        s["population"]["platform_mix"] = {"vantra-ios": 1}
        s["traffic"] = {"mode": "burst", "count": 3, "window": {"last_minutes": 6}}
        saved = self.svc.create_spec(s)
        self.assertIn("ios", self.svc.preview(s)["timelines"])
        with self.assertRaisesRegex(ValueError, "Create the iOS app"):
            self.svc.start_run(saved["id"], confirm=True)
        self.assertTrue(
            self.svc.start_run(saved["id"], confirm=True, dry_run=True)["dry_run"]
        )

    def test_symbols_are_reviewed_confirmed_and_required_before_live(self):
        s = spec.default_spec()
        s["targets"] = [copy.deepcopy(spec.WEB_TARGET)]
        s["population"]["platform_mix"] = {"vantra-web": 1}
        s["targets"][0]["web"]["symbolication"] = True
        s["traffic"] = {"mode": "burst", "count": 2, "window": {"last_minutes": 6}}
        saved = self.svc.create_spec(s)
        preview = symbols.plan(saved["spec"])
        self.assertGreater(len(preview["assets"]), 20)
        with self.assertRaisesRegex(ValueError, "Upload"):
            self.svc.start_run(saved["id"], confirm=True)
        with self.assertRaisesRegex(ValueError, "confirm"):
            symbols.upload(self.svc, saved["id"])
        uploads = []

        def transport(url, body, headers):
            self.assertEqual(url, "https://dsym-store.emb-api.com/v2/store/sourcemap")
            self.assertNotIn("fixture-symbol-value", json.dumps(headers))
            boundary = headers["Content-Type"].split("boundary=")[1].encode()
            segment = next(
                p for p in body.split(b"--" + boundary) if b'filename="blob"' in p
            )
            payload = json.loads(gzip.decompress(segment.split(b"\r\n\r\n", 1)[1][:-2]))
            self.assertEqual(json.loads(payload["sourcemap"])["version"], 3)
            uploads.append(url)
            return b""

        with patch("server.symbols.secrets.get", return_value="fixture-symbol-value"):
            result = symbols.upload(self.svc, saved["id"], True, "user", transport)
            again = symbols.upload(self.svc, saved["id"], True, "user", transport)
        self.assertEqual(len(uploads), result["uploaded"])
        self.assertEqual(result["uploaded"], again["uploaded"])
        self.assertNotIn(
            "fixture-symbol-value", json.dumps(self.store.list("settings"))
        )
        symbols.ready(self.store, saved["spec"])
        # A new release has different assets and cannot silently reuse old acknowledgements.
        changed = copy.deepcopy(saved["spec"])
        changed["targets"][0]["web"]["releases"] = [
            {"version": "2026.11.01.1", "share": 1}
        ]
        with self.assertRaises(ValueError):
            symbols.ready(self.store, changed)

    def test_concurrent_confirmed_starts_reuse_one_run_and_stream(self):
        s = spec.default_spec()
        s["traffic"] = {"mode": "live", "rate_per_minute": 1}
        saved = self.svc.create_spec(s)
        with patch.object(self.svc, "preview", return_value={"linter": []}):
            with ThreadPoolExecutor(max_workers=4) as pool:
                runs = list(
                    pool.map(
                        lambda _: self.svc.start_run(
                            saved["id"],
                            confirm=True,
                            dry_run=True,
                            idempotency_key="same",
                        ),
                        range(4),
                    )
                )
            self.assertEqual(len({r["id"] for r in runs}), 1)
            with ThreadPoolExecutor(max_workers=4) as pool:
                streams = list(
                    pool.map(
                        lambda _: self.svc.create_stream(
                            saved["id"],
                            "Regional traffic",
                            True,
                            True,
                            idempotency_key="one",
                        ),
                        range(4),
                    )
                )
            self.assertEqual(len({r["id"] for r in streams}), 1)
            self.assertEqual(len(self.store.list("runs")), 2)
