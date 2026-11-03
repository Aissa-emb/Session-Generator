"""Local URL rollup preview for the classifications used by Vantra routes.

Mirrors go/lib/grouper/classify.go (12-character hex threshold, two dashes
for slugs, numbers, UUIDs and dates). The server's learned wildcard is shown
only after 25 distinct siblings in the supplied sample; sparse samples are
reported explicitly, not claimed to be a prediction of the server's full history.
"""
import re
from collections import defaultdict
from urllib.parse import urlsplit


def segment(value):
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return "«number»"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "«timestamp»"
    if len(value) >= 64:
        return "«text»"
    if re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value):
        return "«uuid»"
    if re.fullmatch(r"[a-fA-F0-9]{13,}", value):
        return "«hex»"
    if value.count("-") >= 2 and re.fullmatch(r"[a-zA-Z0-9-]+", value):
        return "«slug»"
    return value


def preview(urls):
    paths, siblings = [], defaultdict(set)
    for url in sorted(set(urls)):
        u = urlsplit(url)
        bits = [segment(p) for p in u.path.split("/")]
        paths.append((url, u.netloc, bits))
        for i, bit in enumerate(bits):
            siblings[(u.netloc, tuple(bits[:i]))].add(bit)
    return [{"url": url, "rollup": host + "/".join("«*»" if len(siblings[(host, tuple(bits[:i]))]) > 25 else p
             for i, p in enumerate(bits)), "scope": "sample; server history may merge more siblings"}
            for url, host, bits in paths]
