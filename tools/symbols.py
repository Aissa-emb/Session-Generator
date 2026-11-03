#!/usr/bin/env python3
"""Build reviewable web assets; upload only with explicit confirmation and a token file."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xcorgen import spec, web_symbols
from xcorgen.security import read_token_file


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("spec")
    p.add_argument("--out", required=True)
    p.add_argument("--upload", action="store_true")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--token-file")
    a = p.parse_args()
    s = spec.normalized(spec.load(a.spec))
    root = Path(a.out)
    root.mkdir(parents=True, exist_ok=True)
    if a.upload and not (a.confirm and a.token_file):
        p.error(
            "Upload requires --confirm and --token-file after reviewing the assets."
        )
    token = read_token_file(a.token_file) if a.upload else None
    receipts = []
    manifest = []
    for t in s["targets"]:
        if t["platform"] != "web" or not t["web"].get("symbolication"):
            continue
        for asset in web_symbols.assets(t):
            directory = root / asset["bundle_id"]
            directory.mkdir(exist_ok=True)
            (directory / "bundle.js").write_text(asset["bundle"])
            (directory / "bundle.js.map").write_text(asset["sourcemap"])
            manifest.append({k: asset[k] for k in ("url", "release", "bundle_id")})
            if a.upload:
                receipts.append(web_symbols.upload(asset, t["embrace_app_id"], token))
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if a.upload:
        (root / "receipts.json").write_text(json.dumps(receipts, indent=2) + "\n")
    print(
        f"{len(manifest)} bundles and maps written to {root}; {len(receipts)} uploads acknowledged."
    )


if __name__ == "__main__":
    main()
