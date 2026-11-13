"""Kubernetes probe contracts; no telemetry is sent by these checks."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server import cloud
from server.service import Service
from server.store import Store


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.service = Service(self.store, launch_jobs=False)

    def test_lifecycle_and_database_failure_have_separate_probe_outcomes(self):
        app = create_app(self.service)
        idle_client = TestClient(app)
        self.addCleanup(idle_client.close)
        self.assertEqual(idle_client.get('/readyz').status_code, 503)
        self.assertEqual(idle_client.get('/livez').status_code, 200)
        with TestClient(app) as client:
            self.assertEqual(client.get('/livez').json(), {'status': 'alive'})
            self.assertEqual(client.get('/readyz').json(), {'status': 'ready'})
            with patch.object(self.store.engine, 'connect', side_effect=RuntimeError('private-database-value')):
                self.assertEqual(client.get('/livez').status_code, 200)
                response = client.get('/readyz')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {'status': 'not_ready'})
                self.assertNotIn('private-database-value', response.text)
        # A draining/stopped lifespan cannot claim local serving readiness.
        self.assertEqual(idle_client.get('/readyz').status_code, 503)
        self.assertEqual(idle_client.get('/livez').status_code, 200)

    def test_probes_preserve_host_origin_and_api_authentication_guards(self):
        with patch.dict(os.environ, {'SG_REQUIRE_AUTH': '1'}), TestClient(create_app(self.service)) as client:
            for path in ('/livez', '/readyz'):
                self.assertEqual(client.get(path, headers={'Host': '10.0.0.23:8000'}).status_code, 400)
                response = client.get(path, headers={'Host': 'localhost'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()), {'status'})
                self.assertEqual(client.get(path, headers={'Origin': 'https://untrusted.invalid'}).status_code, 403)
            self.assertEqual(client.get('/api/cloud/status').status_code, 401)
            self.assertEqual(client.post('/api/cloud/stop').status_code, 401)

    def test_readiness_checks_owning_controller_not_generation_outcomes(self):
        self.service.launch_jobs = True
        # Missing app IDs exercise the real controller's blocked configuration
        # path without credentials, workers or external requests.
        with patch.dict(os.environ, {'SG_CLOUD_RUNNER': '1'}, clear=True):
            app = create_app(self.service)
            with TestClient(app) as client:
                controller = app.state.cloud_controller
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    state = cloud.record(self.store)
                    if state and state.get('status') == 'blocked':
                        break
                    time.sleep(.01)
                self.assertEqual(cloud.record(self.store)['status'], 'blocked')
                self.assertEqual(client.get('/readyz').status_code, 200)
                for desired in ('paused', 'stopped'):
                    self.store.update('settings', cloud.STATE, desired=desired)
                    self.assertEqual(client.get('/readyz').status_code, 200)
                    self.assertEqual(client.get('/livez').status_code, 200)
                with patch.object(controller.thread, 'is_alive', return_value=False):
                    self.assertEqual(client.get('/readyz').status_code, 503)
                    self.assertEqual(client.get('/livez').status_code, 200)
                with patch.object(controller, 'lease', None):
                    self.assertEqual(client.get('/readyz').status_code, 503)
                self.assertEqual(client.get('/readyz').status_code, 200)

    def test_normal_ui_mode_is_ready_with_cloud_generation_disabled(self):
        self.service.launch_jobs = True
        with patch.dict(os.environ, {'SG_CLOUD_RUNNER': '0'}, clear=True):
            with TestClient(create_app(self.service)) as client:
                self.assertEqual(client.get('/readyz').status_code, 200)
                self.assertEqual(client.get('/livez').status_code, 200)
                self.assertEqual(client.get('/api/cloud/status').json()['status'], 'disabled')
                self.assertEqual(self.store.list('runs'), [])


if __name__ == '__main__':
    unittest.main()
