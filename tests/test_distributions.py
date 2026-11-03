import copy
import random
import statistics
import unittest

from xcorgen import distributions as d
from xcorgen import spec
from xcorgen.modeling import assign_experiments


class Distributions(unittest.TestCase):
    def test_families_have_stable_finite_draws_and_declared_support(self):
        for family, (_, _, params) in d.FAMILIES.items():
            config = {'family': family, 'parameters': {k: v[1] for k, v in params.items()}}
            self.assertEqual(d.validate(config), [], family)
            a, b = random.Random(7331), random.Random(7331)
            xs = [d.draw(config, a)[0] for _ in range(10000)]
            self.assertEqual(xs, [d.draw(config, b)[0] for _ in xs])
            self.assertGreaterEqual(min(xs), 0)
            if family.startswith('bounded'): self.assertLessEqual(max(xs), config['parameters']['high'])
            if family == 'right_skew': self.assertGreater(statistics.mean(xs), statistics.median(xs))
            if family == 'left_skew': self.assertLess(statistics.mean(xs), statistics.median(xs))
            if family == 'bimodal': self.assertAlmostEqual(sum(x < 125 for x in xs)/len(xs), .7, delta=.02)

    def test_lognormal_quantiles_and_tail_body(self):
        cfg = d.lognormal(100, 500)
        p = d.preview(cfg, 'backend_work_ms', {'kind':'tail','factor':4,'knee':.8}, count=4096)
        self.assertAlmostEqual(p['baseline']['p50'],100,delta=5)
        self.assertAlmostEqual(p['baseline']['p95'],500,delta=50)
        self.assertEqual(p['baseline']['p50'],p['affected']['p50'])
        self.assertGreater(p['affected']['p95'],p['baseline']['p95']*1.5)
        # Tied values never receive arbitrary rank-dependent effects.
        c={'family':'constant','parameters':{'value':50}}
        q=d.preview(c,'backend_work_ms',{'kind':'tail','factor':5,'knee':.8},count=1)
        self.assertEqual(q['affected']['p50'],50)

    def test_bounds_mass_and_invalid_inputs(self):
        c={'family':'bounded_right_skew','parameters':{'low':0,'high':100,'alpha':2,'beta':5,'upper_mass':.2}}
        r=random.Random(81);xs=[d.draw(c,r)[0] for _ in range(15000)]
        self.assertAlmostEqual(sum(x==100 for x in xs)/len(xs),.2,delta=.015)
        for config in (
            {'family':'unknown','parameters':{}},
            {'family':'normal','parameters':{'mean':10,'stddev':-1}},
            {'family':'constant','parameters':{'value':float('nan')}},
            {'family':'constant','parameters':{'value':-10}},
            {'family':'normal','parameters':{'mean':-1000,'stddev':0}},
            {'family':'lognormal','parameters':{'mu':1000,'sigma':1}},
            dict(c,parameters={**c['parameters'],'alpha':5,'beta':2}),
        ): self.assertTrue(d.validate(config),config)
        clip={'family':'constant','parameters':{'value':1000},'bounds':{'low':0,'high':100,'policy':'clip'}}
        self.assertEqual(d.draw(clip,random.Random())[0],100)
        fraction={'family':'constant','parameters':{'value':.04}}
        self.assertEqual(d.draw(fraction,random.Random())[0],.04)

    def test_namespaced_assignment_and_sampling(self):
        e={'id':'checkout','variants':{'control':50,'treatment':50},'exposure_endpoints':['get_product']}
        cfg={'experiments':[e]};person={'device_id':'identity'}
        original=assign_experiments(cfg,person,81,'https://www.vantra.com')
        self.assertEqual(original['checkout'],assign_experiments({'experiments':[dict(e,id='unrelated'),e]},person,81,'https://www.vantra.com')['checkout'])
        for n in range(100):
            p={'device_id':str(n)}
            self.assertEqual(assign_experiments(cfg,p,81),{k:v for k,v in assign_experiments({'experiments':[dict(e,id='unrelated'),e]},p,81).items() if k=='checkout'})
        self.assertEqual(d.rng_for('quantity',81,2,'read').random(),d.rng_for('quantity',81,2,'read').random())

    def test_semantics_reject_ambiguous_or_inert_configuration(self):
        s=spec.default_spec();b={'id':'cost','quantity':'backend_work_ms','distribution':d.default('backend_work_ms')}
        s['modeling']={'version':'1.0','bindings':[b]}
        self.assertFalse(spec.errors(spec.validate(s)))
        s['modeling']['bindings'].append(dict(b,id='other'))
        self.assertTrue(spec.errors(spec.validate(s)))
        s['modeling']['bindings']=[dict(b,cohort={'endpoints':['does_not_exist']})]
        self.assertTrue(spec.errors(spec.validate(s)))
        s['modeling']['bindings']=[b];s['causal']={'scenario':'healthy'}
        self.assertTrue(spec.errors(spec.validate(s)))

    def test_real_execution_all_platforms_and_independent_draws(self):
        import tempfile
        from xcorgen.runner import load_config
        from xcorgen.spec_runner import SpecRun
        from xcorgen.linter import lint_payloads
        from xcorgen.web_contract import check
        for platform,target in [('android',spec.default_spec()['targets'][0]),('ios',spec.IOS_TARGET),('web',spec.WEB_TARGET)]:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp:
                s=spec.default_spec();s['targets']=[copy.deepcopy(target)];s['seed']=81
                s['destinations']['mode']='end_to_end';s['chronosphere']={'tenant':'demo-sandbox','token_ref':'env:CHRONOSPHERE_API_TOKEN'}
                s['population'].update(model='varied',platform_mix={target['id']:1},line_of_business_mix={'retail':1,'banking':0,'travel':0},intent_mix={'retail':{'buyer':1}})
                s['modeling']={'version':'1.0','bindings':[{'id':'gateway','quantity':'backend_work_ms','distribution':{'family':'constant','parameters':{'value':500}}}],
                    'experiments':[{'id':'cart','variants':{'control':0,'treatment':1},'exposure_endpoints':['get_product']}]}
                random.seed(81);run=SpecRun(s,load_config(None),persist=False,state_dir=temp,now_ms=1793638800000)
                t,session,_,ds,fs=run.session(1793638600000);run.record(t,session,fs,ds)
                self.assertFalse(lint_payloads(ds))
                self.assertTrue(any(x['target']=='otlp' and x['kind']=='traces' for x in ds))
                model=session.performance_model.evidence()
                self.assertTrue(model['draws']);self.assertTrue(all(x['after']==500 for x in model['draws']))
                obs=run.emitted.observations[0]
                self.assertTrue(all(n['ms']>=500 for n in obs['net']))
                self.assertEqual(model['assignments']['cart'],'treatment')
                self.assertIn('cart',model['exposed'])
                for delivery in ds:
                    if platform=='web' and delivery['target']=='embrace':
                        errors=[];check(delivery['payload'],errors.append);self.assertEqual(errors,[])
                    for rs in delivery['payload'].get('resourceSpans',[]):
                        for ss in rs['scopeSpans']:
                            for sp in ss['spans']:
                                self.assertGreaterEqual(int(sp['endTimeUnixNano']),int(sp['startTimeUnixNano']))
                report=run.emitted.summary()['modeling']
                self.assertEqual(report['effects'][0]['eligible_visits'],1)
                self.assertFalse(report['storage_verified'])
                # An unrelated reading binding cannot shift the gateway RNG stream.
                model_object=session.performance_model
                model_object.config['bindings'][0]['distribution']=d.default('backend_work_ms')
                counter=model_object.counters.copy()
                first=model_object.sample('backend_work_ms',lambda:1,'home')
                model_object.counters=counter
                model_object.config['bindings'].insert(0,{'id':'other','quantity':'reading_ms','distribution':d.default('reading_ms')})
                second=model_object.sample('backend_work_ms',lambda:1,'home')
                self.assertEqual(first,second)

    def test_release_curve_interpolation_stickiness_and_rollback(self):
        from xcorgen.adoption import choose, mix, versions
        target=copy.deepcopy(spec.ANDROID_TARGET);now=1793638800000
        old,new=target['android']['versions'][1]['version'],target['android']['versions'][0]['version']
        curve={'target':target['id'],'start':spec.iso(now),'points':[
            {'minute':0,'shares':{old:100,new:0}}, {'minute':10,'shares':{old:0,new:100}},
            {'minute':20,'shares':{old:100,new:0}}]}
        self.assertEqual(mix(curve,5),{old:50,new:50})
        self.assertEqual(mix(curve,-100),curve['points'][0]['shares'])
        config={'version':'1.0','releases':[curve]};s=spec.default_spec();s['modeling']=config
        self.assertFalse(spec.errors(spec.validate(s)))
        p={'device_id':'one'};fallback=(old,'1027032','9.3.1')
        self.assertEqual(choose(target,p,now,config,0,fallback),fallback)
        self.assertEqual(choose(target,p,now+600000,config,0,fallback)[0],new)
        self.assertEqual(choose(target,p,now+1200000,config,0,fallback)[0],new)
        curve['allow_rollback']=True
        self.assertEqual(choose(target,p,now+1200000,config,0,fallback)[0],old)
        selected=sum(choose(target,{'device_id':str(i)},now+300000,config,0,fallback)[0]==new for i in range(4000))
        self.assertAlmostEqual(selected/4000,.5,delta=.025)
        curve['points'][1]['shares']={new:90}
        self.assertTrue(spec.errors(spec.validate(s)))
        self.assertEqual(len(versions(spec.WEB_TARGET)),2)

    def test_release_reaches_payloads_and_new_runs_have_new_session_ids(self):
        import tempfile
        from xcorgen.runner import load_config
        from xcorgen.spec_runner import SpecRun
        from xcorgen.adoption import versions
        now=1793638800000
        for target in (spec.ANDROID_TARGET,spec.IOS_TARGET,spec.WEB_TARGET):
            value=versions(target)[-1]['version'];samples=[]
            for _ in range(2):
                with tempfile.TemporaryDirectory() as state:
                    s=spec.default_spec();s['targets']=[copy.deepcopy(target)];s['seed']=0
                    s['population'].update(model='varied',platform_mix={target['id']:1})
                    s['modeling']={'version':'1.0','bindings':[{'id':'gateway','quantity':'backend_work_ms','distribution':d.default('backend_work_ms')}],
                        'releases':[{'target':target['id'],'start':spec.iso(now-600000),'points':[{'minute':0,'shares':{value:100}},{'minute':20,'shares':{value:100}}]}]}
                    random.seed(0);run=SpecRun(s,load_config(None),persist=False,now_ms=now,state_dir=state,rng=random.Random(0))
                    t,session,_,ds,fs=run.session(now-200000);run.record(t,session,fs,ds)
                    observed=run.emitted.observations[0]
                    self.assertEqual(observed['app_version'],value)
                    self.assertEqual(run.emitted.summary()['modeling']['releases'][0]['observed'][0]['version'],value)
                    samples.append((observed['session_id'],session.performance_model.evidence()['draws']))
            self.assertNotEqual(samples[0][0],samples[1][0],target['platform'])
            self.assertEqual(samples[0][1],samples[1][1],target['platform'])
