"""Isolated job process. No UI or concurrent job shares the engine's random state."""
import copy
from collections import deque
import math
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
import traceback
from xcorgen import limits, spec as specs
from xcorgen.journal import RunJournal
from xcorgen.linter import lint_payloads
from xcorgen.runner import Sender, append_manifest, load_config
from xcorgen.spec_runner import SpecRun, curve_factor, expected_trace_ids
from .store import Store, identifier
from . import secrets, delivery_summary


class JobPaused(Exception):
    pass


class JobStopped(Exception):
    pass


class JobFailure(ValueError):
    """A failure whose message is written in this module and is safe to show and log."""


ROOT = Path(__file__).resolve().parents[1]
# A live stream skips a session that cannot be generated cleanly; it stops when this many fail in a row.
MAX_CONSECUTIVE_SKIPS = 3


def failure_reason(error):
    """Exception class and repository frames only: other messages can echo credentials or remote bodies."""
    where = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(ROOT) and '.venv' not in path.parts:
            where.append(f'{path.relative_to(ROOT)}:{frame.lineno} in {frame.name}')
    return {'type': type(error).__name__, 'detail': str(error) if isinstance(error, JobFailure) else None,
            'where': where[-4:]}


def lint_failure(issues):
    """Issue codes and payload paths, never the flagged values."""
    paths = sorted({i['path'] for i in issues})
    return JobFailure('Generated session failed the realism linter (' + ', '.join(sorted({i['code'] for i in issues}))
                      + ' at ' + ', '.join(paths[:3]) + (f' and {len(paths) - 3} more' if len(paths) > 3 else '') + ').')


def config(store, spec, dry):
    cfg = load_config(os.environ.get('SG_CONFIG_FILE'))
    if spec['destinations']['mode'] == 'sessions_only' or dry:
        cfg['chronosphere'] = {'tenant': None, 'api_token': None}  # nosec B105: disabled destination
        cfg['otlp'] = {'endpoint': None, 'headers': {}}
    else:
        ch = spec.get('chronosphere') or {}
        cfg['chronosphere']['tenant'] = ch.get('tenant')
        if 'saved_credentials' in spec:
            cfg['chronosphere']['api_token'] = secrets.for_spec(store, spec, 'chronosphere')
        else:
            cfg['chronosphere']['api_token'] = secrets.get(store, 'chronosphere') or cfg['chronosphere'].get('api_token')
            if ch.get('token_ref') and not cfg['chronosphere']['api_token']:
                from xcorgen.secrets import resolve
                cfg['chronosphere']['api_token'] = resolve(ch['token_ref'])
    return cfg


class ReceiptSender(Sender):
    def __init__(self, store, run, spec, callback=None, writer=None):
        self.store, self.job, self.callback, self.writer = store, run, callback, writer
        super().__init__(config(store, spec, run['dry_run']), str(Path(run['manifest_path']).parent / 'payloads') if run['dry_run'] else None,
                         workers=min(64, max(8, math.ceil(spec['traffic'].get('rate_per_minute', 0) / 12))))
        self.strict_receipts = True

    def _send(self, delivery):
        receipt = super()._send(delivery)
        row = dict(receipt, run_id=self.job['id'], delivery_id=delivery.get('_delivery_id'),
                   session_receipt=delivery.get('_session_receipt'), platform=delivery.get('_platform'),
                   spec_version=delivery.get('_spec_version', self.job['spec_version']))
        if self.callback:
            row['_sessions_acknowledged'] = self.callback(delivery, receipt) or 0
        if self.writer:
            self.writer.put(row)
        else:
            from .acknowledgements import write
            write(self.store, row)
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
    last_progress = 0
    def progress(force=False):
        nonlocal last_progress
        now = time.time()
        if force or now - last_progress >= 1:
            store.update('runs', job['id'], counters=dict(sender.stats), heartbeat=now)
            store.event(job['id'], 'progress', counters=dict(sender.stats))
            last_progress = now
    def check_control():
        control(store, job['id'])
        progress()
    run.check_control = check_control
    original_record = run.record
    def record(target, session, faults, deliveries):
        original_record(target, session, faults, deliveries)
        delivery_summary.annotate(deliveries, {
            'target': target['id'], 'platform': target['platform'], 'app_id': session.app_id,
            'device_id': getattr(session, 'device_id', session.persona['device_id']),
            'session_id': session.user_session_id,
            'sdk_sessions': run.emitted.last_observation.get('sdk_sessions', [])})
    run.record = record
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
        progress()
    run._deliver = deliver
    try:
        n, _ = run.run_batch(sender, quiet=True)
        store.update('runs', job['id'], sessions=n, counters=dict(sender.stats), report=run.emitted.summary())
        if sender.stats['errors']:
            raise JobFailure('One or more deliveries failed; inspect receipts before resume.')
    finally:
        sender.close()
        progress(force=True)
        run.journal.close()


MAX_QUEUED_BYTES = 256 * 1024 * 1024
MAX_PENDING_SESSIONS = 2048
MAX_SCHEDULE_LAG = 30


class DeliveryHealth:
    def __init__(self):
        self.recent = deque(maxlen=200)
        self.consecutive = 0

    def add(self, failed):
        self.recent.append(bool(failed))
        self.consecutive = self.consecutive + 1 if failed else 0

    def failure(self):
        if self.consecutive >= 10:
            return '10 consecutive delivery failures stopped this stream.'
        if len(self.recent) == 200 and sum(self.recent) >= 20:
            return 'At least 20 of the last 200 deliveries failed, so the stream stopped.'
        return None


def live(store, job, version):
    from .receipt_writer import ReceiptWriter
    heap, sequence, senders, pending = [], itertools.count(), [], {}
    lock = threading.Lock()
    counters = {'sessions': 0, 'payloads': 0, 'accepted': 0, 'rejected': 0, 'errors': 0, 'platforms': {}}
    counters.update(copy.deepcopy(job.get('counters') or {}))
    # Anchor scheduling to elapsed time, so clock corrections do not accumulate drift.
    monotonic = getattr(time, 'monotonic', time.time)
    wall_origin, mono_origin = time.time(), monotonic()
    def clock():
        return wall_origin + monotonic() - mono_origin
    payload_dir = Path(job['manifest_path']).parent / 'payloads'
    dry_counter = itertools.count(max((int(p.name.split('-', 1)[0]) for p in payload_dir.glob('*.json')
                                       if p.name.split('-', 1)[0].isdigit()), default=0) + 1)
    current, runner, sender = None, None, None
    next_start = wall_origin
    started_at = job.get('started') or next_start
    stop = False
    generation_end = None
    skipped, consecutive = job.get('skipped_sessions', 0), 0
    queued_bytes = 0
    arrivals = deque()
    health = DeliveryHealth()
    last_publish = last_report = last_retention = 0
    capacity_reason, capacity_until = None, 0
    missed = job.get('missed_arrivals', 0)
    if 'delivery_totals' not in job:
        totals = delivery_summary.summarize(store, job)
        store.update('runs', job['id'], delivery_totals={k: totals[k] for k in ('sessions_acknowledged', 'backend_spans_acknowledged')})
    writer = ReceiptWriter(store, job['id'])

    def receipt(d, r):
        acknowledged = 0
        with lock:
            failed = r['status'] not in ('accepted', 'expired')
            health.add(failed)
            counters['payloads'] += 1
            counters['accepted'] += r['accepted']
            counters['rejected'] += r['rejected']
            counters['errors'] += failed
            p = counters['platforms'].setdefault(d['_platform'], {'payloads': 0, 'accepted': 0, 'rejected': 0, 'errors': 0})
            for field, value in (('payloads', 1), ('accepted', r['accepted']), ('rejected', r['rejected']), ('errors', failed)):
                p[field] += value
            session = pending[d['_session_key']]
            session['remaining'] -= 1
            session['ok'] &= not failed
            if r['target'] == 'embrace':
                session['app_remaining'] -= 1
                session['app_ok'] &= r['status'] == 'accepted' and not r['rejected']
                if not session['app_remaining'] and session['app_ok']:
                    acknowledged = len(d.get('_session_receipt', {}).get('keys', []))
            if r['status'] == 'expired' and r['target'] == 'otlp' and r['kind'] == 'traces':
                session['record']['trace_ids'] = sorted(set(session['record']['trace_ids']) - set(r.get('trace_ids', [])))
            if r['status'] == 'expired' and r['target'] == 'embrace':
                session['app_expired'] = True
            if session['remaining'] == 0:
                from .retention import rotate_manifest
                rotate_manifest(job['manifest_path'])
                append_manifest(job['manifest_path'], dict(session['record'], stage='partially_expired' if session.get('app_expired') else 'sent' if session['ok'] else 'delivery_failed', sent_ms=time.time() * 1000))
                del pending[d['_session_key']]
        return acknowledged

    def publish(force=False):
        nonlocal last_publish, last_report, last_retention
        now = clock()
        if not force and now - last_publish < 1:
            return
        rate_at = generation_end if generation_end is not None else now
        while arrivals and arrivals[0] < rate_at - 60:
            arrivals.popleft()
        changes = dict(heartbeat=time.time(), achieved_rate_per_minute=round(len(arrivals) * 60 / max(1, min(60, rate_at - wall_origin)), 1),
                       capacity_limited=now < capacity_until, capacity_reason=capacity_reason if now < capacity_until else None,
                       missed_arrivals=missed, missed_arrivals_estimated=True, queued_payloads=len(heap), queued_bytes=round(queued_bytes))
        with lock:
            changes['counters'] = copy.deepcopy(counters)
        if runner and (force or now - last_report >= 5):
            runner.save()
            changes.update(report=runner.emitted.summary(), report_scope='current process segment of scenario version ' + str(current))
            last_report = now
        store.update('runs', job['id'], **changes)
        store.event(job['id'], 'progress', counters=changes['counters'])
        last_publish = now
        if force or now - last_retention >= 5:
            store.retain_live_history(job['id'])
            from .retention import trim_payloads
            if job['dry_run']:
                trim_payloads(Path(job['manifest_path']).parent / 'payloads')
            if Path(job['manifest_path'] + '.1').exists():
                store.update('runs', job['id'], history_scope='retained recent live history', history_truncated=True)
            last_retention = now

    try:
        while True:
            writer.check()
            if any(getattr(s, 'task_error', None) for s in senders):
                raise JobFailure('Delivery bookkeeping failed; inspect recorded receipts before continuing.')
            now = clock()
            state = store.get('runs', job['id'])
            desired = state['desired_status']
            stop |= desired == 'stopped'
            latest = store.get('scenario_specs', job['spec_id'])
            duration = latest['spec']['traffic'].get('duration_minutes', 0)
            expired = bool(duration and now >= started_at + duration * 60)
            if desired == 'running' and not stop and not expired:
                generation_end = None
            if generation_end is None and (stop or expired or desired == 'paused'):
                generation_end = min(now, started_at + duration * 60) if expired else now
            # Always service due exports before more generation. A full sender never blocks
            # the control loop: stop/pause/spec changes remain observable under backpressure.
            pass_started = clock()
            while heap and (stop or heap[0][0] <= clock() * 1000):
                _, _, owner, d = heap[0]
                if owner.submit(d, block=False) is None:
                    if now - d['due'] / 1000 > 2:
                        capacity_reason, capacity_until = 'sender_saturation', now + 60
                    break
                heapq.heappop(heap)
                queued_bytes = max(0, queued_bytes - d['_queue_bytes'])
                owner.queued -= 1
                if clock() - pass_started >= .5:
                    break
            for owner in list(senders):
                if owner is not sender and not owner.queued and not owner.inflight:
                    owner.close()
                    senders.remove(owner)
            if desired == 'paused' and not heap and not any(s.inflight for s in senders):
                raise JobPaused()
            with lock:
                failure = health.failure()
            if failure:
                capacity_reason, capacity_until = 'delivery_errors', now + 60
                raise JobFailure(failure)
            count = 0
            while desired == 'running' and not stop and not expired and clock() >= next_start and count < 32 and clock() - pass_started < .5:
                # Controls and the latest immutable spec are re-read before *every* session.
                state = store.get('runs', job['id'])
                desired = state['desired_status']
                latest = store.get('scenario_specs', job['spec_id'])
                now = clock()
                duration = latest['spec']['traffic'].get('duration_minutes', 0)
                if desired != 'running' or (duration and now >= started_at + duration * 60):
                    break
                if now - next_start > MAX_SCHEDULE_LAG:
                    rate = latest['spec']['traffic']['rate_per_minute'] * curve_factor(latest['spec'], now)
                    missed += max(1, round((now - next_start) * rate / 60))
                    next_start = now
                    capacity_reason, capacity_until = 'generation_cpu', now + 60
                if len(pending) >= MAX_PENDING_SESSIONS or queued_bytes >= MAX_QUEUED_BYTES:
                    capacity_reason, capacity_until = 'sender_saturation', now + 60
                    break
                if current != latest['version']:
                    # Bound overlapping destination versions, each of which must drain its
                    # original exports before its threads/connections can be retired.
                    if len(senders) >= 4:
                        capacity_reason, capacity_until = 'sender_saturation', now + 60
                        break
                    if runner is not None:
                        runner.save()
                        reports = store.get('runs', job['id']).get('reports_by_version', {})
                        reports[str(current)] = runner.emitted.summary()
                        reports = dict(list(reports.items())[-32:])
                        store.update('runs', job['id'], reports_by_version=reports)
                    version = latest
                    cfg = config(store, latest['spec'], job['dry_run'])
                    if not job['dry_run'] and latest['spec'].get('causal', {}).get('replay_namespace'):
                        raise JobFailure('Exact-ID replay is restricted to local dry runs.')
                    runner = SpecRun(latest['spec'], cfg, persist=True, state_dir=str(store.directory / 'personas' / job['id']), now_ms=now * 1000)
                    if latest['spec'].get('causal'):
                        runner._causal_index = counters['sessions']
                        runner.run_id = latest['spec']['causal'].get('replay_namespace') or job['id']
                    if latest['spec'].get('modeling'):
                        runner._model_index = counters['sessions']
                        runner.run_id = job['id']
                    sender = ReceiptSender(store, dict(job, spec_version=latest['version']), latest['spec'], receipt, writer)
                    sender.queued = 0
                    sender.counter = dry_counter
                    senders.append(sender)
                    current = latest['version']
                    store.update('runs', job['id'], spec_version=current, destination=latest['spec']['destinations']['mode'], trace_eligible_from=state.get('trace_eligible_from') or (now if latest['spec']['destinations']['mode'] == 'end_to_end' else None))
                    store.event(job['id'], 'spec_applied', spec_version=current)
                rate = latest['spec']['traffic']['rate_per_minute'] * curve_factor(latest['spec'], next_start)
                next_start += random.expovariate(max(.001, rate) / 60)
                count += 1
                runner.now_ms = now * 1000
                try:
                    target, session, _, deliveries, faults = runner.session(now * 1000)
                    if not deliveries:
                        raise JobFailure('Generated session contained no payloads.')
                    issues = specs.errors(lint_payloads(deliveries))
                    if issues:
                        raise lint_failure(issues)
                    consecutive = 0
                except Exception as error:
                    # Nothing from this session is sent. One bad draw must not end a long stream,
                    # but repeated failures mean the scenario itself is broken.
                    session, reason = None, failure_reason(error)
                    skipped, consecutive = skipped + 1, consecutive + 1
                    store.update('runs', job['id'], skipped_sessions=skipped, last_skipped=dict(reason, time=now), heartbeat=now)
                    store.event(job['id'], 'session_skipped', skipped=skipped, detail=reason['detail'],
                                error_type=reason['type'], error_where=reason['where'])
                    if consecutive >= MAX_CONSECUTIVE_SKIPS:
                        last = reason['detail'] or reason['type'] + ' at ' + (reason['where'] or ['an unknown location'])[-1]
                        raise JobFailure(f'{consecutive} sessions in a row could not be generated, so the stream stopped. '
                                         f'Last: {last}') from error
                if session is not None:
                    size = sum(len(json.dumps(d, separators=(',', ':'))) for d in deliveries)
                    if queued_bytes + size > MAX_QUEUED_BYTES:
                        missed += 1
                        capacity_reason, capacity_until = 'sender_saturation', now + 60
                        continue
                    runner.record(target, session, faults, deliveries)
                    key = identifier()
                    record = {'index': counters['sessions'], 'run_id': job['id'], 'dry_run': job['dry_run'], 'target': target['id'], 'platform': target['platform'],
                              'app_id': session.app_id, 'session_id': session.user_session_id, 'device_id': getattr(session, 'device_id', session.persona['device_id']),
                              'start_ms': session.start_ms, 'end_ms': session.end_ms, 'line_of_business': session.properties['business_unit'],
                              'faults': faults, 'traces': session.traces, 'trace_ids': expected_trace_ids(session, deliveries),
                              'sdk_sessions': runner.emitted.last_observation.get('sdk_sessions', []),
                              'spec_version': current, 'part_ids': getattr(session,'sdk_session_ids',[]) + [p.id for p in getattr(session, 'parts', [])]}
                    if hasattr(session, 'causal_evidence'):
                        record['causal'] = session.causal_evidence
                    if hasattr(session, 'performance_model'):
                        record['modeling'] = session.performance_model.evidence()
                    pending[key] = {'remaining': len(deliveries), 'ok': True, 'record': record,
                                    'app_remaining': sum(d['target'] == 'embrace' for d in deliveries), 'app_ok': True}
                    queued_bytes += size
                    delivery_summary.annotate(deliveries, record)
                    for ordinal, d in enumerate(deliveries):
                        d.update(_session_key=key, _platform=target['platform'], _spec_version=current, _delivery_id=key + ':' + str(ordinal))
                        d['_queue_bytes'] = size / max(1, len(deliveries))
                        heapq.heappush(heap, (d['due'], next(sequence), sender, d))
                        sender.queued += 1
                    with lock:
                        counters['sessions'] += 1
                    arrivals.append(now)
            publish()
            if (stop or expired) and not heap and not any(s.inflight for s in senders):
                break
            wake = [clock() + .2]
            if desired == 'running' and not stop and not expired:
                wake.append(next_start)
            if heap:
                wake.append(heap[0][0] / 1000)
            time.sleep(max(.01, min(.2, min(wake) - clock())))
    finally:
        try:
            for owner in senders:
                owner.close()
            writer.close()
            if any(getattr(s, 'task_error', None) for s in senders):
                raise JobFailure('Delivery bookkeeping failed; inspect recorded receipts before continuing.')
        finally:
            publish(force=True)
    if stop:
        raise JobStopped()


def _execute(store, id):
    job = store.get('runs', id)
    version = store.spec_version(job['spec_id'], job['spec_version'])
    random.seed(version['spec'].get('seed'))
    start = job.get('started') or time.time()
    store.update('runs', id, status='running', started=start, ended=None, heartbeat=time.time(), failure=None,
                 trace_eligible_from=start if job['destination'] == 'end_to_end' else None)
    store.event(id, 'running', spec_version=version['version'])
    try:
        (live if job['mode'] == 'live' else bounded)(store, job, version)
        status = 'completed'
    except JobPaused:
        status = 'paused'
    except JobStopped:
        status = 'stopped'
    except Exception as error:
        # Transport and provider exceptions can contain credentials or response bodies, so only
        # this module's own messages, the exception class and repository frames are recorded.
        status, failure = 'failed', failure_reason(error)
        store.update('runs', id, failure=failure)
        store.event(id, 'failed', message='The job failed. Inspect validation results and delivery receipts; credentials and remote error bodies are suppressed.',
                    detail=failure['detail'], error_type=failure['type'], error_where=failure['where'])
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
    try:
        execute(store, id)
    finally:
        store.close()
