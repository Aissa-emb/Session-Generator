"""SDK-shaped ANR samples, startup and UI load traces, and business child spans (Android 9.3.1, Apple 6.21.0)."""
import copy
import os
import random
import sys
import tempfile
import unittest
from collections import Counter

from xcorgen import anr_stacks, hang_stacks, spec
from xcorgen.runner import Generator, load_config
from xcorgen.spec_runner import SpecRun

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import check_output  # noqa: E402

# Package prefixes from go/lib/common/category_jvm.go for the categories the dashboard legend shows.
CATEGORIES = (("io.embrace.", "Embrace"), ("com.google.android.gms.ads.", "Ad SDKs"),
              ("com.google.android.gms.internal.ads.", "Ad SDKs"), ("com.squareup.moshi.", "Serialization"),
              ("kotlin.reflect.", "Reflection"), ("kotlinx.coroutines.", "Concurrency"), ("kotlin.coroutines.", "Concurrency"),
              ("java.util.concurrent.", "Concurrency"), ("java.lang.Object.wait", "Concurrency"),
              ("android.database.sqlite.", "IO"), ("android.app.SharedPreferencesImpl", "IO"), ("libcore.io.", "IO"),
              ("java.io.", "IO"), ("okio.", "IO"), ("android.graphics.ImageDecoder", "IO"), ("java.lang.ClassLoader", "IO"),
              ("com.android.internal.util.FastXmlSerializer.flush", "IO"), ("android.view.", "View"), ("android.widget.", "View"), ("android.webkit.", "View"), ("org.chromium.", "View"),
              ("androidx.recyclerview.", "View"), ("androidx.constraintlayout.", "View"), ("androidx.fragment.", "View"),
              ("androidx.compose.ui.layout.", "View"), ("com.google.firebase.", "SDK"), ("com.google.zxing.", "SDK"),
              ("android.", "Android"), ("androidx.", "Android"), ("com.android.", "Android"), ("dalvik.", "Android"),
              ("java.", "Java"), ("jdk.", "Java"), ("kotlin.", "Java"), ("com.vantra.", "Unknown"))


def category(frame):
    return next((c for prefix, c in CATEGORIES if frame.startswith(prefix)), "Unknown")


def attrs(record):
    return {a["key"]: a["value"] for a in record.get("attributes", [])}


def android(profile, primary=None, seed=14):
    random.seed(seed)
    cfg = load_config(None)
    cfg["embrace"]["apps"]["default"] = "dryrn"
    cfg["generation"] = {"primary_scenario": primary, "crash_family": None, "incident_phase": "incident"}
    gen = Generator([profile], cfg, incident=None, persist=False, backend_logs=False)
    for pool in gen.pools.values():
        pool.personas = []
    return gen.session(1793300400000)[0]


def envelopes(s):
    return [d for d in s.deliveries if d.get("fmt") == "envelope" and d["payload"]["type"] == "spans"]


def problems(docs):
    found = Counter()
    for d in docs:
        check_output.check_native_traces(d, lambda m: found.update([m]))
    return found


class AndroidBlockageSamples(unittest.TestCase):
    def test_sampling_follows_the_sdk_watchdog(self):
        rng = random.Random(4)
        frames, screen = anr_stacks.Frames("35", "Google", "com.vantra.android"), anr_stacks.Screen("banking", "MoveMoneyFragment", 1)
        for story in anr_stacks.STORY_WEIGHTS["banking"]:
            events = anr_stacks.blockage_samples(rng, story, screen, frames, 10_000, 19_000)
            times = [t for t, _ in events]
            self.assertGreater(times[0], 11_000)
            self.assertLessEqual(times[0], 11_100)
            self.assertTrue(all(b - a >= 100 for a, b in zip(times, times[1:])))
            self.assertLess(times[-1], 19_000)
            codes = [a["sample_code"] for _, a in events]
            self.assertEqual(codes, [0] * min(80, len(codes)) + [1] * max(0, len(codes) - 80))
            stacked = [a for _, a in events if "exception.stacktrace" in a]
            self.assertTrue(stacked)
            # Consecutive equal stacks are sent once (ThreadBlockageSampler dedupe).
            seen = [(a["jvm.thread.state"], a["exception.stacktrace"]) for a in stacked]
            self.assertTrue(all(x != y for x, y in zip(seen, seen[1:])))
            for a in stacked:
                lines = a["exception.stacktrace"].split("\n")
                self.assertEqual(a["frame_count"], len(lines))
                self.assertEqual(a["thread_priority"], 5)
                self.assertTrue(lines[-1].startswith("com.android.internal.os.ZygoteInit.main("))
                self.assertIn("android.os.Looper.loop", a["exception.stacktrace"])
                self.assertTrue(all(check_output.JAVA_FRAME.match(x) for x in lines), lines)

    def test_stories_spread_across_dashboard_categories(self):
        rng, seen, leaves = random.Random(9), Counter(), set()
        for lob in anr_stacks.STORY_WEIGHTS:
            for story in anr_stacks.STORY_WEIGHTS[lob]:
                f = anr_stacks.Frames(rng.choice(("34", "35", "36")), rng.choice(("samsung", "Google")), "com.vantra.android")
                events = anr_stacks.blockage_samples(rng, story, anr_stacks.Screen(lob, "HomeFragment", 2), f, 0, 8000)
                for _, a in events:
                    if "exception.stacktrace" in a:
                        lines = a["exception.stacktrace"].split("\n")
                        seen.update(category(x) for x in lines)
                        leaves.add(lines[0].split("(")[0])
        for c in ("Android", "Java", "IO", "Concurrency", "Serialization", "View", "Ad SDKs", "Embrace", "Unknown",
                  "SDK", "Reflection"):
            self.assertIn(c, seen)
        self.assertGreaterEqual(len(leaves), 25)

    def test_generated_blockages_vary_and_match_the_sdk_shape(self):
        stories, first_frames = Counter(), set()
        for seed in range(36):
            for profile in ("ecommerce", "banking", "travel"):
                s = android(profile, primary="blockage", seed=seed)
                docs = envelopes(s)
                self.assertEqual(problems(docs), Counter())
                block = next(x for d in docs for x in d["payload"]["data"]["spans"] if x["name"] == "emb-thread-blockage")
                self.assertNotIn("last_known_time_unix_nano", attrs(block))
                stories[s.blockage_evidence["story"]] += 1
                first = next(attrs(e)["exception.stacktrace"] for e in block["events"] if "exception.stacktrace" in attrs(e))
                first_frames.add(first.split("\n")[0].split("(")[0])
        self.assertGreaterEqual(len(stories), 10)
        self.assertGreaterEqual(len(first_frames), 10)

    def test_spec_family_and_story_names_are_honored(self):
        rng = random.Random(1)
        self.assertIn(anr_stacks.choose_story("migration", "travel", rng), anr_stacks.FAMILIES["migration"])
        self.assertEqual(anr_stacks.choose_story("preferences_commit", "banking", rng), "preferences_commit")
        self.assertNotIn(anr_stacks.choose_story("layout", "banking", rng), ("sponsored_webview_init", "boarding_pass_render"))


class AndroidTraces(unittest.TestCase):
    def test_startup_and_ui_load_traces_follow_the_emitters(self):
        kinds, found = Counter(), Counter()
        for seed in range(40):
            s = android(("ecommerce", "banking", "travel")[seed % 3], seed=seed)
            docs = envelopes(s)
            self.assertEqual(problems(docs), Counter())
            for d in docs:
                for x in d["payload"]["data"]["spans"]:
                    a = attrs(x)
                    if x["name"].startswith("emb-app-startup-"):
                        kinds[x["name"]] += 1
                        # Activity.getLocalClassName(): no application package prefix.
                        self.assertEqual(a["emb.startup_activity"], s._local_activity_name())
                        self.assertFalse(a["emb.startup_activity"].startswith(s.app["package"] + "."))
                        api = int(d["payload"]["resource"]["os_code"])
                        self.assertEqual("emb.startup_launch_reason" in a, api >= 35)
                    if x["name"].endswith("-time-to-initial-display"):
                        found[x["name"]] += 1
                        self.assertEqual(a["emb.type"], "perf.ui_load")
                    if x["name"] == "emb-embrace-init":
                        self.assertIn("modules-init-duration-ms", a)
                        self.assertIn("art-compiler-filter", a)
        self.assertTrue(kinds["emb-app-startup-cold"] and kinds["emb-app-startup-warm"])
        self.assertEqual(sum(kinds.values()), 40)

    def test_hot_ui_load_after_returning_from_background(self):
        s = android("travel", seed=3)
        # The app comes back after a short background period: a new part starts and the activity restarts.
        s.ended, s.t = False, s.t + 7000
        s._start_part("state")
        s.leave()
        act = s._local_activity_name()
        names = [x["name"] for x in envelopes(s)[-1]["payload"]["data"]["spans"]]
        self.assertIn(f"emb-{act}-hot-time-to-initial-display", names)
        self.assertIn(f"emb-{act}-resume", names)
        self.assertFalse(any(n.startswith("emb-app-startup-") for n in names))

    def test_business_spans_have_nested_local_work(self):
        s = android("banking", seed=5)
        spans = [x for d in envelopes(s) for x in d["payload"]["data"]["spans"]]
        roots = {x["span_id"]: x for x in spans if attrs(x).get("app.operation")}
        children = [x for x in spans if x["parent_span_id"] in roots]
        self.assertTrue(roots and children)
        for root in roots.values():
            kids = [x for x in children if x["parent_span_id"] == root["span_id"]]
            request = [x for x in kids if x["name"].startswith(("fetch_", "submit_"))]
            self.assertEqual(len(request), 1, root["name"])
            self.assertEqual(request[0]["status"], root["status"])
            self.assertTrue(all(attrs(k)["emb.type"] == "perf" for k in kids))


class AppleTraces(unittest.TestCase):
    def test_hang_startup_and_view_traces(self):
        random.seed(7)
        sc = spec.default_spec()
        sc["targets"] = [copy.deepcopy(spec.IOS_TARGET)]
        sc["population"]["platform_mix"] = {"vantra-ios": 1}
        sc["traffic"] = {"mode": "burst", "count": 40, "window": {"last_minutes": 6}}
        sc["destinations"] = {"mode": "end_to_end"}
        sc["chronosphere"] = {"tenant": "acme-prod"}
        sc["client_faults"] = [{"id": "hang", "type": "main_thread_blockage", "rate": .5, "platform": "ios"}]
        names, hangs = Counter(), 0
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(sc, load_config(None), persist=False, state_dir=state, now_ms=1793376000000)
            skeletons, allocation, _ = run.plan_batch()
            for sk, faults in zip(skeletons, allocation):
                _, _, _, deliveries, _ = run.session(sk["start_ms"], sk, faults)
                docs = [d for d in deliveries if d.get("fmt") == "envelope" and d["payload"]["type"] == "spans"]
                self.assertEqual(problems(docs), Counter())
                for d in docs:
                    for x in d["payload"]["data"]["spans"]:
                        names[x["name"]] += 1
                        if x["name"] == "emb-thread-blockage":
                            hangs += 1
                            self.assertEqual(len(x["events"]), 1)
                            frames = hang_stacks.decode(attrs(x["events"][0])["emb.stacktrace.ios"])
                            self.assertEqual(frames[-1]["s"], "start")
                            self.assertEqual(frames[-1]["m"], "dyld")
        self.assertTrue(hangs)
        self.assertTrue(names["emb-app-startup-cold"] + names["emb-app-startup-warm"])
        self.assertFalse(names["emb-app-startup"])
        self.assertTrue(any(n.endswith("-time-to-first-render") for n in names))


class WebTraces(unittest.TestCase):
    def test_xhr_names_and_business_children(self):
        xhr, children = Counter(), 0
        for seed in range(30):
            random.seed(seed)
            cfg = load_config(None)
            cfg["embrace"]["apps"]["default"] = "dryrn"
            gen = Generator(["banking"], cfg, persist=False, platform="web")
            for pool in gen.pools.values():
                pool.personas = []
            s = gen.session(1793300400000)[0]
            for d in s.deliveries:
                for rs in d["payload"].get("resourceSpans", []):
                    for ss in rs["scopeSpans"]:
                        ids = {x["spanId"]: x for x in ss["spans"]}
                        for x in ss["spans"]:
                            if ss["scope"]["name"] == "@opentelemetry/instrumentation-xml-http-request":
                                code = next((a["value"].get("intValue") for a in x["attributes"]
                                             if a["key"] == "http.response.status_code"), None)
                                xhr[(x["name"], x["events"][-1]["name"], code is not None and int(code) < 299)] += 1
                            parent = ids.get(x.get("parentSpanId"))
                            if parent and parent["traceId"] == x["traceId"] and ss["scope"]["name"] == "embrace-web-sdk-traces":
                                children += 1
                                self.assertLessEqual(int(parent["startTimeUnixNano"]), int(x["startTimeUnixNano"]))
                                self.assertLessEqual(int(x["endTimeUnixNano"]), int(parent["endTimeUnixNano"]))
        self.assertTrue(children)
        for (name, last, ok), _ in xhr.items():
            self.assertIn(name, ("GET", "POST"))
            self.assertEqual(last, "loaded" if ok else "error")


if __name__ == "__main__":
    unittest.main()
