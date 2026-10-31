"""Real compiled application modules, captured browser frames and matching source maps.

Rebuild the checked-in corpus with apps/web/scripts/build-web-exceptions.mjs.
Only stdlib JSON loading is needed when generating sessions. Release metadata and
CLI injection are appended after compiled code so mapped positions never move.
"""
import copy
import functools
import gzip
import hashlib
import json
import secrets
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID
from . import web_sites
from .security import post

UPLOAD_URL = "https://dsym-store.emb-api.com/v2/store/sourcemap"
CORPUS_PATH = Path(__file__).with_name("data") / "web-exceptions.json"


@functools.lru_cache(maxsize=1)
def corpus():
    return json.loads(CORPUS_PATH.read_text())


def application(url):
    path = urlsplit(url).path
    domain = "money" if "/money/" in path else "travel" if "/travel/" in path else "storefront"
    return corpus()["bundles"][domain]


def captured_exception(url, family, engine):
    return application(url)["engines"][engine]["exceptions"][family]


@functools.lru_cache(maxsize=512)
def bundle(url, release):
    filename = PurePosixPath(urlsplit(url).path).name
    compiled = application(url)
    lines = [compiled["code"], ";globalThis.VantraRelease=" + json.dumps(release) + ";"]
    footer = "//# sourceMappingURL=" + filename + ".map"
    raw = "\n".join(lines + [footer]) + "\n"
    bid = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()
    sm = copy.deepcopy(compiled["map"])
    sm.update(file=filename, debugId=str(UUID(bid)))
    snippet = (
        ';(E=>{try{const _=globalThis,c=(new _.Error).stack;c&&(_[E]=_[E]||{},_[E][c]="'
        + bid + '")}catch(E){}})("_EmbraceFileBundleIDs");'
    )
    return {
        "url": url, "release": release, "bundle_id": bid,
        "bundle": "\n".join(lines + ["// Injected by Embrace Web CLI:", snippet, footer]) + "\n",
        "sourcemap": json.dumps(sm, separators=(",", ":")),
    }


def format_frames(frames, url, engine):
    lines = []
    for frame in frames:
        name = frame["name"]
        if frame.get("native"):
            if engine == "chromium":
                lines.append(f"    at {name} (<anonymous>)")
            continue
        location = f'{url}:{frame["line"]}:{frame["column"]}'
        if engine == "chromium":
            lines.append(f"    at {name} ({location})" if name else f"    at {location}")
        else:
            lines.append(f"{name}@{location}")
    return "\n".join(lines)


def bundle_ids(urls, release, engine):
    result = {}
    for url in sorted(set(urls)):
        stack = format_frames(application(url)["engines"][engine]["injection"], url, engine)
        if engine == "chromium":
            stack = "Error\n" + stack
        result[stack] = bundle(url, release)["bundle_id"]
    return json.dumps(result, separators=(",", ":"))


def assets(target):
    web = target["web"]
    found = {}
    from .browser_session import releases

    for release in releases(web, 0):
        version = release["version"]
        for lob, pages in web_sites.SITES.items():
            for page in pages.values():
                urls = [
                    x[0]
                    for x in web_sites.first_party(web["cdn"], lob, version, page)
                    if x[1] == "script"
                ]
                urls += [
                    web_sites.bundle_url(web["cdn"], lob, version, c)
                    for c in (page.chunk, "vendor")
                ]
                for url in urls:
                    found[url] = bundle(url, version)
    return list(found.values())


def upload(asset, app_id, token, transport=post):
    """Fixed HTTPS destination; token is a multipart field as in the official CLI."""
    boundary = "----Embrace" + secrets.token_hex(16)
    chunks = []
    for name, value in [("id", asset["bundle_id"]), ("app", app_id), ("token", token)]:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + str(value).encode()
            + b"\r\n"
        )
    content = gzip.compress(
        json.dumps(
            {"bundle": asset["bundle"], "sourcemap": asset["sourcemap"]}
        ).encode(),
        mtime=0,
    )
    chunks += [
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="blob"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    response = transport(
        UPLOAD_URL,
        b"".join(chunks),
        {
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "User-Agent": "embrace_symbol_upload/2.31.0 (Python)",
        },
    )
    return {
        "bundle_id": asset["bundle_id"],
        "app_id": app_id,
        "http_status": getattr(response, "status", 200),
    }
