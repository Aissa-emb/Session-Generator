import json
from pathlib import Path
import tempfile
import unittest

from server.delivery_summary import annotate, summarize
from server.store import Store


class DeliverySummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.engine.dispose)
        self.path = Path(self.tmp.name) / 'manifest.jsonl'
        self.run = {'id': 'run', 'manifest_path': str(self.path), 'dry_run': False}
        self.record = {'target': 'app', 'platform': 'web', 'app_id': 'kfs9f',
                       'device_id': 'device', 'session_id': 'session', 'index': 0}

    def put(self, delivery, **changes):
        return self.store.put('receipts', dict(run_id='run', delivery_id=delivery,
            target='otlp', kind='traces', status='accepted', accepted=12, rejected=0, **changes))

    def test_full_history_dedup_and_signal_units(self):
        for n in range(501):
            self.put(str(n))
        self.put('0')  # Persisted retry does not double count a payload.
        for target, kind in [('embrace', 'spans'), ('embrace', 'logs'), ('otlp', 'logs'), ('otlp', 'metrics')]:
            self.store.put('receipts', {'run_id': 'run', 'target': target, 'kind': kind,
                                       'status': 'accepted', 'accepted': 9999})
        self.store.put('receipts', {'run_id': 'another-run', 'target': 'otlp', 'kind': 'traces',
                                   'status': 'accepted', 'accepted': 9999})
        result = summarize(self.store, self.run)
        self.assertEqual(result['backend_spans_acknowledged'], 501 * 12)
        self.assertEqual(result['sessions_acknowledged'], 0)
        self.assertIsNone(result['sessions_stored'])
        self.assertIsNone(result['backend_spans_stored'])

    def test_session_needs_every_app_upload_but_not_backend_success(self):
        deliveries = [{'target': 'embrace'}, {'target': 'embrace'}, {'target': 'otlp'}]
        annotate(deliveries, self.record)
        def app(ident, status='accepted'):
            self.store.put('receipts', {'run_id': 'run', 'delivery_id': ident, 'target': 'embrace',
                'kind': 'spans', 'status': status, 'accepted': 10 if status == 'accepted' else 0,
                'session_receipt': deliveries[0]['_session_receipt']})
        app('app-1')
        app('app-1')
        app('app-2', 'partial')
        self.assertEqual(summarize(self.store, self.run)['sessions_acknowledged'], 0)
        app('app-2')
        self.store.put('receipts', {'run_id': 'run', 'target': 'otlp', 'kind': 'traces',
                                   'status': 'uncertain', 'accepted': 0})
        self.assertEqual(summarize(self.store, self.run)['sessions_acknowledged'], 1)
        self.path.write_text(json.dumps(dict(self.record, stage='sent')) + '\n')
        self.assertEqual(summarize(self.store, self.run)['sessions_acknowledged'], 1)

    def test_native_sessions_legacy_history_and_local_output(self):
        rows = [dict(self.record, stage='sent', platform='ios', sdk_sessions=[
            {'id': 'foreground-1', 'start_ms': 1}, {'id': 'foreground-2', 'start_ms': 2}])]
        rows += [dict(self.record, session_id=stage, index=n + 1, stage=stage)
                 for n, stage in enumerate(['planned', 'delivery_failed', 'partially_expired', 'expired'])]
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        result = summarize(self.store, dict(self.run, dry_run=True))
        self.assertEqual(result['sessions_acknowledged'], 2)
        self.assertEqual(result['basis'], 'local_output')
        self.assertEqual(result['storage_status'], 'not_verified')

    def test_summary_reads_while_last_manifest_record_is_being_appended(self):
        self.path.write_text(json.dumps(dict(self.record, stage='sent')) + '\n{"index":')
        self.assertEqual(summarize(self.store, self.run)['sessions_acknowledged'], 1)

    def test_backend_partial_counts_only_explicitly_acknowledged_spans(self):
        for status, accepted in [('partial', 2), ('uncertain', 99), ('expired', 99), ('rejected', 99)]:
            self.store.put('receipts', {'run_id': 'run', 'delivery_id': status, 'target': 'otlp',
                'kind': 'traces', 'status': status, 'accepted': accepted, 'rejected': 3})
        self.assertEqual(summarize(self.store, self.run)['backend_spans_acknowledged'], 2)
