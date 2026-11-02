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
from functools import lru_cache

from .util import emb_id, weighted

# Browsers in the web population. family/version/engine drive the capability matrix
# (web_capabilities.py); chromium is the Chromium version a Chromium-based browser ships.
# Since iOS 26, Safari's user agent keeps "iPhone OS 18_6" and reports the Safari version only.
_WIN, _MAC = "Windows NT 10.0; Win64; x64", "Macintosh; Intel Mac OS X 10_15_7"
BROWSERS = [
    # family, version, engine, chromium, os, device type, user agent, screens, weight
    ("chrome", "154", "chromium", 154, "windows", "desktop",
     f"Mozilla/5.0 ({_WIN}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
     ["1920x1080", "1536x864", "2560x1440", "1366x768", "1440x900"], 19),
    ("chrome", "153", "chromium", 153, "windows", "desktop",
     f"Mozilla/5.0 ({_WIN}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
     ["1920x1080", "1536x864", "1366x768"], 4),
    ("chrome", "154", "chromium", 154, "macos", "desktop",
     f"Mozilla/5.0 ({_MAC}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
     ["1512x982", "1728x1117", "1440x900", "2560x1440"], 12),
    ("edge", "154", "chromium", 154, "windows", "desktop",
     f"Mozilla/5.0 ({_WIN}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
     ["1920x1080", "1536x864", "1280x720"], 8),
    ("firefox", "157", "gecko", None, "windows", "desktop",
     f"Mozilla/5.0 ({_WIN}; rv:157.0) Gecko/20100101 Firefox/157.0", ["1920x1080", "1536x864"], 3),
    ("firefox", "157", "gecko", None, "macos", "desktop",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:157.0) Gecko/20100101 Firefox/157.0", ["1512x982", "1440x900"], 1),
    ("safari", "27.0", "webkit", None, "macos", "desktop",
     f"Mozilla/5.0 ({_MAC}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15",
     ["1512x982", "1728x1117", "1470x956"], 6),
    ("safari", "26.5", "webkit", None, "macos", "desktop",
     f"Mozilla/5.0 ({_MAC}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15",
     ["1512x982", "1440x900"], 2),
    ("safari", "27.0", "webkit", None, "ios", "mobile",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Mobile/15E148 Safari/604.1",
     ["402x874", "393x852", "430x932", "440x956"], 18),
    ("safari", "26.5", "webkit", None, "ios", "mobile",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Mobile/15E148 Safari/604.1",
     ["393x852", "390x844", "430x932"], 6),
    ("safari", "26.1", "webkit", None, "ios", "mobile",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.1 Mobile/15E148 Safari/604.1",
     ["390x844", "375x812"], 3),
    ("chrome", "154", "chromium", 154, "android", "mobile",
     "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36",
     ["412x915", "384x854", "360x800", "412x892"], 14),
    ("samsung_internet", "29.0", "chromium", 136, "android", "mobile",
     "Mozilla/5.0 (Linux; Android 15; SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/29.0 Chrome/136.0.0.0 Mobile Safari/537.36",
     ["384x832", "412x915"], 4),
    ("safari", "27.0", "webkit", None, "ipados", "tablet",
     "Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Mobile/15E148 Safari/604.1",
     ["820x1180", "1024x1366", "834x1194"], 3),
]
# V1 tuple view used by older code paths: (kind, ua, chromium, screens, weight).
DEVICES = [(b[5], b[6], b[2] == "chromium", b[7], b[8]) for b in BROWSERS]

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
# Hardware class by Build.MODEL, used by spec filters (device_tiers) and startup timing.
DEVICE_TIERS = {
    "SM-S931U": "flagship", "SM-S938U": "flagship", "SM-S936U": "flagship", "SM-S921U": "flagship",
    "SM-S928U": "flagship", "SM-F966U": "flagship", "Pixel 9 Pro": "flagship", "Pixel 10": "flagship",
    "CPH2583": "flagship", "SM-S911U": "mid", "SM-A356U": "mid", "Pixel 9": "mid", "Pixel 9a": "mid",
    "Pixel 8": "mid", "Pixel 8a": "mid", "SM-A166U": "budget", "SM-A156U": "budget",
    "moto g power 5G - 2024": "budget", "moto g 5G - 2024": "budget",
}

# Desktop/mobile split differs by vertical: retail is mobile-heavy, banking and travel lean desktop.
KIND_BIAS = {
    "ecommerce": {"desktop": 0.8, "mobile": 1.25, "tablet": 1.0},
    "banking": {"desktop": 1.3, "mobile": 0.8, "tablet": 0.8},
    "travel": {"desktop": 1.2, "mobile": 0.9, "tablet": 1.2},
}

# Browser chrome eats some screen height; mobile viewports lose the URL and tab bars.
CHROME_HEIGHT = {"desktop": (110, 160), "mobile": (150, 200), "tablet": (100, 140)}


@lru_cache(maxsize=128)
def browser_weights(profile_key, families=(), types=()):
    """Fit requested marginal shares while retaining supported browser/device pairs."""
    margins = [(0, dict(families)), (5, dict(types))]
    weights = [b[8] * KIND_BIAS.get(profile_key, {}).get(b[5], 1) for b in BROWSERS]
    for dim, mix in margins:
        if mix:
            if set(mix) - {b[dim] for b in BROWSERS} or not sum(mix.values()):
                raise ValueError("Browser/device mix contains unsupported values or no positive weight.")
            total = sum(mix.values())
            mix.update({k: v / total for k, v in mix.items()})
            weights = [w if mix.get(b[dim], 0) else 0 for b, w in zip(BROWSERS, weights)]
    for _ in range(2000):
        for dim, mix in margins:
            for value, share in mix.items():
                indices = [i for i, b in enumerate(BROWSERS) if b[dim] == value]
                mass = sum(weights[i] for i in indices)
                if share and not mass:
                    raise ValueError("The requested browser and device shares have no compatible population.")
                for i in indices:
                    weights[i] *= share / mass if mass else 0
        if all(abs(sum(w for b, w in zip(BROWSERS, weights) if b[dim] == k) - v) < 1e-6
               for dim, mix in margins for k, v in mix.items()):
            return tuple(weights)
    raise ValueError("The requested browser and device percentages cannot be combined; adjust their shares.")


def _new_persona(profile_key, rng, overrides=None):
    overrides = overrides or {}
    weights = browser_weights(profile_key, tuple(sorted((overrides.get("browser_mix") or {}).items())),
                              tuple(sorted((overrides.get("device_type_mix") or {}).items())))
    browser = weighted(list(zip(BROWSERS, weights)), rng)
    family, version, engine, chromium_version, os_name, kind, ua, screens, _ = browser
    chromium = engine == "chromium"
    screen = rng.choice(screens)
    w, h = (int(x) for x in screen.split("x"))
    if kind == "desktop":
        vw = w - rng.choice([0, 0, 0, 15, 17])
        vh = h - rng.randint(*CHROME_HEIGHT[kind])
    else:
        vw, vh = w, h - rng.randint(*CHROME_HEIGHT[kind])
    conn_mix = overrides.get("connection_mix") or {}
    options = [("fiber", 5), ("cable", 4), ("wifi", 6)] if kind == "desktop" else [("wifi", 5), ("5g", 4), ("4g", 5), ("4g-weak", 1)]
    if conn_mix:
        from .population import NETWORKS
        conn = weighted(list(conn_mix.items()), rng)
        speed, latency = NETWORKS[conn]
        options = [(conn, 1)]
        rtt_override, mbps_override = (latency * .8, latency * 1.2), (speed * .8, speed * 1.2)
    else:
        rtt_override = mbps_override = None
    conn = weighted(options, rng)
    rtt = rtt_override or {"fiber": (12, 30), "cable": (20, 45), "wifi": (25, 70), "5g": (35, 80), "4g": (55, 130),
                          "4g-weak": (140, 320)}[conn]
    mbps = mbps_override or {"fiber": (150, 600), "cable": (60, 250), "wifi": (30, 180), "5g": (40, 220), "4g": (8, 45),
                            "4g-weak": (1.2, 4)}[conn]
    return {
        "device_id": emb_id(rng),
        "kind": kind, "ua": ua, "chromium": chromium, "browser": family, "browser_version": version,
        "engine": engine, "chromium_version": chromium_version, "os": os_name, "connection": conn,
        "screen": screen, "viewport": f"{vw}x{vh}",
        "effective_type": "2g" if conn == "slow-3g" else "3g" if conn in ("4g-weak", "fast-3g") else "4g",
        "device_memory": rng.choice([4, 8, 8, 16]) if kind == "desktop" else rng.choice([4, 6, 8]),
        "rtt": round(rng.uniform(*rtt), 1), "mbps": round(rng.uniform(*mbps), 1),
        "ip": f"{rng.choice([24, 47, 66, 67, 68, 71, 73, 75, 76, 98, 99, 107, 108, 172, 174])}.{rng.randint(0, 255)}."
              f"{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "user_session_number": 0, "session_part_number": 0, "previous_user_session_id": "",
        "user_id": None, "traits": {},
    }


def _new_android_persona(profile_key, rng, overrides=None):
    overrides = overrides or {}
    device_weights = overrides.get("device_weights") or {}
    maker, model, versions, screen, soc, gb, _ = weighted(
        [(d, device_weights.get(d[1], d[6])) for d in ANDROID_DEVICES], rng)
    os_mix = overrides.get("os_version_mix") or {}
    if os_mix and any(os_mix.get(v, 0) > 0 for v in versions):
        os_version = weighted([(v, os_mix.get(v, 0)) for v in sorted(set(versions))], rng)
    else:
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
    def __init__(self, profile_key, state_dir, size=600, rng=random, platform="web", returning_share=None,
                 android_overrides=None, web_overrides=None, ios_overrides=None, population=None):
        self.profile_key, self.rng, self.platform = profile_key, rng, platform
        # Steady-state share of returning visitors; V1 settles at 45% returning.
        self.new_floor = 1 - returning_share if returning_share is not None else 0.55
        self.android_overrides = android_overrides
        self.ios_overrides = ios_overrides
        self.web_overrides = web_overrides
        self.population = population or {}
        self.path = os.path.join(state_dir, f"personas-{profile_key}{'' if platform == 'web' else '-' + platform}.json")
        self.size = size
        self.personas = []
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.personas = json.load(f)

    def pick(self, make_user, now=None):
        """Return a persona: mostly returning visitors once the pool has filled up.

        A visitor who had a session in the last 30 minutes is not picked again, so one device
        never shows several overlapping or back-to-back sessions.
        """
        now = time.time() if now is None else now
        fresh = [p for p in self.personas if abs(now - p.get("last_seen", 0)) > 1800]
        # A young pool is mostly first-time visitors; it settles at ~55% new once it has grown.
        p_new = max(self.new_floor, 1 - len(self.personas) / 150)
        is_new = not fresh or self.rng.random() < p_new
        if is_new:
            if self.platform == "android":
                p = _new_android_persona(self.profile_key, self.rng, self.android_overrides)
            elif self.platform == "ios":
                p = _new_ios_persona(self.profile_key,self.rng,self.ios_overrides)
            else:
                p = _new_persona(self.profile_key, self.rng, self.web_overrides)
            make_user(p)
            if len(self.personas) >= self.size and fresh:
                self.personas.remove(min(fresh, key=lambda item: item.get("last_seen", 0)))
            if len(self.personas) < self.size:
                self.personas.append(p)
        else:
            # Skew towards a core of frequent visitors.
            pool = fresh
            p = pool[min(int(len(pool) * (self.rng.random() ** 1.3)), len(pool) - 1)]
        p["last_seen"] = now
        p["returning_visit"] = not is_new
        from .population import visit
        visit(p, self.population, self.platform, self.rng)
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


from .ios_devices import IOS_DEVICES
DEVICE_TIERS.update({d[0]:d[4] for d in IOS_DEVICES})

def _new_ios_persona(profile_key,rng,overrides=None):
    overrides=overrides or {}
    d=weighted([(d,(overrides.get('device_weights') or {}).get(d[0],d[5])) for d in IOS_DEVICES],rng)
    p=_new_android_persona(profile_key,rng)
    versions=overrides.get('os_version_mix') or {}
    version=weighted([(v,versions.get(v,0)) for v in d[1]],rng) if any(versions.get(v,0)>0 for v in d[1]) else rng.choice(d[1])
    p.update(platform='ios',manufacturer='Apple',model=d[0],os_version=version,screen=d[2],disk_total=int(d[3]*1e9*.9),kind='tablet' if d[0].startswith('iPad') else 'mobile')
    p.pop('os_code',None);p.pop('soc',None)
    return p
