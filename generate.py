#!/usr/bin/env python3
"""Generate stitched demo data: Embrace (Cortex XCOR RUM) web sessions + Chronosphere distributed traces.

Examples:
  python3 generate.py --profile ecommerce --sessions 25
  python3 generate.py --profile all --rate 6 --duration 120
  python3 generate.py --profile banking --rate 3 --incident fraud-latency
  python3 generate.py --profile travel --sessions 5 --dry-run out/
"""

import argparse
import os
import random
import sys
import time

from xcorgen.profiles import PROFILES, load
from xcorgen.runner import ROOT, Generator, Sender, load_config, run_burst, run_live


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="ecommerce", help=f"{', '.join(PROFILES)} or all (comma-separated also works)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--sessions", type=int, help="burst mode: generate N sessions that ended in the last --spread minutes")
    mode.add_argument("--rate", type=float, help="live mode: new sessions per minute, sent in real time")
    ap.add_argument("--spread", type=float, default=30, help="burst mode: spread sessions over this many minutes (default 30)")
    ap.add_argument("--duration", type=float, default=0, help="live mode: stop after N minutes (default: run until Ctrl-C)")
    ap.add_argument("--platform", choices=["android", "web"],
                    help="client platform to simulate (default: config.json 'platform', else android)")
    ap.add_argument("--incident", help="raise one failure scenario, e.g. payment-timeouts (see --list-incidents)")
    ap.add_argument("--list-incidents", action="store_true")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--dry-run", metavar="DIR", help="write payloads to DIR instead of sending")
    ap.add_argument("--emit-client-spans", action="store_true",
                    help="also send the browser span to Chronosphere yourself (only for apps WITHOUT network span forwarding)")
    ap.add_argument("--backend-logs", action="store_true", help="also send trace-correlated backend logs over OTLP")
    ap.add_argument("--diurnal", action="store_true",
                    help="live mode: follow a daily traffic curve (US Eastern time) around the --rate average")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    keys = list(PROFILES) if a.profile == "all" else [k.strip() for k in a.profile.split(",")]
    if a.list_incidents:
        for k in keys:
            print(f"{k}: {', '.join(load(k).incidents)}")
        return
    if a.seed is not None:
        random.seed(a.seed)
    cfg = load_config(a.config)
    if a.dry_run:
        for k in keys:
            cfg["embrace"]["apps"].setdefault(k, cfg["embrace"]["apps"].get("default") or "dryrn")
    elif not (cfg["otlp"].get("endpoint") or (cfg["chronosphere"].get("tenant") and cfg["chronosphere"].get("api_token"))):
        sys.exit("No backend trace destination. Set chronosphere.tenant and CHRONOSPHERE_API_TOKEN "
                 "(or otlp.endpoint) in config.json / the environment.")

    platform = a.platform or cfg.get("platform") or "android"
    gen = Generator(keys, cfg, a.incident, a.emit_client_spans, a.backend_logs, persist=not a.dry_run,
                    platform=platform)
    sender = Sender(cfg, a.dry_run, verbose=a.verbose)
    t0 = time.time()
    if a.rate:
        n = run_live(gen, sender, a.rate, a.duration, a.quiet, a.diurnal)
    else:
        n = run_burst(gen, sender, a.sessions or 10, a.spread, a.quiet)
    sender.close()
    st = sender.stats
    print(f"\nDone in {time.time() - t0:.1f}s: {st['embrace_spans']} Embrace span payloads, {st['embrace_logs']} log "
          f"payloads, {st['otlp_traces']} backend trace exports, {st['otlp_logs']} backend log exports, "
          f"{st['errors']} errors" + (f" (written to {a.dry_run})" if a.dry_run else ""))
    for e in sender.last_errors:
        print(f"  ! {e}", file=sys.stderr)
    sys.exit(1 if st["errors"] else 0)


if __name__ == "__main__":
    main()
