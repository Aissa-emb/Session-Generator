import json
from pathlib import Path
import random
import tempfile
import time
import unittest
from xcorgen import linter, spec
from xcorgen.emitted import Emitted
from xcorgen.personas import PersonaPool
from xcorgen.runner import load_config
from xcorgen.spec_runner import SpecRun


class StreamingLimitsTests(unittest.TestCase):
    def test_runtime_cache_bounds_lookup_only_misses_and_unexpired_fills(self):
        from xcorgen.scenarios import Runtime
        runtime = Runtime()
        runtime.CACHE_LIMIT = 20
        for i in range(100):
            runtime.lookup(str(i), 1000, initial_hit=0)
        self.assertEqual(len(runtime.cache), 20)
        for i in range(100, 200):
            runtime.fill(str(i), 1000, ttl=10**9)
        self.assertEqual(len(runtime.cache), 20)
        self.assertTrue(runtime.lookup('199', 1001))
        self.assertFalse(runtime.lookup('199', 10**10))

    def test_linter_identifier_tails_and_traversal_order(self):
        for suffix in ('traceId', 'spanId', 'user.id', 'device_id', 'traceparent', 'identifier', 'timeUnixNano', 'message', 'card'):
            for wrapper in ('', '.stringValue', '.intValue', '.doubleValue', '.value'):
                path = '$.' + 'prefix.' * 30 + suffix + wrapper
                self.assertEqual(linter._identifier(path), linter._identifier_tail(path[-linter.IDENTIFIER_TAIL:]), path)
        node = {'items':[{'key':'message','value':{'stringValue':'demo checkout'}}, {'body':'ops@vantra.com'}]}
        visited=[]
        linter._walk(node, '$', lambda *pair: visited.append(pair))
        self.assertEqual(visited, list(linter._strings(node)))

    def test_linter_provenance_long_strings_and_issue_limit(self):
        payload={'resourceSpans':[], 'attributes':[
            {'key':'deployment.environment.name','value':{'stringValue':'synthetic'}},
            {'key':'telemetry.sdk.name','value':{'stringValue':'session-generator'}},
            {'key':'demo.scenario_id','value':{'stringValue':'payments'}},
            {'key':'traceId','value':{'stringValue':'378282246310005'}},
            {'key':'message','value':{'stringValue':'x'*300+' demo ops@vantra.com 555-867-5309 4111 1111 1111 1111'}}]}
        batch=[{'target':'otlp','kind':'traces','payload':payload}]
        issues=linter.lint_payloads(batch)
        self.assertTrue(issues)
        self.assertTrue(all('.message.' in row['path'] for row in issues), issues)
        for limit in (1,3,50):
            self.assertEqual(linter.lint_payloads(batch,max_issues=limit),issues[:limit])
        before=linter._pii_short.cache_info().currsize
        for i in range(100): linter._pii('x'*300+str(i))
        self.assertEqual(before,linter._pii_short.cache_info().currsize)

    def test_streaming_fault_totals_match_full_history_with_bounded_detail(self):
        scenario=json.loads(Path('spec/examples/baseline-burst.json').read_text())
        scenario['traffic']={'mode':'live','rate_per_minute':600,'duration_minutes':1}
        random.seed(42)
        with tempfile.TemporaryDirectory() as tmp:
            run=SpecRun(scenario,load_config(None),persist=False,state_dir=tmp)
            full=Emitted(run.spec)
            run.emitted._sample_limit=8
            for i in range(80):
                target, session, _, deliveries, faults=run.session(run.now_ms+i*100)
                run.record(target,session,faults,deliveries)
                full.add(target,session,deliveries)
            actual, expected=run.emitted.summary(),full.summary()
            self.assertEqual(len(run.emitted.observations),8)
            for key in ('sessions','sdk_sessions','sessions_with_traces','faults'):
                self.assertEqual(actual[key],expected[key],key)
            self.assertEqual(actual['detail_sessions'],8)

    def test_loaded_visitor_pool_respects_cap_and_keeps_recent_visitors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'personas-retail.json'
            path.write_text(json.dumps([{'last_seen':i} for i in range(100)]))
            pool=PersonaPool('retail',tmp,size=20)
            self.assertEqual(len(pool.personas),20)
            self.assertEqual(min(p['last_seen'] for p in pool.personas),80)


if __name__ == '__main__':
    unittest.main()
