#!/usr/bin/env python3
"""Offline causal matrix and evidence reports. Never sends telemetry or credentials."""
import argparse
import copy
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from xcorgen.causal.registry import SCENARIOS, VERSION
from xcorgen.causal.validation import verify_returned_trace
from xcorgen.runner import Sender
from xcorgen.spec import default_spec, normalized, ANDROID_TARGET, IOS_TARGET, WEB_TARGET
from xcorgen.spec_runner import SpecRun
from xcorgen.linter import lint_payloads


def configuration(platform, scenario, variant, seed=7331):
    s=default_spec()
    s.update(name=f'{SCENARIOS[scenario][0]} · {variant}', seed=seed,
        causal={'scenario':scenario,'variant':variant,'reference':True,'mode':'single','cache':'cold','network':'wifi'})
    s['targets']=[copy.deepcopy({'android':ANDROID_TARGET,'ios':IOS_TARGET,'web':WEB_TARGET}[platform])]
    s['population']['platform_mix']={s['targets'][0]['id']:1}
    s['population']['line_of_business_mix']={k:int(k==SCENARIOS[scenario][1]) for k in ('retail','banking','travel')}
    s['destinations']={'mode':'end_to_end','backend_logs':True}
    s['chronosphere']={'tenant':'demo-sandbox','token_ref':'env:CHRONOSPHERE_API_TOKEN'}  # nosec B105 -- credential reference
    s['traffic']={'mode':'burst','count':1,'window':{'last_minutes':1}}
    return normalized(s)


def evidence(manifest, observed=None):
    sessions=[]
    # Terminal records supersede planned records; failed deliveries stay in denominator.
    rows={}
    for line in Path(manifest).read_text().splitlines():
        row=json.loads(line)
        if row.get('causal'):rows[(row['run_id'],row['index'])]=row
    for row in rows.values():
        c=copy.deepcopy(row['causal'])
        c['delivery_stage']=row.get('stage','planned')
        c['evidence']['export_accepted']=row.get('stage')=='sent' and not row.get('dry_run',True)
        if observed is not None:
            c['destination_evidence']=verify_returned_trace(c,observed)
        sessions.append(c)
    return {'scenario_version':VERSION,'sessions':sessions,
        'note':'Generated synthetic payloads. Acceptance is not storage. Navigation requires checking the returned dashboard link.'}


def matrix(directory, seed):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    cfg={'embrace':{'apps':{}},'chronosphere':{},'otlp':{}}
    now=time.time()*1000
    rows=[]
    for platform in ('android','ios','web'):
        for scenario in SCENARIOS:
            for variant in ('affected','control','recovered'):
                folder=directory/platform/scenario/variant
                s=configuration(platform,scenario,variant,seed)
                folder.mkdir(parents=True,exist_ok=True)
                (folder/'scenario.json').write_text(json.dumps(s,indent=2)+'\n')
                random.seed(seed)
                run=SpecRun(s,cfg,persist=False,now_ms=now)
                run.open_manifest(str(folder),dry=True)
                sender=Sender(cfg,str(folder/'payloads'))
                try:run.run_batch(sender,quiet=True)
                finally:sender.close()
                c=run.causal_evidence[0]
                payloads=[json.loads(p.read_text()) for p in (folder/'payloads').glob('*.json')]
                errors=lint_payloads(payloads)
                if errors:raise ValueError(errors)
                c['evidence']['export_accepted']=False
                (folder/'evidence.json').write_text(json.dumps(c,indent=2)+'\n')
                rows.append({'platform':platform,'scenario':scenario,'variant':variant,
                    'session_id':c['session_id'],'implemented':True,'fixture_validated':True,
                    'runtime_executed':False,'export_accepted':False,'dashboard_verified':False,
                    'outcome':c['expected_outcome'],'evidence':str(folder/'evidence.json'),
                    'trace_ids':sorted({n['trace_id'] for n in c['graph'] if n['service']!='frontend'})})
                if run.journal:run.journal.close()
    report={'version':VERSION,'seed':seed,'total':len(rows),'cases':rows,
        'blockers':['Native app/device execution was not performed.',
                    'ios00 is a placeholder: configure a real iOS app before live delivery.',
                    'These are offline fixtures; destination receipt, NSF, retention and dashboard navigation require live evidence.']}
    (directory/'matrix.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f"{len(rows)} platform/scenario/cohort payload fixtures validated: {directory / 'matrix.json'}")


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    m=sub.add_parser('matrix');m.add_argument('directory');m.add_argument('--seed',type=int,default=7331)
    e=sub.add_parser('evidence');e.add_argument('manifest');e.add_argument('--traces',help='Saved actual Chronosphere TRACE_IDS response');e.add_argument('--output',required=True)
    a=p.parse_args()
    if a.command=='matrix':matrix(a.directory,a.seed)
    else:
        result=evidence(a.manifest,json.loads(Path(a.traces).read_text()) if a.traces else None)
        Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
        print(f"{len(result['sessions'])} sessions: {a.output}")

if __name__=='__main__':main()
