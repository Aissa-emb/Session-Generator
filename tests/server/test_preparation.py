import copy
import json
import tempfile
import time
import unittest
from server.service import Service
from server.store import Store
from server import preparation, worker
from xcorgen import spec, limits


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.addCleanup(self.store.engine.dispose)
        self.svc=Service(self.store,False)
        self.spec=spec.default_spec();self.spec['traffic']={'mode':'burst','count':4,'window':{'last_minutes':8}}

    def test_sessions_only_workflow_resumes_without_duplicate_jobs(self):
        saved=self.svc.create_spec(self.spec)
        p=preparation.plan(self.svc,saved['id'],history_hours=1,rate_per_minute=.1,duration_minutes=1)
        self.assertEqual([s['id'] for s in p['stages']],['history','live'])
        with self.assertRaises(ValueError):preparation.start(self.svc,p['id'])
        preparation.start(self.svc,p['id'],True,True)
        first=preparation.advance(self.svc,p['id']);run_id=first['stages'][0]['run_id']
        self.assertEqual(preparation.advance(self.svc,p['id'])['stages'][0]['run_id'],run_id)
        worker.execute(self.store,run_id)
        self.assertEqual(self.store.get('runs',run_id)['status'],'completed')
        self.assertTrue(all(r['target']=='embrace' for r in self.store.list_for('receipts','run_id',run_id)))
        preparation.advance(self.svc,p['id']);done=preparation.advance(self.svc,p['id'])
        self.assertEqual(done['status'],'completed');self.assertEqual(len(self.store.list('runs')),2)
        self.assertEqual(len(self.store.list('streams')),1)
        self.assertEqual(preparation.advance(self.svc,p['id'])['status'],'completed')
        preparation.cancel(self.svc,p['id'])
        self.assertEqual(self.store.list('streams')[0]['status'],'stopped')

    def test_end_to_end_is_blocked_until_readiness_or_explicit_local_dry_run(self):
        s=copy.deepcopy(self.spec);s['destinations']['mode']='end_to_end';s['chronosphere']={'tenant':'acme-prod'}
        saved=self.svc.create_spec(s)
        p=preparation.plan(self.svc,saved['id'],history_hours=1,rate_per_minute=.1)
        self.assertEqual([s['spec']['destinations']['mode'] for s in p['stages']],['sessions_only','end_to_end','end_to_end'])
        blocked=preparation.start(self.svc,p['id'],True)
        self.assertEqual(blocked['status'],'blocked');self.assertEqual(self.store.list('runs'),[])
        p=preparation.start(self.svc,p['id'],True,True)
        self.assertEqual(p['readiness']['status'],'simulated')
        first=preparation.advance(self.svc,p['id']);worker.execute(self.store,first['stages'][0]['run_id'])
        preparation.advance(self.svc,p['id']);recent=preparation.advance(self.svc,p['id'])
        rid=recent['stages'][1]['run_id'];worker.execute(self.store,rid)
        self.assertTrue(any(r['target']=='otlp' for r in self.store.list_for('receipts','run_id',rid)))
        preparation.cancel(self.svc,p['id']);self.assertEqual(preparation.advance(self.svc,p['id'])['status'],'cancelled')
        self.assertEqual(len(self.store.list('runs')),2)

    def test_prepare_ahead_defaults_and_scheduled_incident(self):
        s=copy.deepcopy(self.spec);s['destinations']['mode']='end_to_end';s['chronosphere']={'tenant':'acme-prod'}
        s['client_faults']=[{'id':'crash','type':'jvm_crash','rate':.2}]
        saved=self.svc.create_spec(s)
        p=preparation.plan(self.svc,saved['id'],prepare_ahead=True,minutes_until_demo=150)
        self.assertEqual(len(p['stages']),1);self.assertEqual(p['stages'][0]['spec']['traffic']['duration_minutes'],120)
        self.assertIsNotNone(p['recommendation']);self.assertEqual(p['trace_caveat'],limits.TRACE_CAVEAT)
        p=preparation.plan(self.svc,saved['id'],history_hours=0,minutes_until_demo=10,incident={'id':'launch','fault_ref':'crash','peak_minutes':5})
        start=spec.parse_time(p['stages'][-1]['spec']['incidents'][-1]['start'])
        self.assertAlmostEqual((start-time.time()*1000)/60000,13,delta=.1)
        with self.assertRaises(ValueError):preparation.plan(self.svc,saved['id'],history_hours=25)
        with self.assertRaises(ValueError):preparation.plan(self.svc,saved['id'],rate_per_minute=600)
