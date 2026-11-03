#!/usr/bin/env python3
"""Generate stitched Cortex XCOR RUM (Embrace) sessions and Cortex XCOR (Chronosphere) distributed traces.

V2 runs a ScenarioSpec (spec/scenario.schema.json):
  python3 generate.py run spec.json [--dry-run DIR] [--seed N]
  python3 generate.py validate spec.json        schema, platform limits and realism checks
  python3 generate.py explain spec.json         plain-language summary
  python3 generate.py preset list | show NAME | apply NAME spec.json
  python3 generate.py report DIR spec.json      requested versus emitted, from dry-run payloads
  python3 generate.py lint DIR                  realism linter on dry-run payloads
  python3 generate.py capabilities              platform limits, fault catalog and backfill rules

V1 flags still work:
  python3 generate.py --profile ecommerce --sessions 25
  python3 generate.py --profile all --rate 6 --duration 120
  python3 generate.py --profile banking --rate 3 --incident fraud-latency
  python3 generate.py --profile travel --sessions 5 --dry-run out/
"""

import argparse
import math
import os
import random
import sys
import time

from xcorgen.profiles import PROFILES, load
from xcorgen.runner import ROOT, Generator, Sender, load_config, run_burst, run_live
from xcorgen.security import SecurityError


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="ecommerce", help=f"{', '.join(PROFILES)} or all (comma-separated also works)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--sessions", type=int, help="burst mode: generate N sessions dated over the last --spread minutes")
    mode.add_argument("--rate", type=float, help="live mode: new sessions per minute, sent in real time")
    ap.add_argument("--spread", type=float, default=8,
                    help="burst mode: spread sessions over this many minutes (default 8). Sessions older than 8 minutes "
                         "are sent without trace links: Cortex XCOR accepts trace data only for the last 10 minutes")
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
    ap.add_argument("--scenario", choices=["clean", "mobile", "backend", "slow", "handled", "blockage", "crash"],
                    help="select a primary scenario for a bounded verification run; excluded from population-rate claims")
    ap.add_argument("--incident-phase", choices=["scheduled", "onset", "incident", "recovery"])
    ap.add_argument("--rollout-phase", choices=["active", "rollback"])
    ap.add_argument("--crash-family", type=int, choices=range(5), help="bounded crash-family verification, 0 through 4")
    ap.add_argument("--manifest", help="owner-only JSONL evidence file for live sessions and acceptance receipts")
    ap.add_argument("--verification-plan", help="JSON list of bounded verification cases, then resume the normal mix at the same rate")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.sessions is not None and a.sessions <= 0:
        ap.error("--sessions must be positive")
    if a.rate is not None and (not math.isfinite(a.rate) or a.rate <= 0):
        ap.error("--rate must be a positive finite number")
    for label in ("spread", "duration"):
        if not math.isfinite(getattr(a, label)) or getattr(a, label) < 0:
            ap.error(f"--{label} must be a nonnegative finite number")

    keys = list(PROFILES) if a.profile == "all" else [k.strip() for k in a.profile.split(",")]
    if a.list_incidents:
        for k in keys:
            print(f"{k}: {', '.join(load(k).incidents)}")
        return
    if a.seed is not None:
        random.seed(a.seed)
    cfg = load_config(a.config)
    generation = cfg.setdefault("generation", {})
    if a.manifest:
        generation["manifest_path"] = os.path.abspath(a.manifest)
    if a.verification_plan:
        import json
        with open(a.verification_plan) as f:
            generation["verification_plan"] = json.load(f)
    for key, value in (("primary_scenario", a.scenario), ("incident_phase", a.incident_phase),
                       ("rollout_phase", a.rollout_phase), ("crash_family", a.crash_family)):
        if value is not None:
            generation[key] = value
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


SUBCOMMANDS = ("run", "validate", "explain", "preset", "report", "lint", "capabilities")


def spec_main(argv):
    import json
    from xcorgen import catalog, limits, linter, presets, spec as specs
    from xcorgen.emitted import from_dry_run
    ap = argparse.ArgumentParser(prog="generate.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run a ScenarioSpec")
    r.add_argument("spec")
    r.add_argument("--dry-run", metavar="DIR", help="write payloads to DIR instead of sending")
    r.add_argument("--seed", type=int)
    r.add_argument("--matrix", action="store_true", help="run this causal scenario across Android, iOS and web (configure each app ID)")
    r.add_argument("--symbol-receipt", help="Source-map upload receipts from tools/symbols.py")
    r.add_argument("--resume", metavar="RUN_ID", help="resume the frozen plan and skip accepted deliveries")
    r.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--verbose", action="store_true")
    for name in ("validate", "explain"):
        sub.add_parser(name).add_argument("spec")
    pr = sub.add_parser("preset")
    pr.add_argument("action", choices=["list", "show", "apply"])
    pr.add_argument("name", nargs="?")
    pr.add_argument("spec", nargs="?", help="apply: spec to merge into (default: the default spec)")
    rp = sub.add_parser("report")
    rp.add_argument("dir")
    rp.add_argument("spec")
    sub.add_parser("lint").add_argument("dir")
    sub.add_parser("capabilities")
    a = ap.parse_args(argv)

    if a.command == "capabilities":
        print(json.dumps({"platform_limits": limits.capabilities(), "faults": catalog.describe()}, indent=1))
        return 0
    if a.command == "preset":
        if a.action == "list":
            for p in presets.listing():
                print(f"{p['id']:<36} {p['group']:<8} {p['description']}")
            return 0
        if not a.name:
            sys.exit("Name a preset (see: generate.py preset list).")
        if a.action == "show":
            print(json.dumps(presets.catalog_presets()[a.name][3], indent=1))
        else:
            base = specs.load(a.spec) if a.spec else specs.default_spec()
            print(json.dumps(presets.apply(base, a.name), indent=1))
        return 0
    if a.command == "lint":
        issues = linter.lint_dry_run(a.dir)
        for i in issues:
            print(f"{i['severity']:<7} {i['code']:<14} {i['path']}: {i['message']}")
        print("OK: no realism issues in sampled payloads" if not issues else f"{len(issues)} realism issues")
        return 1 if specs.errors(issues) else 0
    spec = specs.load(a.spec)
    if a.command == "run" and a.seed is not None:
        spec["seed"] = a.seed
    if a.command == "run" and a.matrix:
        if not spec.get("causal"):
            ap.error("--matrix requires a causal scenario preset")
        import copy
        have = {t["platform"]: t for t in spec["targets"]}
        spec["targets"] = [have.get(t["platform"], copy.deepcopy(t)) for t in (specs.ANDROID_TARGET, specs.IOS_TARGET, specs.WEB_TARGET)]
        spec.setdefault("population", {})["platform_mix"] = {t["id"]: 1 for t in spec["targets"]}
        if spec["traffic"]["mode"] != "live":
            spec["traffic"]["count"] = max(3, spec["traffic"]["count"])
    if a.command == "validate":
        issues = specs.validate(spec)
        for i in issues:
            print(f"{i['severity']:<7} {i['path']}: {i['message']}")
        print("Valid." if not specs.errors(issues) else f"{len(specs.errors(issues))} errors.")
        return 1 if specs.errors(issues) else 0
    if a.command == "explain":
        print("\n".join(specs.explain(spec)))
        return 0
    if a.command == "report":
        report = from_dry_run(a.dir, spec)
        print(report.table())
        return 0 if all(r.get("within_tolerance", True) for r in report.summary()["faults"]) else 1
    return run_spec(a, spec)


def run_spec(a, spec):
    import random as _random
    from xcorgen import secrets, spec as specs
    from xcorgen.linter import lint_payloads
    from xcorgen.spec_runner import SpecRun
    seed = a.seed if a.seed is not None else spec.get("seed")
    if seed is not None:
        _random.seed(seed)
    cfg = load_config(a.config if os.path.exists(a.config) else None)
    s = specs.normalized(spec)
    if not a.dry_run and any(t["platform"] == "ios" and t["embrace_app_id"] == "ios00" for t in s["targets"]):
        sys.exit("Create an iOS app in Cortex XCOR RUM and replace ios00 before live delivery.")
    if s["destinations"]["mode"] == "end_to_end" and not a.dry_run:
        ch = s.get("chronosphere") or {}
        cfg["chronosphere"]["tenant"] = ch.get("tenant")
        if ch.get("token_ref"):
            cfg["chronosphere"]["api_token"] = secrets.resolve(ch["token_ref"])
        if not cfg["chronosphere"].get("api_token") and not cfg["otlp"].get("endpoint"):
            sys.exit("End-to-end runs need chronosphere.token_ref in the spec (or CHRONOSPHERE_API_TOKEN).")
    else:
        cfg["chronosphere"] = {"tenant": None, "api_token": None}  # nosec B105: no credential in sessions-only runs
    try:
        resume = getattr(a, "resume", None)
        directory = a.dry_run or (os.path.join(ROOT, ".runs", resume) if resume else None)
        if resume and (os.path.basename(resume) != resume or resume in (".", "..")):
            raise ValueError("Resume expects a run ID, not a path.")
        frozen_now = None
        if resume:
            from xcorgen.journal import RunJournal
            journal = RunJournal(directory)
            meta = journal.get("plan")
            journal.close()
            if not meta or meta["run_id"] != resume:
                raise ValueError("Run ID does not match the saved plan.")
            frozen_now = meta["now_ms"]
        run = SpecRun(spec, cfg, persist=not a.dry_run, now_ms=frozen_now)
    except ValueError as error:
        sys.exit(str(error))
    for issue in run.issues:
        print(f"{issue['severity']}: {issue['path']}: {issue['message']}", file=sys.stderr)
    if not a.dry_run:
        from xcorgen.web_symbols import assets
        required={(t['embrace_app_id'],asset['bundle_id']) for t in s['targets'] if t['platform']=='web' and t['web'].get('symbolication') for asset in assets(t)}
        if required:
            from pathlib import Path
            import json
            receipts=json.loads(Path(a.symbol_receipt).read_text()) if a.symbol_receipt else []
            accepted={(r['app_id'],r['bundle_id']) for r in receipts if 200<=r['http_status']<300}
            if not required <= accepted:
                sys.exit('Upload the release source maps first and pass --symbol-receipt with their receipts.json.')
        # Preflight: generate a sample without sending and lint it; any realism error blocks the run.
        preview = SpecRun(spec, cfg, persist=False, state_dir=os.path.join(ROOT, ".state", "preflight"))
        sample = []
        for _ in range(12):
            _, s_, _, deliveries, _ = preview.session(preview.now_ms)
            sample.extend(deliveries)
        blocking = specs.errors(lint_payloads(sample))
        if blocking:
            sys.exit("Realism lint failed; nothing was sent:\n" + "\n".join(f"  {i['message']}" for i in blocking))
    run.open_manifest(directory, resume=bool(resume), dry=bool(a.dry_run))
    sender = Sender(cfg, a.dry_run, verbose=a.verbose)
    t0 = time.time()
    if s["traffic"]["mode"] == "live":
        n = run.run_live(sender, a.quiet)
    else:
        n, _ = run.run_batch(sender, a.quiet)
    sender.close()
    st = sender.stats
    print(f"\nDone in {time.time() - t0:.1f}s: {n} sessions, {st['embrace_spans']} Embrace span payloads, "
          f"{st['embrace_logs']} log payloads, {st['otlp_traces']} backend trace exports, {st['errors']} errors"
          + (f" (written to {a.dry_run})" if a.dry_run else f"; manifest {run.manifest}"))
    print(run.emitted.table())
    from pathlib import Path
    report_dir=Path(run.manifest).parent/'reports'
    report_dir.mkdir(exist_ok=True)
    (report_dir/'emitted.json').write_text(__import__('json').dumps(run.emitted.summary(),indent=2)+'\n')
    if run.spec.get('modeling'):
        (report_dir/'signals.json').write_text(__import__('json').dumps(dict(run.emitted.summary()['modeling'], run_id=run.run_id, frozen_clock_ms=run.now_ms),indent=2)+'\n')
    for e in sender.last_errors:
        print(f"  ! {e}", file=sys.stderr)
    return 1 if st["errors"] else 0


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
            sys.exit(spec_main(sys.argv[1:]))
        main()
    except (SecurityError, ValueError) as error:
        sys.exit(str(error))
