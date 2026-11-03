"""Inspect serialized payloads independently of planned status/parent claims."""
from .registry import SERVICES


def attributes(rows):
    return {a['key']:next(iter(a['value'].values())) if isinstance(a['value'],dict) else a['value'] for a in rows}


def validate_serialized(session, deliveries):
    g=session.execution
    g.validate()
    frontend={};backend={};logs=[]
    for d in deliveries:
        p=d['payload']
        if d.get('fmt')=='envelope':
            for s in p['data'].get('spans',[]):
                a=attributes(s.get('attributes',[]))
                if a.get('emb.type')=='perf.network_request':
                    frontend[s['span_id']]={'traceId':s['trace_id'],'spanId':s['span_id'],'attributes':a,
                        'start':int(s['start_time_unix_nano']),'end':int(s['end_time_unix_nano'])}
        for rs in p.get('resourceSpans',[]):
            resource=attributes(rs['resource'].get('attributes',[]))
            for scope in rs['scopeSpans']:
                for s in scope['spans']:
                    a=attributes(s.get('attributes',[]))
                    if d['target']=='otlp':
                        if resource.get('service.name') not in SERVICES:raise ValueError('Unknown serialized service')
                        backend[s['spanId']]=s
                    elif a.get('emb.type')=='perf.network_request':
                        frontend[s['spanId']]={**s,'attributes':a,'start':int(s['startTimeUnixNano']),'end':int(s['endTimeUnixNano'])}
        for rs in p.get('resourceLogs',[]):
            logs.extend(row for scope in rs['scopeLogs'] for row in scope['logRecords'])
    for r in g.requests:
        node=g.get(r.client);sp=frontend.get(g.span_id(node.key))
        if not sp:raise ValueError('Missing emitted frontend request')
        a=sp['attributes']
        if sp['traceId']!=g.trace_id(node.trace):raise ValueError('Client trace mismatch')
        status=a.get('http.response.status_code')
        if (int(status) if status is not None else None)!=r.status:raise ValueError('Emitted client response differs')
        if r.transport and status is not None:raise ValueError('Fabricated HTTP status on transport error')
        if abs((sp['end']-sp['start'])/1e6-(node.end-node.start))>1:
            raise ValueError('Serialized client duration differs from the graph')
        if g.span_id(node.key) in backend:raise ValueError('Frontend span duplicated outside NSF')
        for n in g.spans:
            if n.parent!=node.key:continue
            if session.traces:
                b=backend.get(g.span_id(n.key))
                if not b or b.get('parentSpanId')!=sp['spanId'] or b['traceId']!=sp['traceId']:raise ValueError('Broken remote parent')
    for n in g.spans:
        if n.service=='frontend' or not session.traces:continue
        b=backend.get(g.span_id(n.key))
        if not b:raise ValueError('Missing serialized backend span')
        a=attributes(b['attributes'])
        expected_parent=g.span_id(n.parent) if n.parent else None
        if b.get('parentSpanId')!=expected_parent or b['traceId']!=g.trace_id(n.trace):
            raise ValueError('Serialized parent or trace mismatch')
        expected_links={(g.trace_id(g.get(k).trace),g.span_id(k)) for k in n.links}
        if {(l['traceId'],l['spanId']) for l in b.get('links',[])}!=expected_links:
            raise ValueError('Serialized asynchronous links mismatch')
        if a.get('demo.scenario_id')!=g.scenario:raise ValueError('Lost scenario metadata')
        if b['status']['code']!=(2 if n.error else 0):raise ValueError('Serialized status mismatch')
        if abs((int(b['endTimeUnixNano'])-int(b['startTimeUnixNano']))/1e6-(n.end-n.start))>.001:raise ValueError('Backend clock mismatch')
        if n.diagnostic and not any(l.get('spanId')==b['spanId'] and l.get('traceId')==b['traceId'] for l in logs):raise ValueError('Diagnostic missing trace context')
    for sp in frontend.values():
        if 'data.emb-api.com/' in sp['attributes'].get('url.full',''):raise ValueError('Exporter noise in user timeline')
    return {'requests':len(g.requests),'backend_spans':len(backend),'remote_parents':'valid'}


def observed_spans(payload):
    """Normalize the documented Data API base64 IDs and OTLP/JSON IDs."""
    import base64
    import re
    def identity(value):
        if not value:return ''
        if re.fullmatch(r'[0-9a-fA-F]{16}|[0-9a-fA-F]{32}',value):return value.lower()
        try:return base64.b64decode(value,validate=True).hex()
        except (ValueError,TypeError):return ''
    rows=[]
    def walk(value):
        if isinstance(value,dict):
            if ('spanId' in value or 'span_id' in value) and ('traceId' in value or 'trace_id' in value):
                raw=value.get('attributes',[])
                parent=identity(value.get('parentSpanId',value.get('parent_span_id')))
                rows.append({'id':identity(value.get('spanId',value.get('span_id'))),
                    'trace':identity(value.get('traceId',value.get('trace_id'))),
                    'parent':parent if parent.strip('0') else '',
                    'links':[(identity(l.get('traceId',l.get('trace_id'))),identity(l.get('spanId',l.get('span_id')))) for l in value.get('links',[])],
                    'attrs':attributes(raw) if isinstance(raw,list) else raw})
            # A link carries IDs but is not an observed span. Never count it as retention.
            for key,v in value.items():
                if key!='links':walk(v)
        elif isinstance(value,list):
            for v in value:walk(v)
    walk(payload)
    return rows


def verify_returned_trace(graph_manifest, trace_payload):
    """Use a retrieved destination response; never generate a session URL."""
    from urllib.parse import urlsplit, unquote
    rows=observed_spans(trace_payload)
    results=[]
    for req in graph_manifest['requests']:
        expected=[s for s in graph_manifest['graph'] if s['trace_id']==req['trace_id'] and s['service']!='frontend']
        actual={s['id']:s for s in rows if s['trace']==req['trace_id']}
        if req['expected_absence']:
            results.append({'request':req['key'],'state':'expected_absence',
                'unexpected_backend_spans':len([s for s in actual.values() if s['id']!=req['client_span_id']])});continue
        span=actual.get(req['client_span_id'])
        link=(span or {}).get('attrs',{}).get('emb.dashboard_session')
        parents=all(node['span_id'] in actual and actual[node['span_id']]['parent']==
            next((n['span_id'] for n in graph_manifest['graph'] if n['key']==node['parent']), '') for node in expected)
        u=urlsplit(link or '')
        candidate_ids=[graph_manifest.get('session_id','')]+graph_manifest.get('part_ids',[])
        intended=any(sid and sid.lower() in unquote(link or '').lower() for sid in candidate_ids)
        valid_link=(u.scheme=='https' and u.hostname=='dash.embrace.io' and not u.username and not u.password
            and u.path.startswith('/app/'+graph_manifest['app_id']+'/'))
        results.append({'request':req['key'],'trace_id':req['trace_id'],'client_span_present':bool(span),
            'remote_parent_verified':parents,'backend_spans_expected':len(expected),
            'backend_spans_observed':sum(n['span_id'] in actual for n in expected),
            'session_url':link if valid_link else None,'intended_session_verified':bool(valid_link and intended),
            'state':'backlink_observed' if span and parents and valid_link and intended else 'unverified',
            'dashboard_navigation_verified':False})
    request_traces={r['trace_id'] for r in graph_manifest['requests']}
    for tid in sorted({s['trace_id'] for s in graph_manifest['graph'] if s['service']!='frontend'}-request_traces):
        expected=[s for s in graph_manifest['graph'] if s['trace_id']==tid and s['service']!='frontend']
        actual={s['id']:s for s in rows if s['trace']==tid}
        nodes={n['key']:n for n in graph_manifest['graph']}
        present=all(n['span_id'] in actual for n in expected)
        parents=present and all(actual[n['span_id']]['parent']==(nodes[n['parent']]['span_id'] if n['parent'] else '') for n in expected)
        linked=present and all({(nodes[k]['trace_id'],nodes[k]['span_id']) for k in n['links']} <= set(actual[n['span_id']]['links']) for n in expected)
        results.append({'request':'asynchronous processing','trace_id':tid,'state':'observed' if parents and linked else 'unverified',
            'parents_verified':parents,'links_verified':linked,
            'backend_spans_expected':len(expected),'backend_spans_observed':sum(n['span_id'] in actual for n in expected),
            'dashboard_navigation_verified':False})
    return results
