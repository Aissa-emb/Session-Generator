"""Cloud ownership and operator intent must survive API process lifetimes.

These tests use the real SQLite store and Service, but never launch telemetry
workers or make external requests. Payload/platform tests live separately.
"""
import json
import fcntl
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from server.cloud import CloudController, request_control, status
from server.service import Service
from server.store import Store
from xcorgen.spec import default_spec


class CloudLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {'SG_SECRET_DIR': self.tmp.name + '/secrets'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.svc = Service(self.store, launch_jobs=False)
        self.preview = patch.object(self.svc, 'preview', return_value={'linter': []}).start()
        self.addCleanup(patch.stopall)
        self.launch = patch.object(self.svc, 'launch').start()
        self.env = {'SG_CLOUD_RUNNER': '1', 'SG_WEB_APP_ID': 'web12'}
        self.controller = self.controller_for(self.env)

    def controller_for(self, env, dry_run=True):
        controller = CloudController(self.svc, environ=env, dry_run=dry_run)
        self.addCleanup(controller.close)
        return controller

    def only_stream(self):
        streams = self.store.list('streams')
        self.assertEqual(len(streams), 1, streams)
        return streams[0]

    def current_run(self):
        return self.store.get('runs', self.only_stream()['run_id'])

    def mark_running(self):
        run = self.current_run()
        return self.store.update('runs', run['id'], status='running', started=time.time(), heartbeat=time.time())

    def test_opt_in_is_required_even_when_app_ids_are_present(self):
        self.controller_for({'SG_WEB_APP_ID': 'web12'}).tick()
        self.assertEqual(self.store.list('runs'), [])
        self.assertEqual(self.store.list('streams'), [])
        self.launch.assert_not_called()

    def test_service_with_job_launch_disabled_cannot_start_controller_thread(self):
        self.assertFalse(self.controller.start())
        self.assertIsNone(self.controller.thread)
        self.assertEqual(self.store.list('runs'), [])

    def test_controller_lease_allows_only_one_background_supervisor(self):
        self.svc.launch_jobs = True
        first, second = self.controller_for(dict(self.env)), self.controller_for(dict(self.env))
        entered = threading.Event()
        with patch.object(first, 'tick', side_effect=entered.set), patch.object(second, 'tick') as second_tick:
            self.assertTrue(first.start())
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(first.start())  # Repeated setup must retain its original lease.
            self.assertFalse(second.start())
            second_tick.assert_not_called()
            first.close()
            self.assertTrue(second.start())
            second.close()

    def test_losing_controller_lease_cannot_drain_the_active_owner_on_close(self):
        self.controller.tick()
        run = self.mark_running()
        self.svc.launch_jobs = True
        other = self.controller_for(dict(self.env))
        worker_lock = Path(run['manifest_path']).parent / 'worker.lock'
        with worker_lock.open('a') as lock, patch.object(self.controller, 'tick'):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.controller.start())
            self.assertFalse(other.start())
            other.close()
            self.assertEqual(self.current_run()['desired_status'], 'running')
            self.assertEqual(self.current_run()['status'], 'running')

    def test_concurrent_reconciliation_creates_one_stream_and_run(self):
        controllers = [self.controller_for(dict(self.env)) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda controller: controller.tick(), controllers))
        self.only_stream()
        self.assertEqual(len(self.store.list('runs')), 1)
        self.assertEqual(len(self.store.list('scenario_specs')), 1)

    def test_crash_after_stream_write_recovers_ownership_without_duplicate(self):
        create = self.svc.create_stream

        def crash_after_write(*args, **kwargs):
            create(*args, **kwargs)
            raise SystemExit('simulated process death')

        with patch.object(self.svc, 'create_stream', side_effect=crash_after_write):
            with self.assertRaises(SystemExit):
                self.controller.tick()
        self.controller_for(dict(self.env)).tick()
        self.only_stream()
        self.assertEqual(len(self.store.list('runs')), 1)

    def test_crash_after_changed_spec_write_and_config_rollback_restores_original_destination(self):
        self.controller.tick()
        original_spec_id = status(self.store)['spec_id']
        save = self.store.save_spec

        def crash_after_spec_write(*args, **kwargs):
            save(*args, **kwargs)
            raise SystemExit('simulated process death before fingerprint commit')

        changed = self.controller_for(dict(self.env, SG_WEB_APP_ID='web99'))
        with patch.object(self.store, 'save_spec', side_effect=crash_after_spec_write):
            with self.assertRaises(SystemExit):
                changed.tick()
        self.assertEqual(self.store.get('scenario_specs', original_spec_id)['spec']['targets'][0]['embrace_app_id'], 'web99')

        # Rolling deployment inputs back must not launch the partially written
        # new destination just because the old fingerprint still matches.
        self.launch.reset_mock()
        self.controller_for(dict(self.env)).tick()
        state = status(self.store)
        restored = self.store.get('scenario_specs', state['spec_id'])['spec']
        self.assertEqual(restored['targets'][0]['embrace_app_id'], 'web12')
        self.assertEqual(state['spec_id'], original_spec_id)
        self.only_stream()
        self.launch.assert_called_once_with(state['run_id'])

    def test_crash_after_run_write_recovers_same_unlaunched_run(self):
        update = self.store.update

        def crash_before_ownership(table, row_id, **changes):
            if table == 'runs' and 'cloud_owner' in changes:
                raise SystemExit('simulated process death')
            return update(table, row_id, **changes)

        with patch.object(self.store, 'update', side_effect=crash_before_ownership):
            with self.assertRaises(SystemExit):
                self.controller.tick()
        orphan = self.store.list('runs')[0]
        self.launch.assert_not_called()
        self.controller_for(dict(self.env)).tick()
        self.assertEqual(self.current_run()['id'], orphan['id'])
        self.assertEqual(len(self.store.list('runs')), 1)

    def test_initialization_and_repeated_startup_reuse_one_owned_stream(self):
        self.controller.tick()
        initial = self.only_stream()
        self.controller.tick()
        self.controller_for(dict(self.env)).tick()
        self.assertEqual(self.only_stream()['id'], initial['id'])
        self.assertEqual(self.only_stream()['run_id'], initial['run_id'])
        self.assertEqual(len(self.store.list('runs')), 1)
        self.assertEqual(len(self.store.list('scenario_specs')), 1)
        saved = self.store.get('scenario_specs', initial['spec_id'])['spec']
        self.assertEqual(saved['traffic']['mode'], 'live')
        self.assertEqual(saved['traffic']['duration_minutes'], 0)
        self.assertEqual(saved['targets'][0]['embrace_app_id'], 'web12')

    def test_run_pause_survives_controller_restart(self):
        self.controller.tick()
        run = self.mark_running()
        self.svc.control(run['id'], 'pause')
        self.store.update('runs', run['id'], status='paused')
        self.launch.reset_mock()
        self.controller_for(dict(self.env)).tick()
        self.assertEqual(self.current_run()['desired_status'], 'paused')
        self.assertEqual(self.current_run()['status'], 'paused')
        self.assertEqual(len(self.store.list('runs')), 1)
        self.launch.assert_not_called()

    def test_operator_stop_wins_race_with_maintenance_resume(self):
        self.controller.tick()
        run = self.current_run()
        self.store.update('runs', run['id'], status='paused', desired_status='running')
        self.launch.reset_mock()

        def stop_after_controller_read(worker):
            # Reproduce a stop arriving after tick read running intent but
            # before it decides to restart a cleanly paused worker.
            request_control(self.svc, 'stop')
            return False

        with patch.object(self.controller, 'busy', side_effect=stop_after_controller_read):
            self.controller.tick()
        self.assertEqual(status(self.store)['desired'], 'stopped')
        self.assertEqual(self.current_run()['desired_status'], 'stopped')
        self.launch.assert_not_called()

    def test_stream_stop_survives_controller_restart(self):
        self.controller.tick()
        stream = self.only_stream()
        run = self.mark_running()
        self.svc.control_stream(stream['id'], 'stop')
        self.store.update('runs', run['id'], status='stopped', ended=time.time())
        self.launch.reset_mock()
        self.controller_for(dict(self.env)).tick()
        self.assertEqual(self.current_run()['desired_status'], 'stopped')
        self.assertEqual(self.only_stream()['status'], 'stopped')
        self.assertEqual(len(self.store.list('runs')), 1)
        self.launch.assert_not_called()

    def test_resume_is_explicit_and_waits_for_previous_sender_to_exit(self):
        self.controller.tick()
        run = self.mark_running()
        request_control(self.svc, 'stop')
        worker_lock = Path(run['manifest_path']).parent / 'worker.lock'
        with worker_lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            request_control(self.svc, 'resume')
            self.launch.reset_mock()
            self.controller.tick()
            self.assertEqual(len(self.store.list('runs')), 1)
            self.assertEqual(self.current_run()['id'], run['id'])
            self.launch.assert_not_called()
        self.store.update('runs', run['id'], status='stopped', ended=time.time())
        self.controller.tick()
        self.assertNotEqual(self.current_run()['id'], run['id'])
        self.assertEqual(len(self.store.list('runs')), 2)

    def test_resume_after_initial_configuration_failure_does_not_restart_new_worker(self):
        self.controller_for(dict(self.env, SG_WEB_APP_ID='invalid-id')).tick()
        request_control(self.svc, 'resume')
        self.controller.tick()
        run = self.mark_running()
        worker_lock = Path(run['manifest_path']).parent / 'worker.lock'
        with worker_lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.controller.tick()
            self.assertEqual(self.current_run()['desired_status'], 'running')
            self.assertEqual(self.current_run()['id'], run['id'])
            self.assertEqual(len(self.store.list('runs')), 1)

    def test_stop_during_run_preflight_returns_promptly_and_prevents_worker_launch(self):
        entered, release = threading.Event(), threading.Event()
        calls = 0

        def preview(_):
            nonlocal calls
            calls += 1
            # First preview validates the generated config; the second is the
            # ordinary Service.start_run preflight immediately before launch.
            if calls == 2:
                entered.set()
                self.assertTrue(release.wait(timeout=5))
            return {'linter': []}

        with patch.object(self.svc, 'preview', side_effect=preview), ThreadPoolExecutor(max_workers=2) as executor:
            tick = executor.submit(self.controller.tick)
            try:
                self.assertTrue(entered.wait(timeout=2))
                command = executor.submit(request_control, self.svc, 'stop')
                self.assertEqual(command.result(timeout=1)['desired'], 'stopped')
            finally:
                release.set()
            tick.result(timeout=3)
        self.launch.assert_not_called()
        self.assertTrue(all(run['desired_status'] == 'stopped' for run in self.store.list('runs')))
        self.controller.tick()
        self.assertEqual(status(self.store)['status'], 'stopped')

    def test_stop_during_changed_config_preflight_is_not_overwritten_by_maintenance_pause(self):
        self.controller.tick()
        run = self.mark_running()
        changed = self.controller_for(dict(self.env, SG_RATE_PER_MINUTE='20'))
        entered, release = threading.Event(), threading.Event()

        def preview(_):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return {'linter': []}

        worker_lock = Path(run['manifest_path']).parent / 'worker.lock'
        with worker_lock.open('a') as lock, patch.object(self.svc, 'preview', side_effect=preview), ThreadPoolExecutor(max_workers=2) as executor:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            tick = executor.submit(changed.tick)
            try:
                self.assertTrue(entered.wait(timeout=2))
                command = executor.submit(request_control, self.svc, 'stop')
                self.assertEqual(command.result(timeout=1)['desired'], 'stopped')
            finally:
                release.set()
            tick.result(timeout=3)
            self.assertEqual(self.current_run()['desired_status'], 'stopped')
            self.assertEqual(len(self.store.list('runs')), 1)

    def test_unexpected_exit_uses_backoff_new_run_and_a_persistent_retry_budget(self):
        self.controller.tick()
        now = time.time()
        initial = self.only_stream()
        for attempt in range(5):
            run = self.mark_running()
            self.store.update('runs', run['id'], counters={'sessions': attempt + 1})
            with patch('server.cloud.time.time', return_value=now), patch('server.cloud.random.uniform', return_value=0):
                self.controller.tick()
                self.assertEqual(status(self.store)['status'], 'retrying')
                retry_at = status(self.store)['next_retry']
                self.assertGreater(retry_at, now)
                self.launch.reset_mock()
                self.controller_for(dict(self.env)).tick()
                self.launch.assert_not_called()
            with patch('server.cloud.time.time', return_value=retry_at + 1):
                self.controller.tick()
            self.assertNotEqual(self.current_run()['id'], run['id'])
            self.assertEqual(self.store.get('runs', run['id'])['counters']['sessions'], attempt + 1)
            now = retry_at + 2
        self.mark_running()
        self.launch.reset_mock()
        with patch('server.cloud.time.time', return_value=now):
            self.controller.tick()
        self.assertEqual(status(self.store)['status'], 'blocked')
        self.assertIn('budget', status(self.store)['message'])
        self.assertEqual(self.only_stream()['id'], initial['id'])
        self.assertEqual(len(self.store.list('runs')), 6)
        self.launch.assert_not_called()

    def test_disabled_runner_drains_without_app_ids_or_keys(self):
        self.controller.tick()
        run = self.mark_running()
        disabled = self.controller_for({'SG_CLOUD_RUNNER': '0', 'SG_SYMBOL_UPLOAD_TOKEN_FILE': '/does/not/exist'})
        disabled.tick()
        self.assertIn(self.current_run()['desired_status'], ('paused', 'stopped'))
        self.store.update('runs', run['id'], status=self.current_run()['desired_status'])
        self.launch.reset_mock()
        disabled.tick()
        self.assertEqual(len(self.store.list('runs')), 1)
        self.launch.assert_not_called()

    def test_invalid_new_inputs_never_resume_the_old_destination(self):
        self.controller.tick()
        run = self.current_run()
        self.store.update('runs', run['id'], status='paused', desired_status='running')
        invalid = self.controller_for(dict(self.env, SG_WEB_APP_ID='invalid-id'))
        self.launch.reset_mock()
        invalid.tick()
        self.assertEqual(len(self.store.list('runs')), 1)
        self.assertEqual(self.current_run()['status'], 'stopped')
        self.launch.assert_not_called()
        self.assertEqual(status(self.store)['status'], 'blocked')

    def test_cloud_stop_does_not_touch_unrelated_ui_jobs(self):
        self.controller.tick()
        cloud_run = self.mark_running()
        spec = default_spec()
        spec['traffic'] = {'mode': 'live', 'rate_per_minute': 1}
        saved = self.svc.create_spec(spec)
        ui_stream = self.svc.create_stream(saved['id'], 'UI-owned run', confirm=True, dry_run=True)
        request_control(self.svc, 'stop')
        self.assertEqual(self.store.get('runs', cloud_run['id'])['desired_status'], 'stopped')
        self.assertEqual(self.store.get('runs', ui_stream['run_id'])['desired_status'], 'running')
        self.assertEqual(self.store.get('streams', ui_stream['id'])['status'], 'running')

    def test_delivery_circuit_failure_is_latched_instead_of_retried(self):
        self.controller.tick()
        run = self.current_run()
        self.store.update('runs', run['id'], status='failed', ended=time.time(),
                          failure={'type': 'JobFailure', 'detail': '10 consecutive delivery failures stopped this stream.', 'where': []})
        self.launch.reset_mock()
        self.controller.tick()
        self.controller_for(dict(self.env)).tick()
        self.assertEqual(len(self.store.list('runs')), 1)
        self.assertEqual(self.current_run()['status'], 'failed')
        self.assertEqual(status(self.store)['status'], 'blocked')
        self.launch.assert_not_called()

    def test_rate_edit_does_not_clear_a_latched_delivery_failure(self):
        self.controller.tick()
        run = self.current_run()
        self.store.update('runs', run['id'], status='failed', ended=time.time(),
                          failure={'type': 'JobFailure', 'detail': '10 consecutive delivery failures stopped this stream.', 'where': []})
        self.controller.tick()
        self.launch.reset_mock()
        self.controller_for(dict(self.env, SG_RATE_PER_MINUTE='20')).tick()
        self.assertEqual(status(self.store)['status'], 'blocked')
        self.assertEqual(len(self.store.list('runs')), 1)
        self.launch.assert_not_called()

    def test_stale_heartbeat_does_not_replace_a_worker_that_still_holds_its_lock(self):
        self.controller.tick()
        run = self.mark_running()
        self.store.update('runs', run['id'], heartbeat=1)
        worker_lock = Path(run['manifest_path']).parent / 'worker.lock'
        with worker_lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.launch.reset_mock()
            self.controller.tick()
            self.controller_for(dict(self.env)).tick()
            self.assertEqual(len(self.store.list('runs')), 1)
            self.assertEqual(self.current_run()['id'], run['id'])
            self.launch.assert_not_called()

    def test_configuration_change_waits_for_old_worker_before_changing_destination(self):
        self.controller.tick()
        initial = self.only_stream()
        run = self.mark_running()
        changed = self.controller_for(dict(self.env, SG_WEB_APP_ID='web34'))
        worker_lock = Path(run['manifest_path']).parent / 'worker.lock'
        with worker_lock.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            changed.tick()
            self.assertIn(self.current_run()['desired_status'], ('paused', 'stopped'))
            self.assertEqual(len(self.store.list('runs')), 1)
            old = self.store.spec_version(run['spec_id'], run['spec_version'])['spec']
            self.assertEqual(old['targets'][0]['embrace_app_id'], 'web12')
        self.store.update('runs', run['id'], status='stopped', ended=time.time())
        changed.tick()
        self.assertEqual(self.only_stream()['id'], initial['id'])
        self.assertNotEqual(self.current_run()['id'], run['id'])
        latest = self.store.get('scenario_specs', self.only_stream()['spec_id'])['spec']
        self.assertEqual(latest['targets'][0]['embrace_app_id'], 'web34')
        self.assertEqual(len(self.store.list('runs')), 2)

    def test_public_status_does_not_disclose_credential_values(self):
        secret = 'private-cloud-token-value'
        controller = self.controller_for(dict(self.env, SG_CHRONOSPHERE_TENANT='cloud-fixture', SG_CHRONOSPHERE_TOKEN=secret))
        controller.tick()
        summary = json.dumps(status(self.store))
        self.assertNotIn(secret, summary)
        self.assertNotIn('TOKEN_FILE', summary)
        self.assertNotIn(self.tmp.name, summary)

    def test_source_maps_upload_before_run_and_cached_restart_sends_none(self):
        from server import symbols
        from xcorgen import web_symbols
        original_upload = web_symbols.upload
        posts = []

        def transport(url, body, headers):
            # No worker can exist until all requested maps have been accepted.
            self.assertEqual(self.store.list('runs'), [])
            posts.append(url)
            return b''

        def upload(asset, app_id, token):
            self.assertEqual(app_id, 'web12')
            return original_upload(asset, app_id, token, transport=transport)

        env = dict(self.env, SG_SYMBOL_UPLOAD_TOKEN='private-symbol-key')
        with patch('xcorgen.web_symbols.upload', side_effect=upload):
            self.controller_for(env, dry_run=False).tick()
            self.assertGreater(len(posts), 0)
            initial_posts = len(posts)
            saved = self.store.get('scenario_specs', self.only_stream()['spec_id'])['spec']
            symbols.ready(self.store, saved)
            self.controller_for(dict(env), dry_run=False).tick()
            self.assertEqual(len(posts), initial_posts)
            self.assertEqual(len(self.store.list('runs')), 1)

    def test_rejected_symbol_key_blocks_start_and_identifies_app_without_secret(self):
        from xcorgen import web_symbols
        from xcorgen.security import TransportError
        original_upload = web_symbols.upload

        def reject(url, body, headers):
            raise TransportError('remote error body may contain private-symbol-key', status=403)

        with patch('xcorgen.web_symbols.upload', side_effect=lambda asset, app_id, token: original_upload(asset, app_id, token, transport=reject)):
            self.controller_for(dict(self.env, SG_SYMBOL_UPLOAD_TOKEN='private-symbol-key'), dry_run=False).tick()
        self.assertEqual(self.store.list('runs'), [])
        summary = status(self.store)
        self.assertEqual(summary['status'], 'blocked')
        self.assertIn('web12', summary['message'])
        self.assertIn('403', summary['message'])
        self.assertNotIn('private-symbol-key', json.dumps(summary))
        self.launch.assert_not_called()

    def test_cloud_api_obeys_existing_bearer_authentication(self):
        from fastapi.testclient import TestClient
        from server.app import create_app
        self.controller.tick()
        token = self.store.create_token('cloud operator')['token']
        with patch.dict(os.environ, {'SG_REQUIRE_AUTH': '1'}):
            client = TestClient(create_app(self.svc))
            self.addCleanup(client.close)
            self.assertEqual(client.get('/api/cloud/status').status_code, 401)
            self.assertEqual(client.post('/api/cloud/stop').status_code, 401)
            headers = {'Authorization': 'Bearer ' + token}
            self.assertEqual(client.get('/api/cloud/status', headers=headers).status_code, 200)
            response = client.post('/api/cloud/pause', headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['desired'], 'paused')

    def test_api_startup_does_not_generically_resume_cloud_work_when_disabled(self):
        from fastapi.testclient import TestClient
        from server.app import create_app
        self.controller.tick()
        cloud_run = self.current_run()
        self.store.update('runs', cloud_run['id'], status='paused', desired_status='running')
        spec = default_spec()
        spec['traffic'] = {'mode': 'live', 'rate_per_minute': 1}
        saved = self.svc.create_spec(spec)
        ui_stream = self.svc.create_stream(saved['id'], 'UI-owned recovery', confirm=True, dry_run=True)
        self.svc.launch_jobs = True
        self.launch.reset_mock()
        with patch.dict(os.environ, {'SG_CLOUD_RUNNER': '0'}):
            with TestClient(create_app(self.svc)) as client:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and status(self.store)['status'] != 'disabled':
                    time.sleep(.01)
                self.assertEqual(client.get('/api/cloud/status').json()['status'], 'disabled')
                self.launch.assert_called_once_with(ui_stream['run_id'])
                self.assertEqual(self.store.get('runs', cloud_run['id'])['desired_status'], 'stopped')
