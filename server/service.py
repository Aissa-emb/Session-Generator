"""Validated application operations shared by REST, MCP and copilot."""
import copy
import json
import os
from pathlib import Path
import subprocess  # nosec B404 -- fixed worker modules, no shell
import sys
import time
import threading
import jsonpatch
from pydantic import ValidationError
from xcorgen import catalog, limits, linter, presets, spec as specs, web_capabilities
from xcorgen.security import SecurityError, post, tenant_url
from .scenario_models import ScenarioSpec
from .store import Store, identifier
from . import readiness, secrets
from .locks import serialized

ROOT = Path(__file__).resolve().parents[1]
MAX_INPUT = 1024 * 1024


class Service:
    def __init__(self, store=None, launch_jobs=True):
        self.store = store or Store()
        self.launch_jobs = launch_jobs
        self.processes = {}
        self.preview_slots = threading.BoundedSemaphore(2)

    def capabilities(self):
        from xcorgen.distributions import metadata
        from xcorgen.population import INTENTS
        from xcorgen.adoption import versions
        return {'release_catalog': {t['platform']: {'target': t['id'], 'versions': versions(t)} for t in (specs.ANDROID_TARGET, specs.IOS_TARGET, specs.WEB_TARGET)}, 'modeling': metadata(), 'population_model': INTENTS, 'runner_region':os.environ.get('SG_RUNNER_REGION','local'), 'regional_runners':{'plan_endpoint':'/api/regions/plan','geography':'Location comes from each deployed runner egress IP.'}, 'platforms': ['android', 'web', 'ios'], 'faults': catalog.describe(), 'limits': limits.capabilities(),
                'browser_matrix': web_capabilities.MATRIX, 'trace_caveat': limits.TRACE_CAVEAT,
                'backfill_notes': limits.BACKFILL_NOTES, 'demo_preparation': {'steps':['readiness','session history','recent traces','live stream'], 'prepare_ahead_default_minutes':120, 'recommend_prepare_ahead_after_minutes':120}, 'default_spec': specs.default_spec(),
                'web_target': specs.WEB_TARGET, 'ios_target':specs.IOS_TARGET, 'schema': json.loads((ROOT / "spec/scenario.schema.json").read_text())}

    def validate(self, value):
        if len(json.dumps(value)) > MAX_INPUT:
            raise ValueError('Scenario exceeds the 1 MiB size limit.')
        issues = specs.validate(value)
        try:
            ScenarioSpec.model_validate(value)
        except ValidationError as error:
            issues += [{'severity': 'error', 'path': '.'.join(str(p) for p in e['loc']), 'message': e['msg'], 'code': 'schema'} for e in error.errors(include_input=False)]
        return {'valid': not specs.errors(issues), 'issues': issues}

    def require_valid(self, value):
        checked = self.validate(value)
        if not checked['valid']:
            raise ValueError(json.dumps(checked['issues']))
        return specs.normalized(value)

    def create_spec(self, value, author='user'):
        return self.store.save_spec(self.require_valid(value), author)

    def patch_spec(self, id, patch, author='user', expected_version=None):
        if not isinstance(patch, list) or len(patch) > 200:
            raise ValueError('JSON Patch must contain at most 200 operations.')
        current = self.store.get('scenario_specs', id)
        try:
            updated = jsonpatch.apply_patch(current['spec'], patch, in_place=False)
        except (jsonpatch.JsonPatchException, KeyError, IndexError, TypeError):
            raise ValueError('JSON Patch does not apply to the current spec.') from None
        checked = self.require_valid(updated)
        active = [r for r in self.store.list('runs') if r['spec_id'] == id and r['status'] in ('running', 'queued', 'paused')]
        if any(r['mode'] == 'live' for r in active) and checked['traffic']['mode'] != 'live':
            raise ValueError('A running stream must keep live traffic mode; create a separate bounded scenario.')
        if any(not r['dry_run'] for r in active):
            from .symbols import ready
            ready(self.store, checked)
        return self.store.save_spec(checked, author, id, expected_version if expected_version is not None else current['version'])

    def create_target(self, value):
        value = copy.deepcopy(value)
        token = value.pop('token', None)
        if value.get('kind') == 'chronosphere':
            tenant_url(value['tenant'])
            record = {'id': value.get('id') or identifier(), 'kind': 'chronosphere', 'tenant': value['tenant']}
            if token:
                secrets.save(self.store, 'chronosphere', token)
            record['token_ref'] = 'settings:chronosphere'  # nosec B105 -- reference, never a credential
        else:
            t = value.get('target', value)
            check = specs.default_spec()
            check['targets'] = [t]
            check['population']['platform_mix'] = {t['id']: 1}
            t = self.require_valid(check)['targets'][0]
            record = {'id': t['id'], 'kind': 'embrace', 'target': t, 'last_destination': value.get('last_destination', 'sessions_only')}
            if token:
                secrets.save(self.store, 'embrace_api', token)
        return self.store.put('targets', record)

    async def test_target(self, id):
        target = self.store.get('targets', id)
        if target['kind'] == 'chronosphere':
            token = secrets.get(self.store, 'chronosphere')
            if not token:
                return {'state': 'needs_action', 'message': 'Add the Cortex XCOR token in Settings.'}
            import asyncio
            body = await asyncio.to_thread(post, tenant_url(target['tenant']) + '/data/opentelemetry/v1/traces',
                b'{"resourceSpans":[]}', {'API-Token': token, 'Content-Type': 'application/json'})
            return {'state': 'ok', 'http_status': getattr(body, 'status', 200), 'visible_telemetry_sent': False}
        token = secrets.get(self.store, 'embrace_api')
        if not token:
            return {'state': 'needs_action', 'message': 'Add an Embrace API token, or use get_app_details through Embrace MCP.', 'app_id': target['target']['embrace_app_id']}
        from .embrace import query
        return {'state': 'observed', 'results': await query(token, [('get_app_details', {'app_id': target['target']['embrace_app_id']})])}

    def preview(self, value):
        s = self.require_valid(value)
        if not self.preview_slots.acquire(blocking=False):
            raise ValueError('Two previews are already running. Wait for one to finish, then try again.')
        try:
            try:
                proc = subprocess.run([sys.executable, '-m', 'server.preview'], input=json.dumps(s), text=True,
                                      capture_output=True, cwd=ROOT, timeout=120)  # nosec B603: fixed module, JSON on stdin
            except subprocess.TimeoutExpired:
                raise ValueError('Preview exceeded two minutes. Reduce the scenario complexity and try again.') from None
            if proc.returncode:
                raise ValueError('Preview could not produce valid payloads. Review the scenario and server diagnostics.')
            return json.loads(proc.stdout)
        finally:
            self.preview_slots.release()

    def worker_capacity(self, excluding=None):
        active = [r for r in self.store.list('runs') if r['id'] != excluding and r['status'] in ('queued', 'running')]
        if len(active) >= 4:
            raise ValueError('This computer already has four active runs. Pause or stop a run before starting another.')

    @serialized("run-launch")
    def start_run(self, spec_id, mode=None, destination=None, confirm=False, dry_run=False, author='user', idempotency_key=None):
        if confirm is not True:
            raise ValueError('Starting a run requires confirm: true after reviewing its scenario, destination and size.')
        if idempotency_key:
            if not isinstance(idempotency_key, str) or len(idempotency_key) > 100:
                raise ValueError('Invalid idempotency key.')
            existing = next((r for r in self.store.list('runs') if r.get('idempotency_key') == idempotency_key and r.get('author') == author), None)
            if existing:
                if (existing['spec_id'], existing['mode'], existing['destination'], existing['dry_run']) != (spec_id, mode or existing['mode'], destination or existing['destination'], dry_run):
                    raise ValueError('Idempotency key was already used for a different request.')
                return existing
        version = self.store.get('scenario_specs', spec_id)
        spec = copy.deepcopy(version['spec'])
        if mode and mode != spec['traffic']['mode']:
            raise ValueError('Set the traffic mode and required fields on the spec before starting.')
        if destination and destination != spec['destinations']['mode']:
            raise ValueError('Save the destination on the spec before starting.')
        spec = self.require_valid(spec)
        if not dry_run and spec.get('causal', {}).get('replay_namespace'):
            raise ValueError('Exact-ID replay is restricted to local dry runs.')
        if not dry_run and any(t['platform']=='ios' and t['embrace_app_id']=='ios00' for t in spec['targets']):
            raise ValueError('Create the iOS app in Embrace and replace ios00 before live delivery.')
        if not dry_run:
            from .symbols import ready
            ready(self.store,spec)
        live = spec['traffic']['mode'] == 'live'
        active = [r for r in self.store.list('runs') if r['mode'] == 'live' and r['status'] in ('queued', 'running', 'paused')]
        for target in spec['targets']:
            if live and sum(target['id'] in r['targets'] for r in active) >= limits.MAX_STREAMS_PER_TARGET:
                raise ValueError('The maximum concurrent streams for this target has been reached.')
        self.worker_capacity()
        preview = self.preview(spec)
        if specs.errors(preview['linter']):
            raise ValueError('Realism lint blocks this run.')
        if not dry_run and spec['destinations']['mode'] == 'end_to_end':
            from xcorgen.secrets import resolve
            from xcorgen.security import validate_headers
            try:
                token = secrets.get(self.store, 'chronosphere') or resolve((spec.get('chronosphere') or {}).get('token_ref'))
                if token:
                    validate_headers({'API-Token': token})
            except SecurityError:
                raise ValueError('The Cortex XCOR token could not be read. Save it again in Destination, or check its configured token reference. No run was started.') from None
            if not token:
                raise ValueError('An end-to-end run requires the Cortex XCOR token.')
        id = identifier()
        directory = self.store.directory / 'runs' / id
        directory.mkdir(mode=0o700, parents=True)
        run = self.store.put('runs', {'id': id, 'spec_id': spec_id, 'spec_version': version['version'], 'mode': spec['traffic']['mode'],
             'destination': spec['destinations']['mode'], 'status': 'queued', 'desired_status': 'running', 'dry_run': dry_run,
             'started': None, 'ended': None, 'trace_eligible_from': None, 'manifest_path': str(directory / 'manifest.jsonl'),
             'targets': [t['id'] for t in spec['targets']], 'author': author, 'idempotency_key': idempotency_key,
             'runner_region':os.environ.get('SG_RUNNER_REGION','local'), 'report': {}, 'counters': {}, 'trace_caveat': limits.TRACE_CAVEAT})
        self.store.event(id, 'queued', caller=author)
        for t in spec['targets']:
            try:
                self.store.update('targets', t['id'], last_destination=spec['destinations']['mode'])
            except KeyError:
                pass
        if self.launch_jobs:
            self.launch(id)
        return run

    def launch(self, id):
        if id in self.processes and self.processes[id].poll() is None:
            return
        self.processes = {key: process for key, process in self.processes.items() if process.poll() is None}
        env = dict(os.environ, SG_DATA_DIR=str(self.store.directory))
        self.processes[id] = subprocess.Popen([sys.executable, '-m', 'server.worker', id], cwd=ROOT,
            env=env)  # nosec B603: trusted worker and validated identifier

    def control(self, id, action, author='user'):
        if action == 'resume':
            return self._resume_control(id, author)
        return self._control(id, action, author)

    @serialized("run-launch")
    def _resume_control(self, id, author):
        return self._control(id, 'resume', author)

    def _control(self, id, action, author='user'):
        if action not in ('stop', 'pause', 'resume'):
            raise ValueError('Unknown run action.')
        run = self.store.get('runs', id)
        if action == 'resume' and run['status'] == 'completed':
            return run
        if action != 'resume' and run['status'] in ('completed', 'failed', 'stopped'):
            return run
        if action == 'resume':
            self.worker_capacity(excluding=id)
        desired = {'stop': 'stopped', 'pause': 'paused', 'resume': 'running'}[action]
        changes = {'desired_status': desired}
        if action == 'resume' and run['status'] != 'running':
            changes['status'] = 'queued'
        # A paused worker has drained and exited; no process remains to observe stop.
        if action == 'stop' and run['status'] == 'paused':
            changes.update(status='stopped', ended=time.time())
        run = self.store.update('runs', id, **changes)
        self.store.event(id, action + '_requested', caller=author)
        if action == 'resume' and self.launch_jobs:
            self.launch(id)
        return run

    def schedule_incident(self, run_id, incident, author='user'):
        run = self.store.get('runs', run_id)
        return self.patch_spec(run['spec_id'], [{'op': 'add', 'path': '/incidents/-', 'value': incident}], author)

    def cancel_incident(self, run_id, incident_id, author='user'):
        run = self.store.get('runs', run_id)
        current = self.store.get('scenario_specs', run['spec_id'])
        index = next((i for i, item in enumerate(current['spec']['incidents']) if item['id'] == incident_id), None)
        if index is None:
            raise KeyError('Incident was not found')
        return self.patch_spec(run['spec_id'], [{'op': 'remove', 'path': f'/incidents/{index}'}], author, current['version'])

    @serialized('stream-create')
    def create_stream(self, spec_id, name, confirm=False, dry_run=False, author='user', idempotency_key=None):
        if idempotency_key:
            if not isinstance(idempotency_key,str) or len(idempotency_key)>100:
                raise ValueError('Invalid idempotency key.')
            prior=next((s for s in self.store.list('streams') if s.get('idempotency_key')==idempotency_key and s.get('author')==author),None)
            if prior:
                if (prior['spec_id'],prior['name'],prior['dry_run']) != (spec_id,name,dry_run):
                    raise ValueError('Idempotency key belongs to a different stream request.')
                return prior
        saved = self.store.get('scenario_specs', spec_id)
        if saved['spec']['traffic']['mode'] != 'live':
            raise ValueError('A stream requires a live scenario.')
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError('Stream name must be 1–100 characters.')
        run = self.start_run(spec_id, confirm=True, dry_run=dry_run, author=author, idempotency_key='stream:'+idempotency_key if idempotency_key else None) if confirm is True else None
        return self.store.put('streams', {'name': name, 'spec_id': spec_id, 'run_id': run['id'] if run else None,
            'targets': [t['id'] for t in saved['spec']['targets']], 'status': 'running' if run else 'paused',
            'dry_run': dry_run, 'authorized': confirm is True, 'author': author, 'idempotency_key':idempotency_key})

    def control_stream(self, id, action, confirm=False, author='user'):
        stream = self.store.get('streams', id)
        if action == 'resume':
            if not (stream['authorized'] or confirm is True):
                raise ValueError('Starting this stream requires confirm: true.')
            run = self.store.get('runs', stream['run_id']) if stream['run_id'] else None
            if not run or run['status'] in ('completed', 'stopped'):
                run = self.start_run(stream['spec_id'], confirm=True, dry_run=stream['dry_run'], author=author)
            else:
                run = self.control(run['id'], 'resume', author)
            return self.store.update('streams', id, run_id=run['id'], status='running', authorized=True)
        if action not in ('pause', 'stop'):
            raise ValueError('Unknown stream action.')
        if stream['run_id']:
            self.control(stream['run_id'], action, author)
        return self.store.update('streams', id, status='paused' if action == 'pause' else 'stopped')

    def stream_summary(self, id, day=None):
        from datetime import datetime, timezone
        stream = self.store.get('streams', id)
        day = day or datetime.now(timezone.utc).date().isoformat()
        datetime.strptime(day, '%Y-%m-%d')
        runs = [r for r in self.store.list('runs') if r['spec_id'] == stream['spec_id']]
        run_ids = {r['id'] for r in runs}
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
        receipts = [r for rid in run_ids for r in self.store.list_for('receipts', 'run_id', rid, created_from=start) if r['created'] < start + 86400]
        current = self.store.get('runs', stream['run_id']) if stream['run_id'] else None
        health = 'paused' if stream['status'] == 'paused' else 'idle'
        if current and stream['status'] == 'running':
            health = 'healthy' if current['status'] == 'running' and time.time() - current.get('heartbeat', 0) < 90 else current['status']
            if current['status'] == 'running' and time.time() - current.get('heartbeat', 0) >= 90:
                health = 'unresponsive'
        return {'stream_id': id, 'day': day, 'time_zone': 'UTC', 'health': health, 'payloads': len(receipts),
                'accepted_records': sum(r.get('accepted', 0) for r in receipts), 'rejected_records': sum(r.get('rejected', 0) for r in receipts),
                'delivery_errors': sum(r['status'] != 'accepted' for r in receipts), 'run': current}

    def forwarding_probe(self, target_id, spec_id, confirm=False, dry_run=False, author='user'):
        if confirm is not True:
            raise ValueError('A forwarding probe sends telemetry and requires confirm: true.')
        target = self.store.get('targets', target_id)
        s = copy.deepcopy(self.store.get('scenario_specs', spec_id)['spec'])
        if target['kind'] != 'embrace' or target_id not in {t['id'] for t in s['targets']}:
            raise ValueError('The selected scenario must include this app target.')
        s['targets'] = [target['target']]
        s['population']['platform_mix'] = {target_id: 1}
        s['destinations']['mode'] = 'end_to_end'
        s['traffic'] = {'mode': 'burst', 'count': 6, 'window': {'last_minutes': 2}}
        s['incidents'] = []
        s['client_faults'] = []
        s['backend_faults'] = []
        s['rollouts'] = []
        saved = self.create_spec(s, author)
        run = self.start_run(saved['id'], confirm=True, dry_run=dry_run, author=author)
        self.store.update('runs', run['id'], probe_target=target_id)
        return dict(run, verify_after=time.time() + limits.FORWARDING_REFRESH_MS / 1000,
                    next_action='Wait five minutes, verify known trace IDs, then confirm Forwarded on the Cortex XCOR RUM request.')
