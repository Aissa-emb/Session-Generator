import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app
from server.service import Service
from server.store import Store
from server import worker
from xcorgen import spec


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.engine.dispose)
        self.svc = Service(self.store, launch_jobs=False)
        self.client = TestClient(create_app(self.svc))
        self.addCleanup(self.client.close)
        self.spec = spec.default_spec()
        self.spec['traffic'] = {'mode': 'burst', 'count': 4, 'window': {'last_minutes': 5}}

    def save(self):
        response = self.client.post('/api/specs', json=self.spec)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_distribution_preview_and_canonical_model_roundtrip(self):
        from xcorgen.distributions import default
        config=default('reading_ms')
        with patch('xcorgen.runner.Sender', side_effect=AssertionError('Preview cannot send')):
            result=self.client.post('/api/distributions/preview',json={'quantity':'reading_ms','distribution':config})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['count'],2048)
        self.assertEqual(len(result.json()['histogram']),24)
        self.spec['modeling']={'version':'1.0','bindings':[{'id':'read','quantity':'reading_ms','distribution':config}]}
        saved=self.save()
        restored=self.client.get('/api/specs/'+saved['id']).json()['spec']
        self.assertEqual(restored['modeling'],self.spec['modeling'])
        broken=self.client.post('/api/distributions/preview',json={'quantity':'reading_ms','distribution':{'family':'constant','parameters':{'value':-100}}})
        self.assertEqual(broken.status_code,422)

    def test_versions_and_optimistic_patch(self):
        saved = self.save()
        url = '/api/specs/' + saved['id']
        response = self.client.patch(url, json={'expected_version': 1, 'patch': [{'op': 'replace', 'path': '/name', 'value': 'Vantra traffic'}]})
        self.assertEqual(response.json()['version'], 2)
        self.assertEqual(self.client.get(url + '?version=1').json()['spec']['name'], self.spec['name'])
        self.assertEqual(self.client.patch(url, json={'expected_version': 1, 'patch': []}).status_code, 422)

    def test_payload_capabilities_and_semantics_share_validation(self):
        s = copy.deepcopy(self.spec)
        s['targets'] = [copy.deepcopy(spec.WEB_TARGET)]
        s['population']['platform_mix'] = {'vantra-web': 1}
        s['client_faults'] = [{'id': 'shift', 'type': 'layout_instability', 'rate': .5, 'filters': {'browser_families': ['safari']}}]
        result = self.client.post('/api/specs/validate', json=s)
        self.assertFalse(result.json()['valid'])
        self.assertEqual(self.client.post('/api/specs', json=s).status_code, 422)
        self.assertIn('trace_caveat', self.client.get('/api/capabilities').json())

    def test_preview_and_durable_dry_run(self):
        saved = self.save()
        body = {'spec_id': saved['id'], 'confirm': True, 'dry_run': True, 'idempotency_key': 'once'}
        self.assertEqual(self.client.post('/api/runs', json=dict(body, confirm=False)).status_code, 422)
        preview = self.client.post('/api/specs/' + saved['id'] + '/preview').json()
        self.assertIn('android', preview['timelines'])
        self.assertEqual(preview['linter'], [])
        run = self.client.post('/api/runs', json=body).json()
        self.assertEqual(self.client.post('/api/runs', json=body).json()['id'], run['id'])
        worker.execute(self.store, run['id'])
        result = self.client.get('/api/runs/' + run['id']).json()
        self.assertEqual(result['status'], 'completed', self.store.events(run['id']))
        self.assertTrue(result['receipts'])
        self.assertTrue(all(r['status'] == 'accepted' for r in result['receipts']))
        self.assertEqual(result['delivery']['sessions_acknowledged'], 4)
        self.assertEqual(result['delivery']['backend_spans_acknowledged'], 0)
        self.assertEqual(result['delivery']['basis'], 'local_output')
        self.assertTrue(all(r.get('session_receipt') for r in result['receipts']))
        events = self.client.get('/api/runs/' + run['id'] + '/events?once=true')
        self.assertIn('event: progress', events.text)
        cursor = self.store.events(run['id'])[-1]['id']
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/events?once=true&after={cursor}").text, '')
        verified = self.client.post('/api/runs/' + run['id'] + '/verify').json()
        self.assertEqual(verified['embrace']['state'], 'needs_action')
        self.assertEqual(verified['chronosphere']['state'], 'sessions_only')

    def test_preview_includes_future_live_incident(self):
        s = spec.default_spec()
        s['traffic'] = {'mode': 'live', 'rate_per_minute': 4}
        s['client_faults'] = [{'id': 'crash', 'type': 'jvm_crash', 'rate': .2}]
        s['incidents'] = [{'id': 'evening', 'fault_ref': 'crash', 'start': spec.iso(time.time()*1000+180000), 'peak_minutes': 10}]
        result = self.client.post('/api/preview', json=s)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertGreater(spec.parse_time(result.json()['preview_clock']), time.time()*1000)

    def test_release_preview_projects_future_adoption_and_keeps_finished_curves_usable(self):
        self.spec['traffic']={'mode':'live','rate_per_minute':4}
        curve={'target':self.spec['targets'][0]['id'], 'start':spec.iso(time.time()*1000+120000),
               'points':[{'minute':0,'shares':{'10.28.0':10,'10.27.3':90}}, {'minute':20,'shares':{'10.28.0':90,'10.27.3':10}}]}
        self.spec['modeling']={'version':'1.0','releases':[curve]}
        response=self.client.post('/api/preview',json=self.spec)
        self.assertEqual(response.status_code,200,response.text)
        self.assertGreaterEqual(spec.parse_time(response.json()['preview_clock']),spec.parse_time(curve['start'])+1200000)
        self.assertEqual(response.json()['report']['modeling']['releases'][0]['visits'],60)
        curve['start']='2026-01-01T00:00:00Z'
        self.assertEqual(self.client.post('/api/preview',json=self.spec).status_code,200)

    def test_unreadable_live_token_is_correctable_before_any_run_is_created(self):
        from xcorgen.security import SecurityError
        self.spec['destinations'] = {'mode': 'end_to_end'}
        self.spec['chronosphere'] = {'tenant': 'demo-sandbox'}
        saved = self.save()
        body = {'spec_id': saved['id'], 'confirm': True, 'idempotency_key': 'credential-retry'}
        with patch('server.secrets.get', side_effect=SecurityError('private diagnostic')):
            response = self.client.post('/api/runs', json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn('No run was started', response.text)
        self.assertIn('Destination', response.text)
        self.assertNotIn('private diagnostic', response.text)
        self.assertEqual(self.store.list('runs'), [])
        with patch('server.secrets.get', return_value='local-fixture'):
            result = self.client.post('/api/runs', json=body)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(len(self.store.list('runs')), 1)
        # A retry recovers the accepted run even if credentials later become unavailable.
        with patch('server.secrets.get', side_effect=SecurityError('private diagnostic')):
            recovered = self.client.post('/api/runs', json=body)
        self.assertEqual(recovered.json()['id'], result.json()['id'])

    def test_invalid_spec_token_reference_cannot_start_a_live_worker(self):
        self.spec['destinations'] = {'mode': 'end_to_end'}
        self.spec['chronosphere'] = {'tenant': 'demo-sandbox', 'token_ref': 'env:SG_MISSING_FIXTURE_TOKEN'}
        saved = self.save()
        with patch('server.secrets.get', return_value=None), patch.dict(os.environ, {}, clear=True):
            response = self.client.post('/api/runs', json={'spec_id': saved['id'], 'confirm': True})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.list('runs'), [])

    def test_tokens_are_hashed_revocable_and_not_listed_in_clear(self):
        result = self.client.post('/api/settings/tokens', json={'name': 'Desktop'}).json()
        token = result['token']
        self.assertNotIn(token, json.dumps(self.store.list('api_tokens')))
        listed = self.client.get('/api/settings/tokens').json()
        self.assertNotIn('hash', listed[0])
        self.assertNotIn('token', listed[0])
        with patch.dict(os.environ, {'SG_REQUIRE_AUTH': '1'}):
            self.assertEqual(self.client.get('/api/targets').status_code, 401)
            self.assertEqual(self.client.get('/api/targets', headers={'Authorization': 'Bearer ' + token}).status_code, 200)
        self.client.delete('/api/settings/tokens/' + result['id'])
        self.assertEqual(self.client.get('/api/targets', headers={'Authorization': 'Bearer ' + token}).status_code, 401)

    def test_secret_is_write_only_and_origin_guard_prevents_csrf(self):
        value = 'private-value-' + str(time.time_ns())
        with patch('server.secrets.sys.platform', 'linux'), patch.dict(os.environ, {'SG_SECRET_DIR': self.tmp.name}):
            response = self.client.put('/api/settings/secrets/chronosphere', json={'value': value})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(value, response.text)
            self.assertNotIn(value, self.client.get('/api/settings').text)
            self.assertEqual((Path(self.tmp.name) / 'chronosphere.token').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.post('/api/specs', json=self.spec, headers={'Origin': 'https://untrusted.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/health', headers={'Host': 'untrusted.invalid'}).status_code, 400)
        self.assertEqual(self.client.post('/api/specs', content='x' * (1024 * 1024 + 1)).status_code, 413)

    def test_macos_saved_token_is_read_with_the_same_native_keychain_api(self):
        from server import secrets
        from xcorgen.security import SecurityError
        import keyring
        with patch('server.secrets.sys.platform', 'darwin'), patch.dict(os.environ, {}, clear=True), \
             patch('keyring.set_password') as save, patch('keyring.get_password', return_value='local-fixture') as get, \
             patch('server.secrets.resolve') as command_lookup:
            secrets.save(self.store, 'chronosphere', 'local-fixture')
            self.assertEqual(secrets.get(self.store, 'chronosphere'), 'local-fixture')
            save.assert_called_once_with('Session-Generator', 'chronosphere', 'local-fixture')
            get.assert_called_once_with('Session-Generator', 'chronosphere')
            command_lookup.assert_not_called()
            for result in (None, keyring.errors.KeyringError('private diagnostic')):
                get.return_value, get.side_effect = (None, result) if isinstance(result, Exception) else (result, None)
                with self.assertRaises(SecurityError) as raised:
                    secrets.get(self.store, 'chronosphere')
                self.assertNotIn('private diagnostic', str(raised.exception))

    def test_readiness_requires_evidence_and_exports_exact_service(self):
        target = self.client.post('/api/targets', json={'target': spec.WEB_TARGET}).json()
        kit = self.client.get('/api/targets/' + target['id'] + '/setup-kit').json()
        self.assertEqual(kit['forwarded_service'], 'vantra_web-web-production')
        url = '/api/targets/' + target['id'] + '/readiness'
        self.assertFalse(self.client.get(url).json()['ready'])
        self.assertEqual(self.client.put(url + '/probe', json={'state': 'ok'}).status_code, 422)
        self.assertIn('# Dashboard setup', self.client.get('/api/targets/' + target['id'] + '/setup-kit?format=md').text)

    def test_pause_then_resume_bounded_job(self):
        saved = self.save()
        run = self.svc.start_run(saved['id'], confirm=True, dry_run=True)
        self.svc.control(run['id'], 'pause')
        worker.execute(self.store, run['id'])
        self.assertEqual(self.store.get('runs', run['id'])['status'], 'paused')
        self.svc.control(run['id'], 'resume')
        worker.execute(self.store, run['id'])
        self.assertEqual(self.store.get('runs', run['id'])['status'], 'completed')

    def test_live_edits_apply_before_the_next_session_and_stop_flushes(self):
        self.spec['traffic'] = {'mode': 'live', 'rate_per_minute': 600, 'duration_minutes': 0}
        saved = self.save()
        run = self.svc.start_run(saved['id'], confirm=True, dry_run=True)
        clock = type('Clock', (), {'now': time.time(), 'ticks': 0})()
        def sleep(seconds):
            clock.now += 30
            clock.ticks += 1
            if clock.ticks == 1:
                self.svc.patch_spec(saved['id'], [{'op': 'replace', 'path': '/population/signed_in_share', 'value': .8}])
            if clock.ticks == 2:
                self.svc.control(run['id'], 'stop')
        clock.time, clock.sleep = lambda: clock.now, sleep
        with patch('server.worker.time', clock):
            worker.execute(self.store, run['id'])
        result = self.store.get('runs', run['id'])
        self.assertEqual(result['status'], 'stopped', self.store.events(run['id']))
        self.assertEqual(result['spec_version'], 2)
        self.assertGreater(result['counters']['payloads'], 0)
        self.assertEqual({e['spec_version'] for e in self.store.events(run['id']) if e['type'] == 'spec_applied'}, {1, 2})
        return result

    def test_live_model_sequence_and_evidence_continue_across_spec_edits(self):
        from xcorgen.distributions import default
        self.spec['modeling']={'version':'1.0','bindings':[{'id':'transport','quantity':'network_rtt_ms','distribution':default('network_rtt_ms')}]}
        result=self.test_live_edits_apply_before_the_next_session_and_stop_flushes()
        records=[json.loads(line) for line in Path(result['manifest_path']).read_text().splitlines()]
        records=[r for r in records if r.get('modeling')]
        self.assertEqual(sorted(r['modeling']['index'] for r in records),list(range(result['counters']['sessions'])))
        self.assertEqual(len({r['session_id'] for r in records}),len(records))

    def test_stream_controls_and_hourly_observation_comparison(self):
        saved = self.save()
        run = self.svc.start_run(saved['id'], confirm=True, dry_run=True)
        worker.execute(self.store, run['id'])
        records = [json.loads(line) for line in Path(run['manifest_path']).read_text().splitlines()]
        from collections import Counter
        hours = Counter((r['target'], int(r['start_ms'] // 3600000)) for r in records if r.get('stage') == 'sent')
        observed = {'source': 'Platform export of the known session cohort', 'session_unit': 'user_sessions',
                    'hourly_counts': [{'target': t, 'hour_start_ms': h * 3600000, 'count': c} for (t, h), c in hours.items()]}
        response = self.client.post('/api/runs/' + run['id'] + '/observations', json=observed)
        self.assertTrue(response.json()['embrace']['coverage_complete'])
        self.assertTrue(all(r['within_tolerance'] for r in response.json()['embrace']['hourly_counts']))
        self.spec['traffic'] = {'mode': 'live', 'rate_per_minute': 10, 'duration_minutes': 120}
        live = self.save()
        stream = self.client.post('/api/streams', json={'spec_id': live['id'], 'name': 'Vantra continuous', 'dry_run': True}).json()
        self.assertEqual(self.client.post('/api/streams/' + stream['id'] + '/resume', json={}).status_code, 422)
        response = self.client.post('/api/streams/' + stream['id'] + '/resume', json={'confirm': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/streams/' + stream['id'] + '/summary').status_code, 200)
        self.assertEqual(self.client.post('/api/streams/' + stream['id'] + '/pause', json={}).json()['status'], 'paused')

    def test_experimental_integrations_disabled_and_status_is_real(self):
        self.assertEqual(self.client.get('/api/status').json()['service'], 'session-generator')
        self.assertEqual(self.client.post('/mcp', json={}).status_code, 404)
        self.assertEqual(self.client.post('/api/copilot/turns', json={}).status_code, 404)
        self.assertEqual(self.client.put('/api/settings/secrets/anthropic', json={'value':'unused'}).status_code, 404)
        self.assertNotIn('anthropic', self.client.get('/api/settings').json()['secrets'])

    def test_stop_a_paused_run_without_a_worker(self):
        saved = self.save()
        run = self.svc.start_run(saved['id'], confirm=True, dry_run=True)
        self.svc.control(run['id'], 'pause')
        worker.execute(self.store, run['id'])
        self.assertEqual(self.store.get('runs', run['id'])['status'], 'paused')
        stopped = self.svc.control(run['id'], 'stop')
        self.assertEqual(stopped['status'], 'stopped')
        self.assertIsNotNone(stopped['ended'])

    def _live_with_failing_sessions(self, fails, ticks):
        """Run a dry live stream whose session generation raises on the given call numbers."""
        from xcorgen.spec_runner import SpecRun
        self.spec['traffic'] = {'mode': 'live', 'rate_per_minute': 600, 'duration_minutes': 0}
        saved = self.save()
        run = self.svc.start_run(saved['id'], confirm=True, dry_run=True)
        original, calls = SpecRun.session, []
        def session(runner, *args, **kwargs):
            calls.append(1)
            if len(calls) in fails:
                raise KeyError('api-token=do-not-record')
            return original(runner, *args, **kwargs)
        clock = type('Clock', (), {'now': time.time(), 'ticks': 0})()
        def sleep(seconds):
            clock.now += 30
            clock.ticks += 1
            if clock.ticks == ticks:
                self.svc.control(run['id'], 'stop')
        clock.time, clock.sleep = lambda: clock.now, sleep
        with patch('server.worker.time', clock), patch.object(SpecRun, 'session', session):
            worker.execute(self.store, run['id'])
        return self.store.get('runs', run['id']), self.store.events(run['id'])

    def test_one_failed_session_is_skipped_and_the_live_stream_continues(self):
        result, events = self._live_with_failing_sessions({2}, ticks=5)
        self.assertEqual(result['status'], 'stopped', events)
        self.assertEqual(result['skipped_sessions'], 1)
        self.assertGreaterEqual(result['counters']['sessions'], 4)
        self.assertEqual(result['last_skipped']['type'], 'KeyError')
        self.assertIsNone(result['last_skipped']['detail'])
        self.assertTrue(any(w.startswith('tests/server/test_api.py:') for w in result['last_skipped']['where']))
        self.assertEqual([e['type'] for e in events].count('session_skipped'), 1)
        self.assertNotIn('do-not-record', json.dumps([result, events]))

    def test_repeated_session_failures_stop_the_stream_with_a_recorded_reason(self):
        result, events = self._live_with_failing_sessions({2, 3, 4}, ticks=10)
        self.assertEqual(result['status'], 'failed', events)
        self.assertEqual(result['skipped_sessions'], 3)
        self.assertEqual(result['failure']['type'], 'JobFailure')
        self.assertIn('3 sessions in a row could not be generated', result['failure']['detail'])
        self.assertIn('KeyError at tests/server/test_api.py:', result['failure']['detail'])
        failed = next(e for e in events if e['type'] == 'failed' and 'message' in e)
        self.assertEqual(failed['detail'], result['failure']['detail'])
        self.assertNotIn('do-not-record', json.dumps([result, events]))

    def test_realism_linter_failures_name_codes_and_paths_but_not_values(self):
        issue = {'severity': 'error', 'path': '$.embrace.logs.resourceLogs[0].message', 'code': 'pii',
                 'message': 'Emitted value "555-867-5309" looks like a phone number.'}
        with patch('server.worker.lint_payloads', return_value=[issue]):
            result, events = self._live_with_failing_sessions(set(), ticks=10)
        self.assertEqual(result['status'], 'failed', events)
        self.assertIn('realism linter (pii at $.embrace.logs.resourceLogs[0].message)', result['failure']['detail'])
        self.assertNotIn('555-867-5309', json.dumps([result, events]))

    def test_live_duration_stops_generation_and_preserves_counts(self):
        self.spec['traffic'] = {'mode': 'live', 'rate_per_minute': 10, 'duration_minutes': 1}
        saved = self.save()
        run = self.svc.start_run(saved['id'], confirm=True, dry_run=True)
        clock = type('Clock', (), {'now': time.time()})()
        def advance(seconds):
            # Cross the end time after the first session, then let pending deliveries drain.
            self.assertGreaterEqual(self.store.get('runs', run['id'])['counters']['sessions'], 1)
            clock.now += 600
        clock.time, clock.sleep = lambda: clock.now, advance
        with patch('server.worker.time', clock):
            worker.execute(self.store, run['id'])
        result = self.store.get('runs', run['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['counters']['sessions'], 1)
        self.assertGreater(result['counters']['payloads'], 0)
