"""Real release assets and independent regional allocations without external IO."""

import copy
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from xcorgen import regions, spec, web_symbols
from xcorgen.web_faults import exception, FAMILIES
from xcorgen.linter import lint_payloads
from xcorgen.spec_runner import SpecRun
from xcorgen.runner import load_config
from tools.regions import apply


def resolve(sm, target_line, target_column):
    """Independent source-map v3 decoder; generated columns reset on each line."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    source = original_line = column = name = 0
    chosen = None
    for line, segments in enumerate(sm["mappings"].split(";"), 1):
        generated = 0
        for segment in segments.split(","):
            if not segment:
                continue
            values, value, shift = [], 0, 0
            for char in segment:
                digit = alphabet.index(char)
                value |= (digit & 31) << shift
                if digit & 32:
                    shift += 5
                else:
                    values.append(-(value >> 1) if value & 1 else value >> 1)
                    value = shift = 0
            generated += values[0]
            if len(values) >= 4:
                source += values[1]
                original_line += values[2]
                column += values[3]
                if len(values) == 5:
                    name += values[4]
            if line == target_line and generated <= target_column:
                chosen = None if len(values) == 1 else dict(source=sm["sources"][source], line=original_line+1, column=column, name=sm["names"][name] if len(values)==5 else None)
        if line == target_line:
            return chosen
    return None


class SymbolsAndRegions(unittest.TestCase):
    def test_real_assets_ids_and_mapped_frames(self):
        target = spec.normalized(dict(spec.default_spec(), targets=[spec.WEB_TARGET]))[
            "targets"
        ][0]
        assets = web_symbols.assets(target)
        self.assertGreater(len(assets), 20)
        for asset in assets:
            sm = json.loads(asset["sourcemap"])
            self.assertEqual(sm["debugId"].replace("-", ""), asset["bundle_id"])
            code = asset["bundle"].splitlines()
            original = (
                "\n".join(
                    line
                    for line in code
                    if line != "// Injected by Embrace Web CLI:"
                    and not line.startswith(";(E=>")
                )
                + "\n"
            )
            self.assertEqual(
                hashlib.md5(original.encode(), usedforsecurity=False).hexdigest(),
                asset["bundle_id"],
            )
            compiled = web_symbols.application(asset["url"])
            self.assertTrue(asset["bundle"].startswith(compiled["code"] + "\n"))
            for source, content in zip(sm["sources"], sm["sourcesContent"]):
                self.assertEqual(content, (Path(__file__).parents[1] / "reference/web-exceptions" / source).read_text())
            # Validate every captured position, including call sites, against a
            # separate VLQ decoder rather than the compiler's mapping library.
            for engine in compiled["engines"].values():
                for sample in engine["exceptions"].values():
                    for frame in sample["frames"]:
                        if frame.get("native"):
                            continue
                        self.assertLessEqual(frame["column"], len(code[frame["line"] - 1]))
                        self.assertEqual(resolve(sm, frame["line"], frame["column"] - 1), frame["original"])
            top = compiled["engines"]["chromium"]["exceptions"]["property_undefined"]["frames"][0]
            self.assertEqual(top["name"], "readPrice")
            source_index = sm["sources"].index(top["original"]["source"])
            self.assertIn("return record.price.amount", sm["sourcesContent"][source_index].splitlines()[top["original"]["line"] - 1])
        current = assets[0]
        ids = json.loads(
            web_symbols.bundle_ids([current["url"]], current["release"], "chromium")
        )
        self.assertEqual(set(ids.values()), {current["bundle_id"]})
        self.assertTrue(all(k.startswith("Error\n    at https://") for k in ids))

    def test_symbolicated_payloads_are_realistic(self):
        random.seed(634)
        s = spec.default_spec()
        s["targets"] = [copy.deepcopy(spec.WEB_TARGET)]
        s["targets"][0]["web"]["symbolication"] = True
        s["population"]["platform_mix"] = {"vantra-web": 1}
        s["client_faults"] = [
            {"id": "checkout", "type": "js_exception", "platform": "web", "rate": 1}
        ]
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(s, load_config(None), persist=False, state_dir=state)
            for _ in range(4):
                _, _, _, deliveries, _ = run.session(run.now_ms)
                self.assertEqual(lint_payloads(deliveries), [])
                self.assertIn(
                    "Embrace",
                    json.dumps(
                        web_symbols.bundle(
                            "https://static.vantra.com/assets/shop/checkout.js",
                            "10.28.0",
                        )
                    ),
                )

    def test_regional_rates_seeds_and_retry(self):
        s = spec.default_spec()
        s["traffic"] = {"mode": "live", "rate_per_minute": 12}
        connections = [
            {
                "region": r,
                "url": "https://" + r + ".vantra.com",
                "token_env": "SG_REGION_" + str(i),
                "weight": w,
            }
            for i, (r, w) in enumerate([("us-east", 2), ("eu-west", 1)])
        ]
        p = regions.plan(s, connections)
        self.assertEqual(
            [r["spec"]["traffic"]["rate_per_minute"] for r in p["runners"]], [8, 4]
        )
        self.assertEqual(len({r["spec"]["seed"] for r in p["runners"]}), 2)
        self.assertEqual(p, regions.plan(s, connections))
        s["traffic"] = {"mode": "burst", "count": 11, "window": {"last_minutes": 6}}
        self.assertEqual(
            sum(
                r["spec"]["traffic"]["count"]
                for r in regions.plan(s, connections)["runners"]
            ),
            11,
        )
        requests = []

        def transport(url, body, headers):
            requests.append((url, json.loads(body)))
            return json.dumps({"id": str(len(requests))}).encode()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SG_REGION_0": "fixture-value", "SG_REGION_1": "fixture-value"}
        ):
            path = Path(directory) / "receipts.json"
            with self.assertRaises(ValueError):
                apply(p, path, transport=transport)
            first = apply(p, path, True, transport=transport)
            self.assertEqual(first, apply(p, path, True, transport=transport))
            self.assertEqual(len(requests), 4)
            self.assertTrue(
                all(
                    body["dry_run"]
                    for url, body in requests
                    if url.endswith("/streams")
                )
            )
            self.assertNotIn("fixture-value", path.read_text())
            with self.assertRaises(ValueError):
                apply(p, path, True, False, transport)
        with self.assertRaises(ValueError):
            regions.plan(s, [connections[0], connections[0]])
