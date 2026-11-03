import copy
import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from xcorgen.causal.model import stream
from xcorgen.causal.scenarios import build
from xcorgen.causal.registry import SCENARIOS, SERVICES
from xcorgen.causal.adapter import create
from xcorgen.causal.validation import validate_serialized, verify_returned_trace
from xcorgen.causal.business import Ledger
from xcorgen.spec import default_spec, normalized, ANDROID_TARGET, IOS_TARGET, WEB_TARGET, validate, errors
from xcorgen.linter import lint_payloads
from xcorgen.spec_runner import SpecRun

NOW=1793420000000

def spec(platform='android', scenario='healthy', variant='affected', **options):
    s=default_spec()
    s.update(seed=7331,causal={'scenario':scenario,'variant':variant,**options})
    s['targets']=[copy.deepcopy({'android':ANDROID_TARGET,'ios':IOS_TARGET,'web':WEB_TARGET}[platform])]
    s['destinations']={'mode':'end_to_end','backend_logs':True}
    s['chronosphere']={'tenant':'demo-sandbox'}
    s['traffic']={'mode':'burst','count':1,'window':{'last_minutes':1}}
    s['population']['line_of_business_mix']={k:int(k==('travel' if scenario=='travel-provider' else 'banking')) for k in ('retail','banking','travel')}
    return normalized(s)

class CausalTests(unittest.TestCase):
    def test_platform_cohort_matrix(self):
        for platform in ('android','ios','web'):
            for scenario in SCENARIOS:
                for variant in ('affected','control','recovered'):
                    with self.subTest(platform=platform,scenario=scenario,variant=variant):
                        cfg=spec(platform,scenario,variant)
                        self.assertFalse(errors(validate(cfg,NOW)))
                        s,_,deliveries=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
                        self.assertFalse(lint_payloads(deliveries))
                        self.assertTrue(s.causal_evidence['evidence']['fixture_validated'])
                        self.assertFalse(s.causal_evidence['evidence']['dashboard_verified'])

    def test_reference_critical_paths(self):
        g=build('healthy','control','web',7331,0,'1'*32)
        profile,accounts=g.get('overview.profile-out'),g.get('overview.portfolio-out')
        self.assertLess(accounts.start,profile.end)
        self.assertEqual(g.get('overview.client').end-g.get('overview.client').start,310)
        self.assertEqual(g.get('overview.query').end-g.get('overview.query').start,144)
        warm=build('healthy','control','ios',7331,0,'1'*32,{'cache':'warm'})
        self.assertNotIn('overview.query',[s.key for s in warm.spans])
        slow=build('slow-history','affected','android',7331,0,'1'*32)
        self.assertEqual(slow.get('history.query').end-slow.get('history.query').start,2100)
        self.assertTrue(all(not s.error for s in slow.spans))
        pool=build('signin-outage','affected','android',7331,0,'1'*32)
        self.assertEqual(pool.get('signin.pool').end-pool.get('signin.pool').start,3000)
        self.assertEqual(sum(bool(s.diagnostic) for s in pool.spans),1)
        self.assertFalse(any(s.name.startswith('SELECT') for s in pool.spans))

    def test_invalid_graphs_rejected(self):
        mutations=[lambda g:setattr(g.get('overview.profile'),'parent','overview.profile'),
            lambda g:setattr(g.get('overview.profile'),'trace','different'),
            lambda g:setattr(g.get('overview.compose'),'start',g.get('overview.portfolio-out').end-1),
            lambda g:g.get('overview.portfolio').attributes.update({'http.response.status_code':0}),
            lambda g:setattr(g.get('overview.query'),'service','unregistered-service'),
            lambda g:g.get('overview.out').attributes.update({'http.route':'/bad'}),
            lambda g:g.get('overview.accounts-cache').attributes.update({'demo.cache.outcome':'hit'})]
        for mutate in mutations:
            g=build('healthy','control','web',7331,0,'1'*32);mutate(g)
            with self.assertRaises(ValueError):g.validate()
        g=build('connectivity','affected','ios',1,0,'1'*32)
        g.add('false-server','vantra-edge','GET /bad',2,16501,16510,'history.client',trace='history')
        with self.assertRaisesRegex(ValueError,'pre-send'):g.validate()

    def test_response_loss_and_async_completion(self):
        for scenario in ('transfer-response-loss','worker-backlog'):
            g=build(scenario,'affected','web',7331,0,'1'*32,{'retry':True})
            self.assertEqual(g.effects['ledger_transactions'],1)
            self.assertEqual(g.effects['transfer_intents'],1)
            self.assertLess(g.get('submit.edge').end,g.get('submit.consume').start)
            self.assertIsNone(g.get('submit.consume').parent)
            self.assertEqual(g.get('submit.consume').links,['submit.produce'])
            if scenario=='transfer-response-loss':
                self.assertIsNone(next(r for r in g.requests if r.key=='submit').status)
                self.assertEqual(g.get('submit.edge').attributes['http.response.status_code'],202)
            g.effects['ledger_transactions']=2
            with self.assertRaisesRegex(ValueError,'duplicate transfer'):g.validate()

    def test_idempotent_races_and_authorization(self):
        ledger=Ledger()
        def attempt(_):
            ledger.accept('owner','OP','K');ledger.post('OP');ledger.complete('OP')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(attempt,range(80)))
        self.assertEqual(len(ledger.transactions),1)
        self.assertEqual(ledger.status('owner','OP'),'Completed')
        with self.assertRaises(ValueError):ledger.status('different-owner','OP')
        with self.assertRaises(ValueError):ledger.accept('owner','DIFFERENT','K')
        with self.assertRaises(ValueError):ledger.accept('owner','OP','K',20000)

    def test_fatal_contract_is_distinct_from_handled(self):
        for platform in ('android','ios'):
            for failure in ('handled','fatal'):
                cfg=spec(platform,'contract-regression',client_failure=failure)
                s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
                self.assertEqual(bool(getattr(s,'crash_evidence',None)),failure=='fatal')
                text=json.dumps(ds)
                self.assertIn('TransactionDetailMapper' if failure=='fatal' else 'Decoding',text)
                if platform=='ios':self.assertNotIn('TransactionDetailMapper.kt',text)
                network=next(r for r in s.plan.evidence if r['key']=='detail')
                self.assertEqual(network['status'],200)
                self.assertEqual(network['backend_errors'],0)

    def test_seed_replay_and_fresh_identity(self):
        a=build('slow-history','affected','web',7331,7,'1'*32,{'reference':False})
        b=build('slow-history','affected','web',7331,7,'2'*32,{'reference':False})
        self.assertEqual([(s.name,s.start,s.end) for s in a.spans],[(s.name,s.start,s.end) for s in b.spans])
        self.assertNotEqual(a.trace_id('history'),b.trace_id('history'))
        for platform in ('android','web','ios'):
            cfg=spec(platform)
            x,_,_=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
            y,_,_=create(cfg['targets'][0],cfg,NOW,0,'2'*32)
            z,_,_=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
            self.assertNotEqual(x.user_session_id,y.user_session_id)
            self.assertEqual(x.user_session_id,z.user_session_id)
            self.assertEqual(x.persona['device_id'],y.persona['device_id'])
            self.assertEqual(x.causal_evidence['device_id'],y.causal_evidence['device_id'])

    def test_malformed_serialized_parent_fails(self):
        cfg=spec();s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
        bad=copy.deepcopy(ds)
        for d in bad:
            if d['target']=='otlp' and d['kind']=='traces':
                d['payload']['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['parentSpanId']='f'*16;break
        with self.assertRaisesRegex(ValueError,'parent'):validate_serialized(s,bad)

    def test_no_invented_backlinks(self):
        cfg=spec();s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
        manifest=s.causal_evidence
        result=verify_returned_trace(manifest,ds)
        self.assertTrue(all(x['state']=='unverified' for x in result))
        self.assertTrue(all(x['session_url'] is None for x in result))

    def test_causal_counts_and_no_unrelated_business_measures(self):
        from xcorgen.emitted import Emitted
        cfg=spec('web','slow-history');s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
        report=Emitted(cfg);report.add(cfg['targets'][0],s,ds)
        self.assertEqual(report.summary()['sessions_with_traces'],1)
        self.assertNotIn('accounts:balances-rendered',json.dumps(ds))
        self.assertNotIn('balance-summary',json.dumps(ds))

    def test_network_profiles_and_mixed_variants(self):
        for platform in ('android','ios','web'):
            for scenario in ('contract-regression','transfer-response-loss','connectivity','travel-provider'):
                cfg=spec(platform,scenario,network='constrained',connectivity_phase='post_send')
                s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
                self.assertFalse(lint_payloads(ds))
        from xcorgen.causal.adapter import select
        cfg={'scenario':'signin-outage','mode':'mixed'}
        choices=[select(cfg,7331,i,NOW) for i in range(1000)]
        self.assertGreater(choices.count(('healthy','control')),650)
        self.assertEqual(choices,[select(cfg,7331,i,NOW) for i in range(1000)])

    def test_scheduled_cohort_and_recovery(self):
        from xcorgen.causal.adapter import select
        from xcorgen.spec import iso
        cfg={'scenario':'signin-outage','variant':'scheduled','incident_start':iso(NOW),'incident_minutes':2,'cohort_share':1}
        self.assertEqual(select(cfg,0,0,NOW-1)[1],'control')
        self.assertEqual(select(cfg,0,0,NOW)[1],'affected')
        self.assertEqual(select(cfg,0,0,NOW+120000)[1],'recovered')
        cfg['cohort_share']=0
        self.assertEqual(select(cfg,0,0,NOW)[1],'control')

    def test_widget_and_conditional_provider(self):
        from xcorgen.web_observation import records, attrs
        cfg=spec('web','frontend-regression',web_widget=True,provider='support',reference=False)
        s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
        self.assertEqual(s.causal_evidence['expected_outcome'],'slow_interaction')
        self.assertFalse(any(e.kind=='exception' for e in s.execution.events))
        external=[sp for d in ds if d['target']=='embrace' for sp in records(d['payload'],'spans') if attrs(sp).get('server.address')=='support.example']
        self.assertEqual(len(external),1)
        self.assertNotIn('emb.w3c_traceparent',attrs(external[0]))
        cfg['targets']=[copy.deepcopy(ANDROID_TARGET)]
        self.assertTrue(errors(validate(cfg,NOW)))

    def test_real_base64_backlink_and_wrong_session(self):
        import base64
        from xcorgen import otlp
        cfg=spec();s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
        payload=copy.deepcopy(ds)
        for req in s.causal_evidence['requests']:
            payload.append({'span_id':base64.b64encode(bytes.fromhex(req['client_span_id'])).decode(),
                'trace_id':base64.b64encode(bytes.fromhex(req['trace_id'])).decode(),
                'attributes':otlp.attrs({'emb.dashboard_session':'https://dash.embrace.io/app/5nrc2/session/'+s.user_session_id})})
        rows=verify_returned_trace(s.causal_evidence,payload)
        self.assertTrue(all(r['state']=='backlink_observed' for r in rows))
        payload[-1]['attributes']=otlp.attrs({'emb.dashboard_session':'https://dash.embrace.io/app/5nrc2/session/unrelated'})
        self.assertEqual(verify_returned_trace(s.causal_evidence,payload)[-1]['state'],'unverified')

    def test_no_duplicate_frontend_export_or_exact_id_live(self):
        cfg=spec();cfg['destinations']['emit_client_spans']=True
        self.assertTrue(errors(validate(cfg,NOW)))
        cfg=spec(replay_namespace='1'*32)
        run=SpecRun(cfg,{'embrace':{'apps':{}},'chronosphere':{},'otlp':{}},persist=False,now_ms=NOW)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'local dry'):run.open_manifest(directory,dry=False)

    def test_reference_variation_preserves_deadlines(self):
        for scenario,node,duration in [('signin-outage','signin.pool',3000),('transfer-response-loss','submit.client',5000)]:
            for index in range(20):
                g=build(scenario,'affected','web',7331,index,'1'*32,{'reference':False})
                n=g.get(node)
                self.assertEqual(n.end-n.start,duration)
                g=build(scenario,'affected','web',7331,index,'1'*32,{'reference':False,'network':'constrained'})
                n=g.get(node)
                self.assertEqual(n.end-n.start,duration)

    def test_async_links_are_not_observed_spans(self):
        from xcorgen.causal.validation import observed_spans
        cfg=spec('web','worker-backlog');s,_,ds=create(cfg['targets'][0],cfg,NOW,0,'1'*32)
        backend=[copy.deepcopy(d) for d in ds if d['target']=='otlp' and d['kind']=='traces']
        original=observed_spans(backend)
        self.assertEqual(len(original),len({(r['trace'],r['id']) for r in original}))
        rows=verify_returned_trace(s.causal_evidence,backend)
        self.assertTrue(next(r for r in rows if r['request']=='asynchronous processing')['links_verified'])
        observed_copy=copy.deepcopy(backend)
        for d in observed_copy:
            for rs in d['payload']['resourceSpans']:
                for ss in rs['scopeSpans']:
                    for sp in ss['spans']:
                        if not sp.get('parentSpanId'):sp['parentSpanId']='AAAAAAAAAAA='
        self.assertTrue(next(r for r in verify_returned_trace(s.causal_evidence,observed_copy) if r['request']=='asynchronous processing')['parents_verified'])
        for d in backend:
            for rs in d['payload']['resourceSpans']:
                for ss in rs['scopeSpans']:
                    for sp in ss['spans']:sp.pop('links',None)
        self.assertFalse(next(r for r in verify_returned_trace(s.causal_evidence,backend) if r['request']=='asynchronous processing')['links_verified'])
        with self.assertRaisesRegex(ValueError,'links'):validate_serialized(s,[d for d in ds if d['target']!='otlp']+backend)

if __name__=='__main__':unittest.main()
