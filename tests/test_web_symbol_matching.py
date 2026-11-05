"""Generated web exceptions must be resolvable by XCOR RUM's ingest parser.

The loader assigns a bundle ID to a frame only when the frame's file URL equals a file URL
parsed out of an `emb.js_file_bundle_ids` key (go/lib/codec/js.go: ParseJSStackTraceString,
parseFileBundleIDsMap, jsWebLineMatcher and the Gecko/WebKit regex matcher). The symbol
service then looks up the uploaded map by that bundle ID. A frame without a matching ID is
stored as compiled `file:line:col` with no source context, which is what an empty `{}` map
produces in the dashboard.
"""

import json
import random
import re
import tempfile
import unittest
from xcorgen import spec, web_symbols
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun

FILE_LINE_COL = re.compile(r"^(.*):(\d+):(\d+)$")
GECKO = re.compile(r"([a-zA-Z0-9_$]*)@((https?:[^\0:]+)|[^\0:]+):?(\d*):?(\d*)")


def _web(line):
    if not line.startswith("at "):
        return None
    line = line[3:].removeprefix("async ")
    method = ""
    if " (" in line and line.endswith(")"):
        method, _, line = line[:-1].rpartition(" (")
    m = FILE_LINE_COL.match(line)
    return (method, m[1], int(m[2]), int(m[3])) if m else (method, line, 0, 0)


def _gecko(line):
    m = GECKO.search(line)
    return (m[1], m[2], int(m[4] or 0), int(m[5] or 0)) if m else None


def parse(stack):
    """Frames as (method, file, line, column); the first matching parser is used for all lines."""
    frames, matcher = [], None
    for raw in stack.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if matcher is None:
            for candidate in (_web, _gecko):
                if candidate(line) is not None:
                    matcher = candidate
                    break
            else:
                continue
        frame = matcher(line)
        if frame is not None:
            frames.append((frame[0].strip(), frame[1].strip(), frame[2], frame[3]))
    return frames


def file_bundle_ids(raw):
    return {f[1]: v for k, v in raw.items() for f in parse(k) if f[1]}


class WebSymbolMatching(unittest.TestCase):
    def test_parser_matches_backend_examples(self):
        # Examples from the jsWebLineMatcher comment and a WebKit/Gecko frame.
        self.assertEqual(
            parse("Error\n    at t.<computed> (https://www.example.com/:118:5337)\n"
                  "    at async https://dash.embrace.io/assets/components-Dp4oax7B.js:13:49873"),
            [("t.<computed>", "https://www.example.com/", 118, 5337),
             ("", "https://dash.embrace.io/assets/components-Dp4oax7B.js", 13, 49873)],
        )
        self.assertEqual(
            parse("readPrice@https://static.vantra.com/assets/shop/home-1.js:1:956"),
            [("readPrice", "https://static.vantra.com/assets/shop/home-1.js", 1, 956)],
        )

    def test_every_first_party_frame_resolves_to_an_uploaded_map(self):
        random.seed(5127)
        s = spec.default_spec()
        s["targets"] = [dict(spec.WEB_TARGET, web=dict(spec.WEB_TARGET["web"], symbolication=True))]
        s["population"]["platform_mix"] = {"vantra-web": 1}
        s["client_faults"] = [{"id": "js", "type": "js_exception", "platform": "web", "rate": 1}]
        target = spec.normalized(s)["targets"][0]
        uploaded = {a["bundle_id"]: a for a in web_symbols.assets(target)}
        frames = 0
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(s, load_config(None), persist=False, state_dir=state)
            for _ in range(24):
                for delivery in run.session(run.now_ms)[3]:
                    if delivery["kind"] != "logs":
                        continue
                    for rl in delivery["payload"]["resourceLogs"]:
                        for sl in rl["scopeLogs"]:
                            for record in sl["logRecords"]:
                                a = {x["key"]: next(iter(x["value"].values())) for x in record["attributes"]}
                                if a.get("emb.type") != "sys.exception":
                                    continue
                                ids = json.loads(a["emb.js_file_bundle_ids"])
                                self.assertTrue(ids, "symbolicated sessions must send bundle IDs")
                                by_file = file_bundle_ids(ids)
                                for method, file, line, column in parse(a["exception.stacktrace"]):
                                    if not file.startswith(target["web"]["cdn"]):
                                        continue
                                    frames += 1
                                    bid = by_file.get(file)
                                    self.assertIn(bid, uploaded, f"{method} in {file} has no uploaded map")
                                    self.assertEqual(uploaded[bid]["url"], file)
                                    code = uploaded[bid]["bundle"].split("\n")
                                    self.assertLessEqual(column, len(code[line - 1]) + 1)
        self.assertGreater(frames, 20)


if __name__ == "__main__":
    unittest.main()
