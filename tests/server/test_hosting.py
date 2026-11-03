import io
import json
import logging
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.service import Service
from server.store import Store
from server.app import create_app
from server import audit
from xcorgen.spec import default_spec


class HostingTests(unittest.TestCase):
    def test_only_authorized_active_streams_restart(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d)
            try:
                svc=Service(store,False);s=default_spec();s['traffic']={'mode':'live','rate_per_minute':1}
                saved=svc.create_spec(s)
                active=svc.create_stream(saved['id'],'Active',confirm=True,dry_run=True)
                paused=svc.create_stream(saved['id'],'Paused',confirm=True,dry_run=True)
                svc.control_stream(paused['id'],'pause')
                svc.launch_jobs=True
                with patch.object(svc,'launch') as launch:
                    with TestClient(create_app(svc)):
                        launch.assert_called_once_with(active['run_id'])
                self.assertEqual(store.get('runs',active['run_id'])['desired_status'],'paused')
                self.assertEqual(store.get('streams',active['id'])['status'],'running')
            finally:store.engine.dispose()

    def test_structured_logs_drop_arbitrary_fields_and_bodies(self):
        capture=io.StringIO();handler=logging.StreamHandler(capture);audit.logger.addHandler(handler)
        try:
            audit.emit('delivery',run_id='cohort',payloads=2,authorization='private-value',body={'key':'private-value'})
            row=json.loads(capture.getvalue())
            self.assertEqual(row['payloads'],2);self.assertNotIn('private-value',capture.getvalue())
        finally:audit.logger.removeHandler(handler)
