"""Browser/device personas and the returning-visitor pool.

Each persona keeps a stable Embrace device ID (X-EM-DID) and user-session
counters across runs, so the same visitors come back over days the way real
traffic does. The pool is saved under .state/ next to the project.
"""

import json
import os
import random
import time
import tempfile

from .util import emb_id, weighted

CHROME, EDGE, FIREFOX, SAFARI, SAMSUNG = "154.0.0.0", "154.0.0.0", "157.0", "26.1", "29.0"

# (kind, ua, chromium, screens, weight)
DEVICES = [
    ("desktop", f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME} Safari/537.36",
     True, ["1920x1080", "1536x864", "2560x1440", "1366x768", "1440x900"], 22),
    ("desktop", f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME} Safari/537.36",
     True, ["1512x982", "1728x1117", "1440x900", "2560x1440"], 13),
    ("desktop", f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{SAFARI} Safari/605.1.15",
     False, ["1512x982", "1728x1117", "1470x956"], 8),
    ("desktop", f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{EDGE} Safari/537.36 Edg/{EDGE}",
     True, ["1920x1080", "1536x864", "1280x720"], 8),
    ("desktop", f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{FIREFOX}) Gecko/20100101 Firefox/{FIREFOX}",
     False, ["1920x1080", "1536x864"], 3),
    ("mobile", f"Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{SAFARI} Mobile/15E148 Safari/604.1",
     False, ["402x874", "393x852", "430x932", "440x956", "390x844"], 26),
    ("mobile", "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
     False, ["393x852", "390x844", "375x812"], 5),
    ("mobile", f"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME} Mobile Safari/537.36",
     True, ["412x915", "384x854", "360x800", "412x892"], 15),
    ("mobile", f"Mozilla/5.0 (Linux; Android 15; SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/{SAMSUNG} Chrome/136.0.0.0 Mobile Safari/537.36",
     True, ["384x832", "412x915"], 4),
    ("tablet", f"Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{SAFARI} Mobile/15E148 Safari/604.1",
     False, ["820x1180", "1024x1366", "834x1194"], 3),
]

# Android phones: (manufacturer, model as Build.MODEL reports it, OS versions, screen, SoC or None, storage GB, weight).
# The backend maps model codes such as SM-S931U to market names.
ANDROID_DEVICES = [
    ("samsung", "SM-S931U", ["16", "16", "15"], "1080x2340", "SM8750", 128, 10),
    ("samsung", "SM-S938U", ["16", "16", "15"], "1440x3120", "SM8750", 256, 6),
    ("samsung", "SM-S936U", ["16", "15"], "1080x2340", "SM8750", 256, 3),
    ("samsung", "SM-S921U", ["16", "15", "15"], "1080x2340", "SM8650", 128, 8),
    ("samsung", "SM-S928U", ["16", "15"], "1440x3120", "SM8650", 256, 5),
    ("samsung", "SM-S911U", ["15", "15", "14"], "1080x2340", "SM8550", 128, 5),
    ("samsung", "SM-A166U", ["15", "15", "16"], "1080x2340", None, 128, 7),
    ("samsung", "SM-A156U", ["15", "14", "16"], "1080x2340", None, 128, 7),
    ("samsung", "SM-A356U", ["15", "16"], "1080x2340", None, 128, 4),
    ("samsung", "SM-F966U", ["16"], "1968x2184", "SM8750", 256, 2),
    ("Google", "Pixel 9", ["16", "16", "15"], "1080x2424", "Tensor G4", 128, 7),
    ("Google", "Pixel 9 Pro", ["16", "16"], "1280x2856", "Tensor G4", 256, 4),
    ("Google", "Pixel 9a", ["16", "15"], "1080x2424", "Tensor G4", 128, 4),
    ("Google", "Pixel 10", ["16"], "1080x2424", "Tensor G5", 128, 5),
    ("Google", "Pixel 8", ["16", "15"], "1080x2400", "Tensor G3", 128, 4),
    ("Google", "Pixel 8a", ["16", "15"], "1080x2400", "Tensor G3", 128, 4),
    ("motorola", "moto g power 5G - 2024", ["15", "14"], "1080x2400", None, 128, 4),
    ("motorola", "moto g 5G - 2024", ["15", "14"], "720x1604", None, 128, 3),
    ("OnePlus", "CPH2583", ["15", "16"], "1440x3168", "SM8650", 256, 2),
]
API_LEVEL = {"16": "36", "15": "35", "14": "34"}

# Desktop/mobile split differs by vertical: retail is mobile-heavy, banking and travel lean desktop.
KIND_BIAS = {
    "ecommerce": {"desktop": 0.8, "mobile": 1.25, "tablet": 1.0},
    "banking": {"desktop": 1.3, "mobile": 0.8, "tablet": 0.8},
    "travel": {"desktop": 1.2, "mobile": 0.9, "tablet": 1.2},
}

# Browser chrome eats some screen height; mobile viewports lose the URL and tab bars.
CHROME_HEIGHT = {"desktop": (110, 160), "mobile": (150, 200), "tablet": (100, 140)}


def _new_persona(profile_key, rng):
    bias = KIND_BIAS.get(profile_key, {})
    kind, ua, chromium, screens, _ = weighted([(d, d[4] * bias.get(d[0], 1.0)) for d in DEVICES], rng)
    screen = rng.choice(screens)
    w, h = (int(x) for x in screen.split("x"))
    if kind == "desktop":
        vw = w - rng.choice([0, 0, 0, 15, 17])
        vh = h - rng.randint(*CHROME_HEIGHT[kind])
    else:
        vw, vh = w, h - rng.randint(*CHROME_HEIGHT[kind])
    conn = weighted([("fiber", 5), ("cable", 4), ("wifi", 6)] if kind == "desktop" else
                    [("wifi", 5), ("5g", 4), ("4g", 5), ("4g-weak", 1)], rng)
    rtt = {"fiber": (12, 30), "cable": (20, 45), "wifi": (25, 70), "5g": (35, 80), "4g": (55, 130),
           "4g-weak": (140, 320)}[conn]
    mbps = {"fiber": (150, 600), "cable": (60, 250), "wifi": (30, 180), "5g": (40, 220), "4g": (8, 45),
            "4g-weak": (1.2, 4)}[conn]
    return {
        "device_id": emb_id(rng),
        "kind": kind, "ua": ua, "chromium": chromium,
        "screen": screen, "viewport": f"{vw}x{vh}",
        "effective_type": "3g" if conn == "4g-weak" else "4g",
        "device_memory": rng.choice([4, 8, 8, 16]) if kind == "desktop" else rng.choice([4, 6, 8]),
        "rtt": round(rng.uniform(*rtt), 1), "mbps": round(rng.uniform(*mbps), 1),
        "ip": f"{rng.choice([24, 47, 66, 67, 68, 71, 73, 75, 76, 98, 99, 107, 108, 172, 174])}.{rng.randint(0, 255)}."
              f"{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "user_session_number": 0, "session_part_number": 0, "previous_user_session_id": "",
        "user_id": None, "traits": {},
    }


def _new_android_persona(profile_key, rng):
    maker, model, versions, screen, soc, gb, _ = weighted([(d, d[6]) for d in ANDROID_DEVICES], rng)
    os_version = rng.choice(versions)
    conn = weighted([("wifi", 6), ("5g", 4), ("4g", 4), ("4g-weak", 1)], rng)
    rtt = {"wifi": (25, 70), "5g": (35, 80), "4g": (55, 130), "4g-weak": (140, 320)}[conn]
    mbps = {"wifi": (30, 180), "5g": (40, 220), "4g": (8, 45), "4g-weak": (1.2, 4)}[conn]
    return {
        "device_id": emb_id(rng), "kind": "mobile", "platform": "android",
        "manufacturer": maker, "model": model, "os_version": os_version, "os_code": API_LEVEL[os_version],
        "screen": screen, "soc": soc, "disk_total": int(gb * 1e9 * rng.uniform(0.86, 0.9)),
        "timezone": weighted([("America/New_York", 5), ("America/Chicago", 3), ("America/Los_Angeles", 4),
                              ("America/Denver", 1), ("America/Phoenix", 1)], rng),
        "network": "wan" if conn != "wifi" else "wifi",
        "rtt": round(rng.uniform(*rtt), 1), "mbps": round(rng.uniform(*mbps), 1),
        "ip": f"{rng.choice([24, 47, 66, 67, 68, 71, 73, 75, 76, 98, 99, 107, 108, 172, 174])}.{rng.randint(0, 255)}."
              f"{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "user_session_number": 0, "session_part_number": 0, "previous_user_session_id": "",
        "process_starts": 0, "installed_version": None, "user_id": None, "traits": {},
    }


class PersonaPool:
    def __init__(self, profile_key, state_dir, size=600, rng=random, platform="web"):
        self.profile_key, self.rng, self.platform = profile_key, rng, platform
        self.path = os.path.join(state_dir, f"personas-{profile_key}{'' if platform == 'web' else '-' + platform}.json")
        self.size = size
        self.personas = []
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.personas = json.load(f)

    def pick(self, make_user):
        """Return a persona: mostly returning visitors once the pool has filled up.

        A visitor who had a session in the last 30 minutes is not picked again, so one device
        never shows several overlapping or back-to-back sessions.
        """
        now = time.time()
        fresh = [p for p in self.personas if now - p.get("last_seen", 0) > 1800]
        # A young pool is mostly first-time visitors; it settles at ~55% new once it has grown.
        p_new = max(0.55, 1 - len(self.personas) / 150)
        if len(self.personas) < self.size and (not fresh or self.rng.random() < p_new):
            new = _new_android_persona if self.platform == "android" else _new_persona
            p = new(self.profile_key, self.rng)
            make_user(p)
            self.personas.append(p)
        else:
            # Skew towards a core of frequent visitors.
            pool = fresh or self.personas
            p = pool[min(int(len(pool) * (self.rng.random() ** 1.3)), len(pool) - 1)]
        p["last_seen"] = now
        return p

    def save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".personas-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.personas, f)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
