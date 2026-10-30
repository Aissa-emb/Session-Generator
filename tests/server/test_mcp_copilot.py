import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from server.app import create_app
from server.service import Service
from server.store import Store
from server.mcp import build_server, caller
from server import copilot
from xcorgen import spec


class RecordedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = self
        self.requests = []
    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(content=next(self.responses))


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.engine.dispose)
        self.svc = Service(self.store, launch_jobs=False)
        self.spec = spec.default_spec()
        self.spec['traffic'] = {'mode': 'burst', 'count': 4, 'window': {'last_minutes': 8}}

    def test_http_protocol_auth_confirmation_and_attribution(self):
        token = self.store.create_token('Protocol client')['token']
        with TestClient(create_app(self.svc)) as client:
            headers = {'host': 'localhost:80', 'Authorization': 'Bearer '+token, 'Accept': 'application/json, text/event-stream'}
            def call(method, params=None):
                return client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}, headers=headers)
            self.assertEqual(client.post('/mcp', json={}).status_code, 401)
            initialized = call('initialize', {'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'acceptance','version':'1'}})
            self.assertEqual(initialized.status_code,200, initialized.text)
            listed = call('tools/list').json()['result']['tools']
            self.assertGreaterEqual(len(listed), 22)
            saved = call('tools/call', {'name':'create_spec','arguments':{'spec':self.spec}}).json()['result']['structuredContent']
            denied = call('tools/call', {'name':'start_run','arguments':{'spec_id':saved['id'],'dry_run':True}}).json()['result']
            self.assertTrue(denied['isError'])
            accepted = call('tools/call', {'name':'start_run','arguments':{'spec_id':saved['id'],'dry_run':True,'confirm':True,'idempotency_key':'confirmed'}}).json()['result']['structuredContent']
            self.assertEqual(accepted['author'],'mcp:Protocol client')
            self.assertEqual(len(self.store.list('runs')),1)
            self.assertIsNotNone(self.store.list('api_tokens')[0]['last_used'])
            self.store.update('api_tokens', self.store.list('api_tokens')[0]['id'], revoked=True)
            self.assertEqual(call('tools/list').status_code,401)

    def test_stdio_protocol_lists_resources_and_previews(self):
        async def check():
            params = StdioServerParameters(command=sys.executable,args=['-m','server.mcp'],env=dict(os.environ, SG_DATA_DIR=self.tmp.name))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    self.assertEqual(len((await session.list_resources()).resources),4)
                    self.assertEqual(len((await session.list_prompts()).prompts),2)
                    result=await session.call_tool('preview_spec',{'spec':self.spec})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content['sample_count'],4)
        asyncio.run(check())

    def test_seven_recorded_requests_produce_valid_reviewable_versions(self):
        fixtures=json.loads((Path(__file__).parents[1]/'fixtures/copilot/responses.json').read_text().replace('$INCIDENT_START',spec.iso(time.time()*1000-20*3600000)))
        for i,f in enumerate(fixtures):
            with self.subTest(request=i):
                s=copy.deepcopy(self.spec)
                s['targets'].append(copy.deepcopy(spec.WEB_TARGET))
                s['population']['platform_mix']={'vantra-android':1,'vantra-web':1}
                saved=self.svc.create_spec(s)
                model=RecordedModel([[{'type':'tool_use','id':'inspect','name':'get_spec','input':{'spec_id':saved['id']}}],
                    [{'type':'tool_use','id':'answer','name':'propose_patch','input':{'patch':f['patch'],'summary':f['summary']}}]])
                result=asyncio.run(copilot.turn(self.svc,saved['id'],f['request'],client=model))
                self.assertEqual(result['state'],'proposed')
                self.assertEqual(self.store.get('scenario_specs',saved['id'])['version'],1)
                self.assertTrue(model.requests[0]['system'][0]['cache_control'])
                self.assertTrue(all(t['name'] in copilot.READ_TOOLS|{'propose_patch'} for t in model.requests[0]['tools']))
                applied=copilot.apply(self.svc,result['id'],True)
                self.assertEqual(applied['version'],2)
                self.assertEqual(applied['author'],'copilot')
                self.assertEqual(copilot.apply(self.svc,result['id'],True)['version'],2)
                self.assertTrue(self.svc.validate(applied['spec'])['valid'])
                if i==0:
                    self.assertEqual(applied['spec']['client_faults'][1]['details']['with_fault'],'checkout-502')
                if i==3:
                    self.assertEqual(applied['spec']['web_performance'][0]['targets']['lcp'],3800)

    def test_key_gate_invalid_capability_and_stale_apply(self):
        saved=self.svc.create_spec(self.spec)
        with patch('server.copilot.secrets.get',return_value=None):
            with self.assertRaisesRegex(ValueError,'API key'):
                asyncio.run(copilot.turn(self.svc,saved['id'],'Change traffic'))
        with self.assertRaises(ValueError):
            copilot.proposal(self.svc,saved,{'patch':[{'op':'add','path':'/client_faults/-','value':{'id':'safari-cls','type':'layout_instability','rate':1,'filters':{'browser_families':['safari']}}}], 'summary':'Unsupported'})
        result=self.store.put('copilot_turns',dict(copilot.proposal(self.svc,saved,{'patch':[],'summary':'Current state'}),state='proposed',spec_id=saved['id']))
        self.svc.patch_spec(saved['id'],[{'op':'replace','path':'/name','value':'Current traffic'}])
        with self.assertRaises(ValueError):
            copilot.apply(self.svc,result['id'],True)
        copilot.discard(self.svc,result['id'])
        with self.assertRaises(ValueError):
            copilot.apply(self.svc,result['id'],True)
