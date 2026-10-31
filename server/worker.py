"""Isolated job process. No UI or concurrent job shares the engine's random state."""
import copy
import heapq
import itertools
import json
import os
from pathlib import Path
import random
import signal
import sys
import threading
import time
from xcorgen import limits, spec as specs
from xcorgen.journal import RunJournal
from xcorgen.linter import lint_payloads
from xcorgen.runner import Sender, append_manifest, load_config
from xcorgen.spec_runner import SpecRun, curve_factor, expected_trace_ids
from .store import Store, identifier
from . import secrets


class JobPaused(Exception):
    pass


class JobStopped(Exception):
    pass


def config(store, spec, dry):
    cfg = load_config(os.environ.get('SG_CONFIG_FILE'))
    if spec['destinations']['mode'] == 'sessions_only' or dry:
        cfg['chronosphere'] = {'tenant': None, 'api_token': None}  # nosec B105: disabled destination
        cfg['otlp'] = {'endpoint': None, 'headers': {}}
    else:
        ch = spec.get('chronosphere') or {}
        cfg['chronosphere']['tenant'] = ch.get('tenant')
        cfg['chronosphere']['api_token'] = secrets.get(store, 'chronosphere') or cfg['chronosphere'].get('api_token')
        if ch.get('token_ref') and not cfg['chronosphere']['api_token']:
            from xcorgen.secrets import resolve
            cfg['chronosphere']['api_token'] = resolve(ch['token_ref'])
    return cfg


class ReceiptSender(Sender):
    def __init__(self, store, run, spec, callback=None):
        self.store, self.job, self.callback = store, run, callback
        super().__init__(config(store, spec, run['dry_run']), str(Path(run['manifest_path']).parent / 'payloads') if run['dry_run'] else None)
        self.strict_receipts = True

    def _send(self, delivery):
        receipt = super()._send(delivery)
        self.store.put('receipts', dict(receipt, run_id=self.job['id'], delivery_id=delivery.get('_delivery_id'),
                                      platform=delivery.get('_platform'), spec_version=delivery.get('_spec_version', self.job['spec_version'])))
        if self.callback:
            self.callback(delivery, receipt)
        return receipt


def control(store, id):
    desired = store.get('runs', id)['desired_status']
    if desired == 'paused':
        raise JobPaused()
    if desired == 'stopped':
        raise JobStopped()


def bounded(store, job, version):
    directory = Path(job['manifest_path']).parent
    resume = (directory / 'run.sqlite3').exists()
    now = None
    if resume:
        j = RunJournal(directory)
        now = j.get('plan')['now_ms']
        j.close()
    run = SpecRun(version['spec'], config(store, version['spec'], job['dry_run']), persist=not job['dry_run'],
                  state_dir=str(store.directory / 'personas' / job['id']), now_ms=now)
    run.open_manifest(str(directory), resume=resume, dry=job['dry_run'])
    sender = ReceiptSender(store, job, version['spec'])
    original = run._deliver
    published_report = False
    def deliver(sender, d):
        nonlocal published_report
        control(store, job['id'])
        if not published_report:
            store.update('runs', job['id'], report=run.emitted.summary())
            published_report = True
        original(sender, d)
        store.update('runs', job['id'], counters=dict(sender.stats), heartbeat=time.time())
        store.event(job['id'], 'progress', counters=dict(sender.stats))
    run._deliver = deliver
    try:
        n, _ = run.run_batch(sender, quiet=True)
        store.update('runs', job['id'], sessions=n, counters=dict(sender.stats), report=run.emitted.summary())
        if sender.stats['errors']:
            raise ValueError('One or more deliveries failed; inspect receipts before resume.')
    finally:
        sender.close()
        run.journal.close()


def live(store, job, version):
    heap, sequence, senders, pending = [], itertools.count(), [], {}
    lock = threading.Lock()
    counters = {'sessions': 0, 'payloads': 0, 'accepted': 0, 'rejected': 0, 'errors': 0, 'platforms': {}}
    counters.update(copy.deepcopy(job.get('counters') or {}))
    current, runner, sender = None, None, None
    next_start = time.time()
    started_at = job.get('started') or next_start
    stop = False
    def receipt(d, r):
        with lock:
            counters['payloads'] += 1
            counters['accepted'] += r['accepted']
            counters['rejected'] += r['rejected']
            counters['errors'] += r['status'] not in ('accepted','expired')
            p = counters['platforms'].setdefault(d['_platform'], {'payloads': 0, 'accepted': 0, 'rejected': 0, 'errors': 0})
            p['payloads'] += 1
            p['accepted'] += r['accepted']
            p['rejected'] += r['rejected']
            p['errors'] += r['status'] not in ('accepted','expired')
            session = pending[d['_session_key']]
            session['remaining'] -= 1
            session['ok'] &= r['status'] in ('accepted','expired')
            if r['status']=='expired' and r['target']=='otlp' and r['kind']=='traces':
                session['record']['trace_ids']=sorted(set(session['record']['trace_ids'])-set(r.get('trace_ids',[])))
            if r['status']=='expired' and r['target']=='embrace':
                session['app_expired']=True
            if session['remaining'] == 0:
                append_manifest(job['manifest_path'], dict(session['record'], stage='partially_expired' if session.get('app_expired') else 'sent' if session['ok'] else 'delivery_failed', sent_ms=time.time() * 1000))
                del pending[d['_session_key']]
            store.update('runs', job['id'], counters=copy.deepcopy(counters), heartbeat=time.time())
            store.event(job['id'], 'progress', counters=copy.deepcopy(counters))
    try:
        while True:
            state = store.get('runs', job['id'])
            now = time.time()
            desired = state['desired_status']
            stop = stop or desired == 'stopped'
            latest = store.get('scenario_specs', job['spec_id'])
            duration = latest['spec']['traffic'].get('duration_minutes', 0)
            expired = bool(duration and now >= started_at + duration * 60)
            if desired == 'paused' and not heap:
                for s in senders:
                    s.close()
                raise JobPaused()
            if desired == 'running' and not stop and not expired and now >= next_start:
                if current != latest['version']:
                    if runner is not None:
                        reports=store.get('runs',job['id']).get('reports_by_version',{})
                        reports[str(current)]=runner.emitted.summary()
                        store.update('runs',job['id'],reports_by_version=reports)
                    version = latest
                    cfg = config(store, latest['spec'], job['dry_run'])
                    runner = SpecRun(latest['spec'], cfg, persist=True, state_dir=str(store.directory / 'personas' / job['id']), now_ms=now * 1000)
                    sender = ReceiptSender(store, dict(job, spec_version=latest['version']), latest['spec'], receipt)
                    senders.append(sender)
                    current = latest['version']
                    store.update('runs', job['id'], spec_version=current, destination=latest['spec']['destinations']['mode'], trace_eligible_from=state.get('trace_eligible_from') or (now if latest['spec']['destinations']['mode']=='end_to_end' else None))
                    store.event(job['id'], 'spec_applied', spec_version=current)
                runner.now_ms = now * 1000
                target, session, _, deliveries, faults = runner.session(now * 1000)
                issues = specs.errors(lint_payloads(deliveries))
                if issues:
                    raise ValueError('Generated session failed the realism linter.')
                runner.record(target, session, faults, deliveries)
                key = identifier()
                record = {'index': counters['sessions'], 'run_id': job['id'], 'target': target['id'], 'platform': target['platform'],
                          'app_id': session.app_id, 'session_id': session.user_session_id, 'device_id': getattr(session, 'device_id', session.persona['device_id']),
                          'start_ms': session.start_ms, 'end_ms': session.end_ms, 'line_of_business': session.properties['business_unit'],
                          'faults': faults, 'traces': session.traces, 'trace_ids': expected_trace_ids(session, deliveries),
                          'sdk_sessions': runner.emitted.observations[-1].get('sdk_sessions', []),
                          'spec_version': current, 'part_ids': getattr(session,'sdk_session_ids',[]) + [p.id for p in getattr(session, 'parts', [])]}
                pending[key] = {'remaining': len(deliveries), 'ok': True, 'record': record}
                for ordinal, d in enumerate(deliveries):
                    d.update(_session_key=key, _platform=target['platform'], _spec_version=current, _delivery_id=key + ':' + str(ordinal))
                    heapq.heappush(heap, (d['due'], next(sequence), sender, d))
                with lock:
                    counters['sessions'] += 1
                    store.update('runs', job['id'], counters=copy.deepcopy(counters), heartbeat=now)
                runner.save()
                store.update('runs', job['id'], report=runner.emitted.summary(), report_scope='current process segment of scenario version '+str(current), heartbeat=now)
                rate = latest['spec']['traffic']['rate_per_minute'] * curve_factor(latest['spec'], now)
                next_start = now + random.expovariate(max(.001, rate) / 60)
            while heap and (stop or heap[0][0] <= time.time() * 1000):
                _, _, owner, d = heapq.heappop(heap)
                owner.submit(d)
            if (stop or expired) and not heap:
                break
            if counters['errors'] >= 20:
                raise ValueError('Repeated delivery failures stopped this stream.')
            store.update('runs', job['id'], heartbeat=time.time())
            time.sleep(.2)
    finally:
        for sender in senders:
            sender.close()
    if stop:
        raise JobStopped()


def _execute(store, id):
    job = store.get('runs', id)
    version = store.spec_version(job['spec_id'], job['spec_version'])
    random.seed(version['spec'].get('seed'))
    start = job.get('started') or time.time()
    store.update('runs', id, status='running', started=start, ended=None, heartbeat=time.time(),
                 trace_eligible_from=start if job['destination'] == 'end_to_end' else None)
    store.event(id, 'running', spec_version=version['version'])
    try:
        (live if job['mode'] == 'live' else bounded)(store, job, version)
        status = 'completed'
    except JobPaused:
        status = 'paused'
    except JobStopped:
        status = 'stopped'
    except Exception:
        # Transport and provider exceptions can contain credentials or response bodies.
        status = 'failed'
        store.event(id, 'failed', message='The job failed. Inspect validation results and delivery receipts; credentials and remote error bodies are suppressed.')
    store.update('runs', id, status=status, ended=time.time() if status in ('completed', 'stopped', 'failed') else None, heartbeat=time.time())
    store.event(id, status)


def execute(store, id):
    import fcntl
    job = store.get('runs', id)
    path = Path(job['manifest_path']).parent / 'worker.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if store.get('runs', id)['status'] == 'completed':
            return
        _execute(store, id)
    finally:
        os.close(fd)


if __name__ == '__main__':
    store = Store()
    id = sys.argv[1]
    signal.signal(signal.SIGINT, lambda *_: store.update('runs', id, desired_status='stopped'))
    signal.signal(signal.SIGTERM, lambda *_: store.update('runs', id, desired_status='stopped'))
    execute(store, id)
