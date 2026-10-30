#!/usr/bin/env python3
"""Make a shareable copy of a real Web SDK session sample with no personal or account data.

The real 2.29 sample (~/Claudecode/go/tool/internal/schema-stats/testdata/web_2_29_0_sample.json)
shows what a deployed site's sessions look like, but it carries real identities: LaunchDarkly
context URLs with a person's name and email, real device, session, trace and span IDs, the
site's LaunchDarkly client ID and Embrace app ID. This tool replaces them while keeping the
structure, value types, formats, timings and the links between records:

- every hex identifier (32 uppercase, 32 or 16 lowercase, 24 lowercase) is replaced by a
  keyed hash of itself, so the same ID maps to the same new ID everywhere and traceparents,
  parent span IDs and soft-navigation correlation arrays still line up;
- LaunchDarkly evaluation-context segments are replaced by an anonymous context;
- the Embrace app ID in data URLs is replaced.

It then verifies the result (no email addresses, no decodable context with a name or email,
none of the original identifiers) and refuses to write output that fails.

Usage: python3 tools/scrub_web_sample.py SOURCE OUTPUT
"""
import base64
import hashlib
import json
import re
import sys

SALT = "session-generator-reference-scrub"
HEX_ID = re.compile(r"(?<![0-9A-Za-z])(?=[0-9A-Fa-f]*[A-Fa-f])([0-9A-F]{32}|[0-9a-f]{32}|[0-9a-f]{24}|[0-9a-f]{16})(?![0-9A-Za-z])")
CONTEXT = re.compile(r"(/contexts/)([A-Za-z0-9_-]{16,})")
EMBRACE_APP = re.compile(r"\ba-([a-z0-9]{5})\.data\.emb-api\.com")
EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}(?![\w-])")


def new_id(old):
    digest = hashlib.sha256(f"{SALT}:{old}".encode()).hexdigest()
    out = digest[:len(old)]
    return out.upper() if any(c in "ABCDEF" for c in old) else out


def anonymous_context(match):
    key = hashlib.sha256(f"{SALT}:{match.group(2)}".encode()).hexdigest()[:32]
    context = json.dumps({"kind": "user", "key": key, "anonymous": True}, separators=(",", ":"))
    return match.group(1) + base64.urlsafe_b64encode(context.encode()).decode().rstrip("=")


def decoded_contexts(text):
    for m in CONTEXT.finditer(text):
        raw = m.group(2) + "=" * (-len(m.group(2)) % 4)
        try:
            yield base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
        except ValueError:
            yield ""


def scrub(text):
    originals = set(HEX_ID.findall(text))
    text = CONTEXT.sub(anonymous_context, text)
    text = EMBRACE_APP.sub(lambda m: "a-" + hashlib.sha256(f"{SALT}:{m.group(1)}".encode()).hexdigest()[:5]
                           .translate(str.maketrans("0123456789", "ghjkmnpqrs")) + ".data.emb-api.com", text)
    text = HEX_ID.sub(lambda m: new_id(m.group(1)), text)
    return text, originals


def verify(text, originals):
    problems = []
    emails = [e for e in EMAIL.findall(text) if not re.fullmatch(r"[\w.-]+@\d[\d.]*", e)]
    if emails:
        problems.append(f"{len(emails)} email addresses remain")
    for decoded in decoded_contexts(text):
        if re.search(r'"(name|email|firstName|lastName)"', decoded):
            problems.append("a LaunchDarkly context still carries a name or email")
    left = originals & set(HEX_ID.findall(text))
    if left:
        problems.append(f"{len(left)} original identifiers remain")
    for line in text.splitlines():
        if line.strip():
            json.loads(line)
    return problems


def main(source, output):
    with open(source) as f:
        text = f.read()
    scrubbed, originals = scrub(text)
    problems = verify(scrubbed, originals)
    if problems:
        sys.exit("Refusing to write: " + "; ".join(problems))
    with open(output, "w") as f:
        f.write(scrubbed)
    print(f"Wrote {output}: {len(originals)} identifiers replaced, no personal data found.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
