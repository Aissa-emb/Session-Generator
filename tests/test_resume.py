import copy
import json
import os
import random
import tempfile
import unittest
from unittest.mock import patch
from xcorgen import spec
from xcorgen.journal import RunJournal
from xcorgen.runner import Sender, load_config
from xcorgen.spec_runner import SpecRun
from xcorgen.security import TransportError


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name
        self.s = spec.default_spec()
        self.s['traffic'] = {'mode': 'backfill', 'count': 8, 'window': {'last_minutes': 720}}
        self.cfg = load_config(None)

    def make(self, resume=False):
        random.seed(71 if not resume else 999)
        run = SpecRun(self.s, self.cfg, persist=False, state_dir=os.path.join(self.directory, 'state'))
        run.open_manifest(self.directory, resume=resume, dry=True)
        self.addCleanup(run.journal.close)
        return run

    def test_resume_regenerates_exact_plan_and_skips_received_payloads(self):
        first = self.make()
        sender = Sender(self.cfg, self.directory)
        original = first._deliver
        calls = []
        def interrupt(sender, d):
            if len(calls) == 4:
                raise KeyboardInterrupt()
            calls.append(d['_delivery_id'])
            original(sender, d)
        first._deliver = interrupt
        with self.assertRaises(KeyboardInterrupt):
            first.run_batch(sender, quiet=True)
        sender.close()
        from pathlib import Path
        before = {f: Path(self.directory, f).read_text() for f in os.listdir(self.directory) if f.endswith('.json')}
        second = self.make(True)
        sender = Sender(self.cfg, self.directory)
        second.run_batch(sender, quiet=True)
        sender.close()
        for name, content in before.items():
            with open(os.path.join(self.directory, name)) as f:
                self.assertEqual(f.read(), content)
        with open(second.manifest) as f:
            receipts = [r for line in f if (r := json.loads(line)).get('stage') == 'sent']
        self.assertEqual(len(receipts), 8)
        self.assertEqual(len({r['index'] for r in receipts}), 8)
        count = len([f for f in os.listdir(self.directory) if f.endswith('.json')])
        third = self.make(True)
        sender = Sender(self.cfg, self.directory)
        third.run_batch(sender, quiet=True)
        sender.close()
        self.assertEqual(sum(sender.stats.values()), 0)
        self.assertEqual(count, len([f for f in os.listdir(self.directory) if f.endswith('.json')]))

    def test_spec_change_is_rejected(self):
        self.make()
        self.s['traffic']['count'] = 10
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.make(True)

    def test_inflight_crash_cannot_be_automatically_resent(self):
        journal = RunJournal(self.directory)
        self.addCleanup(journal.close)
        ds = [{'payload': {}}]
        journal.plan_session(0, {'session_id': 'one'}, ds)
        journal.begin('0:0')
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            journal.assert_resumable()

    def test_timeout_is_uncertain_and_is_not_retried(self):
        sender = Sender(self.cfg)
        self.addCleanup(sender.close)
        sender.strict_receipts = True
        with patch('xcorgen.runner.post', side_effect=TransportError('network timeout')) as post:
            with self.assertRaises(TransportError):
                sender._post('https://collector.example.com/v1/traces', b'', {})
            self.assertEqual(post.call_count, 1)

    def test_partial_success_is_never_classified_as_accepted(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['otlp'].update(endpoint='http://127.0.0.1:4318', encoding='json')
        sender = Sender(cfg)
        self.addCleanup(sender.close)
        with patch.object(sender, '_post', return_value=b'{"partialSuccess":{"rejectedSpans":2}}'):
            receipt = sender._send({'target': 'otlp', 'kind': 'traces', 'payload': {'resourceSpans': []}})
        self.assertEqual(receipt['status'], 'partial')
        self.assertEqual(receipt['rejected'], 2)
