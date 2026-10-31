"""Deterministic executable bundles, source maps and Web CLI-compatible bundle IDs.

Source positions in exception frames refer to these real assets. The map's
MD5-derived debugId and injected stack-to-ID map follow web-cli 2.31 source.
"""

import functools
import gzip
import hashlib
import json
import secrets
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID
from . import web_sites
from .security import post

# Original function, minified name, parameters, executable body.
FUNCTIONS = [
    ("mapProductPrice", "p", "t", "return t.price;"),
    ("formatProductPrice", "f", "t,e", "return t.formatPrice(e);"),
    ("loadCheckoutRoute", "l", "u", "return import(u);"),
    ("loadRouteChunk", "k", "u", "return import(u);"),
    ("decodeResponse", "j", "t", "return JSON.parse(t);"),
    ("requestAccount", "q", "u", "return fetch(u);"),
    ("cancelRequest", "a", "u,s", "return fetch(u,{signal:s});"),
    (
        "saveRecentProducts",
        "s",
        "t",
        'return localStorage.setItem("vantra:recently-viewed",t);',
    ),
    (
        "renderCheckout",
        "h",
        "t",
        'if(!t)throw new Error("Checkout content is unavailable");return t;',
    ),
    ("confirmOrder", "c", "t", "return p(t.item);"),
    ("commitCheckout", "r", "t", "return h(t);"),
]
FAMILIES = dict(
    zip(
        (
            "property_undefined",
            "not_a_function",
            "chunk_load",
            "webpack_chunk_load",
            "json_html",
            "failed_to_fetch",
            "abort",
            "quota_exceeded",
            "hydration",
        ),
        range(9),
    )
)
BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
UPLOAD_URL = "https://dsym-store.emb-api.com/v2/store/sourcemap"


def vlq(value):
    n = (-value << 1 | 1) if value < 0 else value << 1
    out = ""
    while True:
        digit = n & 31
        n >>= 5
        out += BASE64[digit | (32 if n else 0)]
        if not n:
            return out


@functools.lru_cache(maxsize=512)
def bundle(url, release):
    filename = PurePosixPath(urlsplit(url).path).name
    source = ["export const release = " + json.dumps(release) + ";"]
    lines = ["const v=" + json.dumps(release) + ";"]
    mappings = ["AAAA"]
    previous_line = previous_name = 0
    for i, (original, short, args, body) in enumerate(FUNCTIONS):
        original_line = len(source)
        readable = body.replace("return p(", "return mapProductPrice(").replace(
            "return h(", "return renderCheckout("
        )
        source += [f"export function {original}({args}) {{", "  " + readable, "}"]
        line = f"function {short}({args}){{{body}}}"
        lines.append(line)
        mappings.append(
            "".join(
                vlq(x)
                for x in (0, 0, original_line - previous_line, 0, i - previous_name)
            )
        )
        previous_line, previous_name = original_line, i
    lines.append("//# sourceMappingURL=" + filename + ".map")
    raw = "\n".join(lines) + "\n"
    bid = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()
    sm = {
        "version": 3,
        "file": filename,
        "sources": ["src/" + filename + ".source.js"],
        "sourcesContent": ["\n".join(source) + "\n"],
        "names": [f[0] for f in FUNCTIONS],
        "mappings": ";".join(mappings),
        "debugId": str(UUID(bid)),
    }
    snippet = (
        ';(E=>{try{const _=globalThis,c=(new _.Error).stack;c&&(_[E]=_[E]||{},_[E][c]="'
        + bid
        + '") }catch(E){}})("_EmbraceFileBundleIDs");'
    )
    lines[-1:-1] = ["// Injected by Embrace Web CLI:", snippet]
    return {
        "url": url,
        "release": release,
        "bundle_id": bid,
        "bundle": "\n".join(lines) + "\n",
        "sourcemap": json.dumps(sm, separators=(",", ":")),
    }


def frame(family, role="fault"):
    index = (
        FAMILIES.get(family, 0) if role == "fault" else (9 if role == "caller" else 10)
    )
    original, short, args, body = FUNCTIONS[index]
    return short, index + 2, len(f"function {short}({args}){{") + 1


def bundle_ids(urls, release, engine):
    result = {}
    for url in sorted(set(urls)):
        asset = bundle(url, release)
        line = len(FUNCTIONS) + 3
        stack = (
            f"Error\n    at {url}:{line}:30"
            if engine == "chromium"
            else f"@{url}:{line}:30"
        )
        result[stack] = asset["bundle_id"]
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
