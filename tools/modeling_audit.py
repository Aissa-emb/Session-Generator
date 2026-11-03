#!/usr/bin/env python3
"""Measure optional statistical models through SDK payloads; never send telemetry."""
import argparse
import copy
import json
from pathlib import Path
import random
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xcorgen import distributions as dist
from xcorgen.linter import lint_payloads
from xcorgen.runner import load_config
from xcorgen.spec import ANDROID_TARGET, IOS_TARGET, WEB_TARGET, default_spec
from xcorgen.spec_runner import SpecRun
from xcorgen.web_contract import check


def audit(count=600, seed=8219):
    now = 1793638800000
    results = []
    for target in (ANDROID_TARGET, IOS_TARGET, WEB_TARGET):
        scenario = default_spec()
        scenario['targets'] = [copy.deepcopy(target)]
        scenario['seed'] = seed
        scenario['destinations']['mode'] = 'end_to_end'
        scenario['chronosphere'] = {'tenant': 'demo-sandbox', 'token_ref': 'env:CHRONOSPHERE_API_TOKEN'}
        scenario['population'].update(model='varied', platform_mix={target['id']: 1})
        scenario['modeling'] = {'version': '1.0', 'bindings': [{
            'id': 'gateway-tail', 'quantity': 'backend_work_ms', 'share': .5,
            'distribution': dict(dist.lognormal(120, 500), bounds={'low': 0, 'high': 30000, 'policy': 'truncate'}),
            'effect': {'kind': 'tail', 'factor': 5, 'knee': .8}}],
            'experiments': [{'id': 'navigation', 'version': '1', 'variants': {'control': 50, 'treatment': 50},
                             'exposure_endpoints': ['home', 'accounts', 'locations']}]}
        started = time.perf_counter()
        random.seed(seed)
        with tempfile.TemporaryDirectory() as state:
            run = SpecRun(scenario, load_config(None), persist=False, now_ms=now, state_dir=state, rng=random.Random(seed))
            for i in range(count):
                t, session, _, deliveries, faults = run.session(now - 300000 + i * 200)
                issues = lint_payloads(deliveries)
                if target['platform'] == 'web':
                    for delivery in deliveries:
                        if delivery['target'] == 'embrace': check(delivery['payload'], issues.append)
                if issues: raise ValueError(issues[:5])
                run.record(t, session, faults, deliveries)
            report = run.emitted.summary()['modeling']
        effect = report['effects'][0]
        if not .42 <= effect['selected_visits'] / effect['eligible_visits'] <= .58:
            raise ValueError('Affected visit proportion outside statistical tolerance.')
        if effect['modeled_before']['p50'] != effect['modeled_after']['p50']:
            raise ValueError('Tail-only effect moved the body median.')
        if effect['modeled_after']['p95'] < effect['modeled_before']['p95'] * 1.3:
            raise ValueError('Tail effect did not produce its modeled upper-tail separation.')
        observed = effect['emitted']
        if observed['affected']['request_ms']['p95'] <= observed['control']['request_ms']['p95']:
            raise ValueError('Serialized requests did not preserve the modeled tail separation.')
        results.append({'platform': target['platform'], 'visits': count, 'seconds': round(time.perf_counter()-started, 3),
                        'payload_validation': 'passed', 'signals': report})
    return {'seed': seed, 'clock_ms': now, 'evidence': 'Local SDK payloads. No destination delivery or storage verification.', 'platforms': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps([{'platform': p['platform'], 'visits': p['visits'], 'seconds': p['seconds']} for p in result['platforms']]))
