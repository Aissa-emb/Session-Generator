import os
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xcorgen.payload_spool import PayloadSpool
from xcorgen.runner import load_config
from xcorgen.spec import default_spec
from xcorgen.spec_runner import SpecRun


class PayloadSpoolTests(unittest.TestCase):
    def test_backend_first_stable_order_and_private_cleanup(self):
        with PayloadSpool() as spool:
            path = spool.path
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            for target, due, tag in [('embrace', 1, 'a'), ('otlp', 3, 'b'), ('otlp', 2, 'c'), ('otlp', 2, 'd')]:
                spool.add(dict(target=target, due=due, payload={'tag': tag}))
            self.assertEqual([d['payload']['tag'] for d in spool], ['c', 'd', 'b', 'a'])
        self.assertFalse(os.path.exists(path))

    def test_disk_and_staging_limits_fail_before_delivery(self):
        with PayloadSpool() as spool:
            with patch.object(spool, 'MAX_BYTES', 1), self.assertRaisesRegex(ValueError, 'Split this run'):
                spool.add(dict(target='embrace', due=1, payload={}))
        with PayloadSpool() as spool, patch('xcorgen.payload_spool.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ValueError, 'free disk space'):
                spool.add(dict(target='embrace', due=1, payload={}))

    def test_invalid_last_session_prevents_every_post(self):
        scenario = default_spec()
        scenario['traffic'] = dict(mode='backfill', count=2, window={'last_minutes': 720})
        with tempfile.TemporaryDirectory() as directory:
            run = SpecRun(scenario, load_config(None), persist=False, state_dir=directory, rng=random.Random(71))
            sender = Mock()
            invalid = [{'severity': 'error', 'code': 'forbidden', 'path': 'payload', 'message': 'Forbidden text'}]
            with patch('xcorgen.linter.lint_payloads', side_effect=[[], invalid]):
                with self.assertRaisesRegex(ValueError, 'no payloads were sent'):
                    run.run_batch(sender, quiet=True)
            sender.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
