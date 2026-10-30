"""Run inside an isolated, network-disabled container against its actual HTTP API."""
import json
import os
import time
import urllib.request
from server.store import Store


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    if os.getuid()==0:
        raise RuntimeError('Container must run as non-root.')
    store=Store()
    access=store.create_token('Container acceptance')
    def call(path,value=None):
        req=urllib.request.Request('http://127.0.0.1:8000'+path,data=json.dumps(value).encode() if value is not None else None,
            headers={'Authorization':'Bearer '+access['token'],'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=120) as response:  # nosec B310 -- fixed loopback HTTP receiver
            return json.load(response)
    try:
        require(call('/health')['status']=='ok','Health endpoint failed')
        s=call('/api/capabilities')['default_spec']
        s['traffic']={'mode':'burst','count':4,'window':{'last_minutes':8}}
        saved=call('/api/specs',s)
        job=call('/api/runs',{'spec_id':saved['id'],'confirm':True,'dry_run':True})
        for _ in range(90):
            run=call('/api/runs/'+job['id'])
            if run['status'] in ('completed','failed'):
                break
            time.sleep(1)
        require(run['status']=='completed','Bounded run did not complete')
        require(run['receipts'] and all(r['status']=='accepted' for r in run['receipts']), 'Missing accepted receipts')
        s['traffic']={'mode':'live','rate_per_minute':60,'duration_minutes':1}
        saved=call('/api/specs',s)
        stream=call('/api/streams',{'spec_id':saved['id'],'name':'Container acceptance','confirm':True,'dry_run':True})
        for _ in range(30):
            state=call('/api/streams/'+stream['id'])
            if state['summary']['health']=='healthy':
                break
            time.sleep(1)
        require(state['summary']['health']=='healthy','Live stream did not become healthy')
        call('/api/streams/'+stream['id']+'/pause',{})
        call('/api/streams/'+stream['id']+'/resume',{'confirm':True})
        call('/api/streams/'+stream['id']+'/stop',{})
        print('PASS: non-root container, health, authenticated API, dry delivery, live stream and controls')
    finally:
        store.update('api_tokens',access['id'],revoked=True)
        store.engine.dispose()


if __name__=='__main__':
    main()
