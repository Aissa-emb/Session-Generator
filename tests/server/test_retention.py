import json
from pathlib import Path
import tempfile
import unittest
from server import acknowledgements, delivery_summary
from server.retention import rotate_manifest, rotate_log, trim_payloads
from server.store import Store
from server.verification import manifest_sessions


class RetentionTests(unittest.TestCase):
    def test_bounded_rollup_is_duplicate_safe_and_counts_complete_sdk_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            self.addCleanup(store.close)
            run=store.put('runs',{'id':'r','dry_run':True,'manifest_path':str(Path(directory)/'manifest.jsonl')})
            def receipt(ident,status='accepted',kind='logs',target='embrace',accepted=1):
                return dict(run_id='r',delivery_id=ident,target=target,kind=kind,status=status,accepted=accepted,rejected=0,
                            session_receipt={'keys':['session-a','session-b'],'expected_payloads':2})
            acknowledgements.write(store,receipt('1'))
            acknowledgements.write(store,receipt('1'))
            self.assertEqual(delivery_summary.summarize(store,store.get('runs','r'))['sessions_acknowledged'],0)
            acknowledgements.write(store,receipt('2','rejected'))
            acknowledgements.write(store,receipt('2'))
            acknowledgements.write(store,receipt('3',kind='traces',target='otlp',accepted=25))
            acknowledgements.write(store,receipt('3',kind='traces',target='otlp',accepted=25))
            totals=delivery_summary.summarize(store,store.get('runs','r'))
            self.assertEqual(totals['sessions_acknowledged'],2)
            self.assertEqual(totals['backend_spans_acknowledged'],25)

    def test_live_retention_keeps_cumulative_totals_and_uses_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            self.addCleanup(store.close)
            store.put('runs',{'id':'r','dry_run':False})
            rows=[dict(run_id='r',target='otlp',kind='traces',status='accepted',accepted=3,rejected=0,
                       created=float(i),_sessions_acknowledged=1) for i in range(100)]
            store.write_live_receipts('r',rows)
            for _ in range(30): store.event('r','progress')
            store.retain_live_history('r',receipts=10,event_count=5)
            self.assertEqual(len(store.list_for('receipts','run_id','r')),10)
            self.assertEqual(len(store.events('r')),5)
            self.assertEqual(store.get('runs','r')['delivery_totals'],{'sessions_acknowledged':100,'backend_spans_acknowledged':300})
            with store.engine.connect() as conn:
                plan=conn.exec_driver_sql("EXPLAIN QUERY PLAN SELECT data FROM receipts WHERE json_extract(data, '$.run_id')=? ORDER BY created DESC LIMIT 500",('r',)).all()
            self.assertIn('receipts_run_created',str(plan))
            self.assertNotIn('TEMP B-TREE',str(plan))

    def test_manifest_and_log_rotation_and_dry_payload_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'manifest.jsonl'
            path.write_text(json.dumps(dict(index=0,target='a',session_id='one'))+'\n')
            rotate_manifest(path,max_bytes=1)
            path.write_text(json.dumps(dict(index=1,target='a',session_id='two'))+'\n')
            self.assertEqual(len(manifest_sessions(path)),2)
            log=Path(directory)/'server.log'
            log.write_text('messages\n')
            rotate_log(log,max_bytes=1)
            self.assertEqual(log.read_text(),'')
            self.assertEqual(Path(str(log)+'.1').read_text(),'messages\n')
            for i in range(12): (Path(directory)/f'{i:05d}-embrace_logs.json').write_text('{}')
            trim_payloads(directory,keep=4)
            self.assertEqual(len(list(Path(directory).glob('*-embrace_logs.json'))),4)
