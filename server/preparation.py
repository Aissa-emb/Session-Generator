"""Persisted, reviewable preparation plans with resumable local orchestration."""
import copy
import fcntl
import hashlib
from functools import wraps
import os
import subprocess  # nosec B404
import sys
import time
from xcorgen import limits, spec as specs
from .service import Service, ROOT
from .store import Store
from . import readiness


def serialized(fn):
    @wraps(fn)
    def guarded(svc, id, *args, **kwargs):
        svc.store.get('preparations', id)
        path = svc.store.directory / (hashlib.sha256(id.encode()).hexdigest()+'.transition.lock')
        with path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return fn(svc, id, *args, **kwargs)
    return guarded


def bounds(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}.')
    return value


def fit_incidents(s, start, end):
    """Keep an in-window story; project out-of-window incidents before history ends."""
    for incident in s.get('incidents', []):
        w = specs.incident_window(incident)
        duration = w['onset_ms'] + w['peak_ms'] + w['recovery_ms']
        if not w['repeat_ms'] and (w['start_ms'] > end or w['start_ms'] + duration < start):
            incident['start'] = specs.iso(max(start, end-duration-60000))
    return s


def plan(svc, spec_id, history_hours=24, rate_per_minute=6, minutes_until_demo=10,
         duration_minutes=120, prepare_ahead=False, incident=None):
    bounds(history_hours, 'History hours', 0, 24)
    bounds(rate_per_minute, 'Rate', .1, limits.MAX_RATE_PER_STREAM)
    bounds(minutes_until_demo, 'Time until demo', 0, 10080)
    bounds(duration_minutes, 'Duration', 1, 10080)
    if not isinstance(prepare_ahead, bool):
        raise ValueError('Prepare ahead must be boolean.')
    current = svc.store.get('scenario_specs', spec_id)
    base = copy.deepcopy(current['spec'])
    mode = base['destinations']['mode']
    if prepare_ahead and mode != 'end_to_end':
        raise ValueError('Prepare ahead requires the end-to-end destination.')
    now = time.time()*1000
    stages = []
    if history_hours and not prepare_ahead:
        historical = copy.deepcopy(base)
        historical['destinations']['mode'] = 'sessions_only'
        start = now-history_hours*3600000
        end = now-(8*60000 if mode == 'end_to_end' else 0)
        if end > start:
            count = max(1, round(history_hours*60 * rate_per_minute))
            historical['traffic'] = {'mode':'backfill', 'count':count, 'window':{'last_minutes':history_hours*60}}
            fit_incidents(historical,start,end)
            stages.append({'id':'history','title':'Backfill session history','spec':svc.require_valid(historical),'status':'pending'})
    if mode == 'end_to_end' and not prepare_ahead:
        recent=copy.deepcopy(base)
        recent['traffic']={'mode':'burst','count':max(1,round(8*rate_per_minute)),'window':{'last_minutes':8}}
        fit_incidents(recent, now-8*60000, now)
        stages.append({'id':'recent','title':'Generate recent stitched traces','spec':svc.require_valid(recent),'status':'pending'})
    live=copy.deepcopy(base)
    live['traffic']={'mode':'live','rate_per_minute':rate_per_minute,'duration_minutes':duration_minutes}
    # Historic, nonrepeating incidents have ended; don't make the live spec invalid.
    live['incidents']=[i for i in live.get('incidents',[]) if i.get('repeat_every_minutes') or specs.parse_time(i['start'])+sum(i.get(k,d)*60000 for k,d in [('onset_minutes',0),('peak_minutes',30),('recovery_minutes',0)]) >= now]
    referenced={i['id'] for i in live['incidents']}
    expired_schedules={i['id'] for i in base.get('incidents',[])}-referenced
    expired_faults={i.get('fault_ref') for i in base.get('incidents',[]) if i['id'] in expired_schedules}
    for fault in live['client_faults']+live['backend_faults']:
        if (fault.get('schedule') in expired_schedules or fault['id'] in expired_faults) and not (incident and incident.get('fault_ref')==fault['id']):
            fault['rate']=0
            fault.pop('schedule',None)
    if incident:
        incident=copy.deepcopy(incident)
        incident['start']=specs.iso(now+(minutes_until_demo+3)*60000)
        live['incidents']=[i for i in live['incidents'] if i.get('fault_ref')!=incident.get('fault_ref')]
        live['incidents'].append(incident)
    stages.append({'id':'live','title':'Start a live stream','spec':svc.require_valid(live),'status':'pending'})
    previews = {s['id']: svc.preview(s['spec'])['report'] for s in stages}
    return svc.store.put('preparations', {'spec_id':spec_id,'spec_version':current['version'],'status':'planned',
        'mode':mode,'stages':stages,'previews':previews,'history_hours':history_hours,'prepare_ahead':prepare_ahead,
        'recommendation':'Prepare ahead is recommended: start the live stream now to accumulate trace history.' if minutes_until_demo>120 else None,
        'trace_caveat':limits.TRACE_CAVEAT,'backfill_notes':limits.BACKFILL_NOTES,
        'checklist':[{'id':'readiness','title':'Check destination readiness','status':'pending'}]+[{'id':s['id'],'title':s['title'],'status':'pending'} for s in stages]})


def checks(svc, record, dry_run):
    results=[]
    for t in record['stages'][-1]['spec']['targets']:
        target={'id':t['id'],'kind':'embrace','target':t}
        observed=readiness.check(svc.store,target)
        required={'nsf','retention','probe'} if record['mode']=='end_to_end' else set()
        if t['platform']=='web' and t['web'].get('origin_header',{}).get('policy','none')!='none':
            required.add('origins')
        results.append(dict(observed,ready=all(i['state']=='ok' for i in observed['items'] if i['id'] in required),required=sorted(required)))
    return {'status':'simulated' if dry_run else 'complete' if all(r['ready'] for r in results) else 'blocked','targets':results}


def launch(svc, id):
    env=dict(os.environ,SG_DATA_DIR=str(svc.store.directory))
    subprocess.Popen([sys.executable,'-m','server.preparation',id],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)  # nosec B603


@serialized
def start(svc, id, confirm=False, dry_run=False, author='user'):
    if confirm is not True:
        raise ValueError('Review the preparation plan and pass confirm: true.')
    record=svc.store.get('preparations',id)
    if record['status'] in ('running','completed'):
        return record
    if record['status'] not in ('planned','blocked'):
        raise ValueError('Create a new preparation plan after a failed or cancelled run.')
    checked=checks(svc,record,dry_run)
    checklist=copy.deepcopy(record['checklist'])
    checklist[0]['status']=checked['status']
    if checked['status']=='blocked':
        return svc.store.update('preparations',id,status='blocked',readiness=checked,checklist=checklist)
    # Validate all stages before the first send, including credentials and size limits.
    for stage in record['stages']:
        svc.require_valid(stage['spec'])
    if not dry_run and record['mode']=='end_to_end':
        from . import secrets
        if not secrets.for_spec(svc.store,record['stages'][-1]['spec'],'chronosphere'):
            raise ValueError('Configure the Cortex XCOR token before preparing end-to-end traffic.')
    record=svc.store.update('preparations',id,status='running',dry_run=dry_run,author=author,authorized=True,readiness=checked,checklist=checklist)
    if svc.launch_jobs:
        launch(svc,id)
    return record


@serialized
def advance(svc,id):
    record=svc.store.get('preparations',id)
    if record['status']!='running':
        return record
    stages=copy.deepcopy(record['stages']);checklist=copy.deepcopy(record['checklist'])
    for index,stage in enumerate(stages):
        if stage['status']=='complete':
            continue
        if not stage.get('spec_id'):
            # Fixed identifier survives a crash between version creation and stage persistence.
            stage_id=id+'-'+stage['id']
            try:
                saved=svc.store.get('scenario_specs',stage_id)
            except KeyError:
                saved=svc.store.save_spec(stage['spec'],record['author'],stage_id)
            stage['spec_id']=saved['id']
            svc.store.update('preparations',id,stages=stages)
        if not stage.get('run_id'):
            run=svc.start_run(stage['spec_id'],confirm=True,dry_run=record['dry_run'],author=record['author'],idempotency_key='preparation:'+id+':'+stage['id'])
            stage['run_id']=run['id'];stage['status']='running'
        run=svc.store.get('runs',stage['run_id'])
        if stage['id']=='live':
            # The live stream owns its job after orchestration completes.
            stream=svc.store.put('streams',{'id':id+'-stream','name':stage['spec']['name'],'spec_id':stage['spec_id'],'run_id':run['id'],
                'targets':run['targets'],'status':'running','dry_run':record['dry_run'],'authorized':True,'author':record['author'],'preparation_id':id})
            stage['stream_id']=stream['id'];stage['status']='complete'
        elif run['status']=='completed':
            stage['status']='complete'
        elif run['status'] in ('failed','stopped'):
            stage['status']='failed'
        checklist[index+1]['status']=stage['status'];checklist[index+1]['run_id']=run['id']
        state='failed' if stage['status']=='failed' else 'completed' if all(s['status']=='complete' for s in stages) else 'running'
        return svc.store.update('preparations',id,stages=stages,checklist=checklist,status=state,heartbeat=time.time())
    return svc.store.update('preparations',id,status='completed')


@serialized
def cancel(svc,id,author='user'):
    record=svc.store.update('preparations',id,status='cancelled')
    for stage in record['stages']:
        if stage.get('run_id'):
            svc.control(stage['run_id'],'stop',author)
        if stage.get('stream_id'):
            svc.store.update('streams',stage['stream_id'],status='stopped')
    return record


def execute(id):
    svc=Service(Store())
    with (svc.store.directory/(id+'.preparation.lock')).open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            while svc.store.get('preparations',id)['status']=='running':
                advance(svc,id)
                time.sleep(1)
        except Exception:
            svc.store.update('preparations',id,status='failed',message='Preparation stopped. Inspect stage receipts before creating a new plan.')


if __name__=='__main__':
    execute(sys.argv[1])
