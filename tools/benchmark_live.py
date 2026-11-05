#!/usr/bin/env python3
"""Measure generation/bookkeeping after visitor-pool warmup; never send telemetry."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import random
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xcorgen.linter import lint_payloads
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', default='tools/fixtures/load-web.json')
    parser.add_argument('--sessions', type=int, default=10000)
    args = parser.parse_args()
    if args.sessions < 1:
        parser.error('Session count must be positive.')
    scenario = json.loads(Path(args.spec).read_text())
    scenario['traffic'] = dict(mode='live', rate_per_minute=600, duration_minutes=30)
    random.seed(7331)
    with tempfile.TemporaryDirectory(prefix='sg-benchmark-') as directory:
        run = SpecRun(scenario, load_config(None), state_dir=directory, persist=True)
        base, totals, block_count = run.now_ms, defaultdict(float), 0
        def timed(name, fn, *args):
            started = time.perf_counter()
            result = fn(*args)
            totals[name] += (time.perf_counter() - started) * 1000
            return result
        for i in range(args.sessions):
            run.now_ms = base + i * 100
            target, session, _, deliveries, faults = timed('generate', run.session, run.now_ms)
            if timed('lint', lint_payloads, deliveries):
                raise ValueError('Generated payload failed realism lint.')
            timed('record', run.record, target, session, faults, deliveries)
            if i % 50 == 0:
                timed('bookkeeping_amortized', run.save)
                timed('bookkeeping_amortized', lambda: json.dumps(run.emitted.summary()))
            block_count += 1
            if block_count == 1000 or i == args.sessions - 1:
                print(json.dumps(dict(sessions=i + 1, ms_per_session={k: round(v / block_count, 3) for k, v in totals.items()})), flush=True)
                totals.clear()
                block_count = 0


if __name__ == '__main__':
    main()
