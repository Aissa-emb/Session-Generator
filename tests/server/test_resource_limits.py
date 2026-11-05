import fcntl
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app

from server.service import Service
from server.store import Store
from xcorgen.spec import default_spec


class ResourceLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.addCleanup(self.store.close)
        self.service = Service(self.store, launch_jobs=False)

    def test_preview_concurrency_and_timeout_release_slots(self):
        for _ in range(2):
            self.assertTrue(self.service.preview_slots.acquire(blocking=False))
        with patch('server.service.subprocess.run') as process:
            with self.assertRaisesRegex(ValueError, 'Two previews'):
                self.service.preview(default_spec())
            process.assert_not_called()
        for _ in range(2):
            self.service.preview_slots.release()
        with patch('server.service.subprocess.run', side_effect=subprocess.TimeoutExpired('preview', 120)):
            with self.assertRaisesRegex(ValueError, 'two minutes'):
                self.service.preview(default_spec())
        with patch('server.service.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}')):
            self.assertEqual(self.service.preview(default_spec()), {})

    def test_host_capacity_reserves_queued_runs_and_excludes_resuming_run(self):
        for i in range(4):
            self.store.put('runs', dict(id=str(i), status='queued' if i == 0 else 'running'))
        with self.assertRaisesRegex(ValueError, 'four active runs'):
            self.service.worker_capacity()
        self.service.worker_capacity(excluding='0')
        self.store.update('runs', '0', status='paused')
        self.service.worker_capacity()

    def test_stop_does_not_wait_for_run_start_preview_lock(self):
        self.store.put('runs', dict(id='r', status='running', desired_status='running'))
        with (Path(self.directory.name) / 'run-launch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.service.control('r', 'stop')['desired_status'], 'stopped')

    def test_request_body_timeout_returns_actionable_status(self):
        async def stalled_stream(_):
            raise TimeoutError()
            yield b''
        with TestClient(create_app(self.service)) as client, patch('starlette.requests.Request.stream', stalled_stream):
            response = client.post('/api/preview', content='{}')
        self.assertEqual(response.status_code, 408)
        self.assertIn('15 seconds', response.json()['detail'])

    def test_new_indexes_install_on_existing_database(self):
        with self.store.engine.begin() as conn:
            for index in ('receipts_run_created', 'verifications_run_created', 'api_tokens_hash', 'run_events_run_cursor'):
                conn.exec_driver_sql('DROP INDEX ' + index)
        self.store.close()
        reopened = Store(self.directory.name)
        try:
            with reopened.engine.connect() as conn:
                plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN SELECT data FROM receipts WHERE json_extract(data, '$.run_id')='r' ORDER BY created DESC LIMIT 500").all()
            self.assertIn('receipts_run_created', str(plan))
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main()
