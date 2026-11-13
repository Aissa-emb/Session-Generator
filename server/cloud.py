"""Opt-in cloud startup and single-host supervision using the existing service."""
import argparse
import copy
import fcntl
import hmac
import json
import os
from pathlib import Path
import random
import tempfile
import threading
import time

from . import secrets, symbols
from .cloud_config import CloudConfigurationError, load_config
from .locks import serialized
from .store import identifier

STATE = 'cloud-runner'
AUTHOR = 'cloud-runner'


class CloudStartError(ValueError):
    """Only messages constructed here, without raw provider exception text."""


def record(store):
    try:
        return store.get('settings', STATE)
    except KeyError:
        return None


def owned(store, *, run_id=None, stream_id=None, spec_id=None):
    state = record(store)
    if not state:
        return False
    if spec_id:
        return state.get('spec_id') == spec_id
    if stream_id:
        return state.get('stream_id') == stream_id
    if run_id:
        try:
            run = store.get('runs', run_id)
            return (run.get('cloud_owner') == state['owner'] or
                    (run.get('author') == AUTHOR and run.get('spec_id') == state.get('spec_id')))
        except KeyError:
            return False
    return False


def status(store):
    state = record(store)
    if not state:
        return {'status': 'disabled', 'message': 'No cloud runner has been configured.'}
    result = {k: state.get(k) for k in ('status', 'desired', 'message', 'owner', 'stream_id',
              'run_id', 'spec_id', 'fingerprint', 'summary', 'next_retry', 'updated')}
    result['controller_heartbeat'] = state.get('controller_heartbeat')
    result['controller_available'] = time.time() - state.get('controller_heartbeat', 0) < 30
    if state.get('run_id'):
        run = store.get('runs', state['run_id'])
        totals = run.get('delivery_totals') or {}
        result.update(worker_status=run['status'], worker_heartbeat=run.get('heartbeat'),
                      sessions_generated=run.get('counters', {}).get('sessions', 0),
                      achieved_rate_per_minute=run.get('achieved_rate_per_minute', 0),
                      sessions_acknowledged=totals.get('sessions_acknowledged', 0),
                      backend_spans_acknowledged=totals.get('backend_spans_acknowledged', 0),
                      delivery_errors=run.get('counters', {}).get('errors', 0),
                      storage='not_verified', dry_run=run['dry_run'])
        receipts = store.list_for('receipts', 'run_id', run['id'], limit=1)
        result['last_delivery_at'] = receipts[0]['created'] if receipts else None
    return result


@serialized('cloud-command')
def request_control(svc, action):
    if action not in ('pause', 'stop', 'resume'):
        raise ValueError('Choose pause, stop or resume for the cloud runner.')
    state = record(svc.store)
    if not state:
        raise ValueError('No cloud runner has been configured.')
    desired = {'pause': 'paused', 'stop': 'stopped', 'resume': 'running'}[action]
    changes = dict(desired=desired, updated=time.time())
    if action == 'resume':
        changes.update(blocked=False, message='Resume requested.', next_retry=None, retries=[], restart_requested=True)
    else:
        changes.update(message=f'{action.capitalize()} requested.')
        if state.get('run_id'):
            run = svc.store.get('runs', state['run_id'])
            if run['status'] not in ('completed', 'failed', 'stopped'):
                fields = {'desired_status': desired}
                if run['status'] == 'paused' and action == 'stop':
                    fields.update(status='stopped', ended=time.time())
                svc.store.update('runs', run['id'], **fields)
        if state.get('stream_id'):
            svc.store.update('streams', state['stream_id'], status=desired)
    svc.store.update('settings', STATE, **changes)
    return status(svc.store)


class CloudController:
    def __init__(self, svc, environ=None, dry_run=False):
        self.svc, self.store = svc, svc.store
        self.environ = os.environ if environ is None else environ
        self.dry_run = dry_run
        self.stop_event = threading.Event()
        self.thread = self.lease = None
        self.started = False

    def _update(self, **changes):
        return self.store.update('settings', STATE, updated=time.time(), **changes)

    def _ensure(self):
        return record(self.store) or self.store.put('settings', dict(
            id=STATE, owner=identifier(), desired='running', status='starting',
            blocked=False, generation=0, retries=[], updated=time.time()))

    def busy(self, run):
        process = self.svc.processes.get(run['id'])
        if process is not None and process.poll() is None:
            return True
        path = Path(run['manifest_path']).parent / 'worker.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    @serialized('cloud-command')
    def _drain(self, state, desired='stopped', flush=False):
        latest = record(self.store)
        if flush:
            desired = 'stopped'
        elif latest and latest['desired'] != 'running':
            desired = latest['desired']
        if not state.get('run_id'):
            return True
        run = self.store.get('runs', state['run_id'])
        active = self.busy(run)
        if run['status'] not in ('completed', 'failed', 'stopped'):
            changes = {'desired_status': desired}
            if not active:
                changes['status'] = desired
            self.store.update('runs', run['id'], **changes)
        return not active

    def _block(self, message, code='failure'):
        prior = record(self.store)
        if code == 'configuration' and prior.get('blocked') and prior.get('failure_code') != 'configuration':
            return  # Invalid replacement settings cannot clear an existing safety latch.
        self._update(status='blocked', blocked=True, message=message, failure_code=code, next_retry=None)

    def _same_credentials(self, spec, values):
        for kind, value in values.items():
            try:
                prior = secrets.for_spec(self.store, spec, kind)
            except (ValueError, OSError):
                return False
            if not hmac.compare_digest((value or '').encode(), (prior or '').encode()):
                return False
        return True

    def _snapshot(self, spec, credentials):
        # Immutable per-activation copies keep in-flight work bound to its original tenant/key.
        spec = copy.deepcopy(spec)
        refs = {}
        directory = self.store.directory / 'cloud-credentials'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError('Cloud credential directory must not be a symlink.')
        for kind, token in credentials.items():
            refs[kind] = None
            if not token:
                continue
            key = identifier()
            path = directory / (key + '.token')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write(token)
                out.flush()
                os.fsync(out.fileno())
            self.store.put('settings', dict(id='credential:' + key, kind=kind,
                                           ref='file:' + str(path), cloud_owner=record(self.store)['owner']))
            refs[kind] = key
        spec['saved_credentials'] = refs
        return spec

    def _prepare(self, state, config):
        self.svc.require_valid(config.spec)
        preview = self.svc.preview(config.spec)
        if any(i.get('severity') == 'error' for i in preview['linter']):
            raise ValueError('Cloud baseline failed realism validation.')
        latest = record(self.store)
        if self.stop_event.is_set() or latest['desired'] != 'running':
            self._drain(latest)
            return False
        if not self._drain(state):
            self._update(status='draining', message='Applying changed cloud settings after pending deliveries finish.')
            return False
        value = self._snapshot(config.spec, config.credentials)
        spec_id = state.get('spec_id') or identifier()
        # Persist intended IDs before side effects; interrupted initialization reuses them.
        self._update(spec_id=spec_id, status='starting')
        saved = self.store.save_spec(value, AUTHOR, spec_id)
        if not state.get('stream_id'):
            stream = self.svc.create_stream(saved['id'], 'Cloud runner', dry_run=self.dry_run,
                                           author=AUTHOR, idempotency_key='cloud:' + state['owner'])
            self._update(stream_id=stream['id'])
        self._update(fingerprint=config.fingerprint, summary=config.summary,
                     run_id=None, launch_key=None, blocked=False, message='Cloud configuration ready.')
        return True

    def _launch(self, state):
        if self.stop_event.is_set() or record(self.store)['desired'] != 'running':
            return
        spec = self.store.get('scenario_specs', state['spec_id'])['spec']
        if not self.dry_run and any(t.get('web', {}).get('symbolication') for t in spec['targets']):
            try:
                symbols.upload(self.svc, state['spec_id'], confirm=True, author=AUTHOR)
            except symbols.SymbolUploadRejected as error:
                raise CloudStartError(str(error)) from None
            except Exception:
                apps = ', '.join(t['embrace_app_id'] for t in spec['targets'] if t['platform'] == 'web')
                raise CloudStartError(f'Source-map upload failed for web app {apps}. Check the Symbol Upload key, endpoint access and upload receipts; then resume.') from None
        if self.stop_event.is_set() or record(self.store)['desired'] != 'running':
            return
        if not state.get('launch_key'):
            generation = state.get('generation', 0) + 1
            state = self._update(generation=generation, launch_key=f"cloud:{state['owner']}:{generation}")
        run = self.svc.start_run(state['spec_id'], confirm=True, dry_run=self.dry_run,
                                 author=AUTHOR, idempotency_key=state['launch_key'], launch=False)
        self.store.update('runs', run['id'], cloud_owner=state['owner'])
        self.store.update('streams', state['stream_id'], run_id=run['id'], authorized=True, status='running')
        self._update(run_id=run['id'], status='starting', launched_at=time.time(), message='Worker starting.', next_retry=None, restart_requested=False)
        self._start_worker(run)

    @serialized('cloud-command')
    def _start_worker(self, run, maintenance=False):
        # The final intent check and process launch are short and atomic with
        # operator commands. Slow preview/uploads never hold this lock.
        state = record(self.store)
        desired = state['desired']
        if self.stop_event.is_set() or desired != 'running':
            desired = desired if desired != 'running' else 'paused'
            self.store.update('runs', run['id'], status=desired, desired_status=desired)
            self.store.update('streams', state['stream_id'], status=desired)
            return
        if maintenance:
            if state.get('restart_requested'):
                return
            self.store.update('runs', run['id'], desired_status='running')
            self._update(status='starting', launched_at=time.time(), message='Worker resuming.')
        self.svc.launch(run['id'])

    def _retry(self, state, run):
        now = time.time()
        if state.get('next_retry') is None:
            attempts = [t for t in state.get('retries', []) if now - t < 900]
            if len(attempts) >= 5:
                self._block('Worker restart budget exhausted. Inspect the host and explicitly resume.', 'restart_budget')
                return
            attempts.append(now)
            self.store.update('runs', run['id'], status='failed', ended=now, recovery_required=True)
            self.store.event(run['id'], 'cloud_interrupted', message='Worker exited; uncertain live deliveries will not be replayed.')
            self._update(status='retrying', retries=attempts,
                         next_retry=now + min(300, 2 ** len(attempts)) + random.uniform(0, 1),
                         message='Worker interrupted. New work will start after backoff; prior deliveries remain uncertain.')
            return
        if now >= state['next_retry']:
            self._update(run_id=None, launch_key=None, next_retry=None)
            self._launch(record(self.store))

    @serialized('cloud-control')
    def tick(self):
        state = record(self.store)
        try:
            config = load_config(self.environ)
        except ValueError as error:
            state = self._ensure()
            self._drain(state)
            self._block(str(error), 'configuration')
            self._update(controller_heartbeat=time.time())
            return
        if not config.enabled:
            if state:
                drained = self._drain(state)
                if state.get('stream_id'):
                    self.store.update('streams', state['stream_id'], status='paused')
                self._update(status='disabled' if drained else 'draining',
                             controller_heartbeat=time.time(), message='Cloud mode disabled.')
            return
        state = self._ensure()
        self._update(controller_heartbeat=time.time())
        if state['desired'] != 'running':
            drained = self._drain(state, state['desired'])
            self._update(status=state['desired'] if drained else 'draining', message='Operator ' + state['desired'] + '.')
            return
        if state.get('blocked') and state.get('failure_code') != 'configuration':
            return  # Configuration edits do not reset circuit breakers or permanent failures.
        try:
            current = self.store.get('scenario_specs', state['spec_id'])['spec'] if state.get('fingerprint') else None
            # save_spec and the ownership checkpoint are separate transactions.
            # Verify the persisted spec too: a crash between them followed by
            # an operator config rollback must not resume the new destination.
            actual = {k: v for k, v in (current or {}).items() if k != 'saved_credentials'}
            expected = {k: v for k, v in config.spec.items() if k != 'saved_credentials'}
            changed = (current is None or state['fingerprint'] != config.fingerprint or
                       actual != expected or not self._same_credentials(current, config.credentials))
            if changed:
                if not self._prepare(state, config):
                    return
                state = record(self.store)
            if state.get('blocked') and state.get('failure_code') != 'configuration':
                return
            if state.get('failure_code') == 'configuration':
                state = self._update(blocked=False, failure_code=None)
            if not state.get('run_id'):
                self._launch(state)
                return
            run = self.store.get('runs', state['run_id'])
            busy = self.busy(run)
            if state.get('restart_requested'):
                if busy:
                    self._drain(state)
                    self._update(status='draining', message='Waiting for the previous worker before resuming.')
                    return
                self._update(restart_requested=False, run_id=None, launch_key=None)
                self._launch(record(self.store))
                return
            if busy:
                if time.time() - run.get('heartbeat', state.get('launched_at', run['created'])) > 90:
                    self._drain(state)
                    self._block('Worker heartbeat expired. Drain requested; inspect it before resuming.', 'stalled')
                else:
                    self._update(status='running' if run['status'] == 'running' else 'starting', message='Generating traffic.')
                return
            if state.get('next_retry') is not None:
                self._retry(state, run)
            elif run['status'] == 'failed':
                self._block('Worker stopped after a failure. Inspect run diagnostics and explicitly resume.', 'worker_failure')
            elif run['status'] in ('paused', 'stopped'):
                # A clean maintenance pause can continue the same counters/pool; never replay exports.
                self._start_worker(run, maintenance=True)
            elif run['status'] == 'completed':
                self._update(status='completed', desired='stopped', message='Run completed; explicit resume starts a new activation.')
            elif run['status'] == 'queued' and time.time() - state.get('launched_at', 0) < 15:
                return
            else:
                self._retry(state, run)
        except CloudStartError as error:
            self._block(str(error), 'startup')
        except Exception:
            # Provider errors may contain keys or bodies. The run/upload receipts are the evidence.
            self._block('Cloud startup failed. Check app settings, credential access, source-map uploads and run diagnostics; then resume.', 'startup')

    def start(self):
        if not self.svc.launch_jobs:
            return False
        if self.lease is not None:
            return bool(self.thread and self.thread.is_alive())
        self.lease = os.open(self.store.directory / 'cloud-controller.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lease)
            self.lease = None
            return False
        self.started = True
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    # Keep the API available for diagnosis; never automatically clear corrupt state.
                    break
                self.stop_event.wait(2)
        self.thread = threading.Thread(target=loop, name='cloud-controller', daemon=True)
        self.thread.start()
        return True

    def close(self):
        self.stop_event.set()
        if self.lease is None:
            return  # Only the controller holding ownership may drain its worker.
        state = record(self.store)
        if state:
            # Reuse the worker's stop/flush path so scheduled future payloads do
            # not consume the shutdown grace period. Durable operator intent
            # remains unchanged; a maintenance restart can continue this run.
            self._drain(state, flush=True)
        if self.thread:
            # A slow preflight/upload cannot consume the worker drain budget.
            # Every launch boundary rechecks stop_event; the daemon owns its lease
            # until it finishes or the process exits.
            self.thread.join(timeout=5)
        if self.lease is not None and (not self.thread or not self.thread.is_alive()):
            os.close(self.lease)
            self.lease = None

    def worker_busy(self):
        if not self.started:
            return False
        state = record(self.store)
        return bool(state and state.get('run_id') and self.busy(self.store.get('runs', state['run_id'])))


def main():
    parser = argparse.ArgumentParser(description='Configure, inspect or control the optional cloud runner.')
    parser.add_argument('command', choices=('check', 'status', 'pause', 'stop', 'resume'))
    parser.add_argument('--sessions', type=int, default=12, help='Finite offline check size (1–120).')
    args = parser.parse_args()
    from .service import Service
    from .store import Store
    if args.command == 'check':
        if not 1 <= args.sessions <= 120:
            parser.error('--sessions must be between 1 and 120')
        config = load_config(dict(os.environ, SG_CLOUD_RUNNER='1'))
        value = copy.deepcopy(config.spec)
        value['traffic'] = {'mode': 'burst', 'count': args.sessions, 'window': {'last_minutes': 5}}
        # A check is entirely offline, including when symbol credentials are supplied.
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                svc = Service(store, launch_jobs=False)
                result = svc.preview(value)
                if any(i.get('severity') == 'error' for i in result['linter']):
                    raise ValueError('Generated payloads failed realism validation.')
                print(json.dumps({'status': 'passed', 'delivery': 'offline', 'sessions': args.sessions,
                                  'configuration': config.summary, 'linter': result['linter']}, indent=2))
            finally:
                store.close()
    else:
        store = Store()
        try:
            svc = Service(store, launch_jobs=False)
            result = status(store) if args.command == 'status' else request_control(svc, args.command)
            print(json.dumps(result, indent=2))
        finally:
            store.close()


if __name__ == '__main__':
    try:
        main()
    except CloudConfigurationError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, OSError):
        raise SystemExit('Cloud command failed. Check the documented app IDs, credential files and local state permissions.') from None
