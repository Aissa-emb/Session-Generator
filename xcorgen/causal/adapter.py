"""Render one precomputed execution using the established three wire contracts."""
import copy
import json
from types import SimpleNamespace
from .. import otlp, limits
from ..android_session import AndroidSession, SessionEnded, kv, nanos
from ..browser_session import BrowserSession
from ..ios_session import IOSSession
from ..calls import Call
from ..personas import _new_persona, _new_android_persona, _new_ios_persona
from ..profiles import load, Page, Endpoint
from ..web_sites import Page as WebPage
from .model import ident, stream
from .registry import ROUTES, SERVICES, SCHEMA_URL, VERSION
from .scenarios import build

SCOPE={'name':'vantra.causal-simulator', 'version':VERSION}


def epoch_ns(base, relative):
    # Avoid float multiplication of epoch seconds/nanoseconds.
    return str(round(base)*1_000_000 + round(relative*1_000_000))


class Trace:
    def __init__(self, graph, trace, base, metadata):
        self.spans=[]; self.logs=[]; self.resources={}
        causal_service=graph.get(graph.causal_node).service if graph.causal_node else None
        for n in graph.spans:
            if n.service=='frontend' or n.trace!=trace: continue
            version=graph.config.get('service_release','2026.10.2') if graph.variant=='affected' and n.service==causal_service else SERVICES[n.service].version
            resource={'service.name':n.service,'service.namespace':'vantra','service.version':version,
                'service.instance.id':ident('vantra-process',0,n.service+version,8),
                'deployment.environment.name':'synthetic','cloud.region':graph.config.get('region','us-east-1'),
                'telemetry.sdk.name':'session-generator','telemetry.sdk.language':'python','telemetry.sdk.version':VERSION,
                'demo.synthetic':True}
            self.resources[n.service]={'attributes':otlp.attrs(resource)}
            attrs=dict(metadata, **n.attributes)
            if n.error:attrs['error.type']=n.error
            s={'traceId':graph.trace_id(n.trace),'spanId':graph.span_id(n.key),'name':n.name,'kind':n.kind,
                'startTimeUnixNano':epoch_ns(base,n.start),'endTimeUnixNano':epoch_ns(base,n.end),
                'attributes':otlp.attrs(attrs),'status':{'code':2 if n.error else 0},'flags':257}
            if n.parent:s['parentSpanId']=graph.span_id(n.parent)
            if n.links:s['links']=[{'traceId':graph.trace_id(graph.get(k).trace),'spanId':graph.span_id(k),'flags':257} for k in n.links]
            if n.diagnostic:
                self.logs.append((n.service, {'timeUnixNano':epoch_ns(base,n.end),'observedTimeUnixNano':epoch_ns(base,n.end),
                    'traceId':s['traceId'],'spanId':s['spanId'],'severityNumber':17 if n.error else 9,
                    'severityText':'ERROR' if n.error else 'INFO','body':{'stringValue':n.diagnostic},
                    'attributes':otlp.attrs(attrs)}))
            self.spans.append((n.service,None,SCOPE,s))

    def traces_request(self):
        return {'resourceSpans':[{'resource':r,'schemaUrl':SCHEMA_URL,'scopeSpans':[{'scope':SCOPE,
            'spans':[s for service,_,_,s in self.spans if service==key]}]} for key,r in self.resources.items()]}

    def logs_request(self):
        return {'resourceLogs':[{'resource':self.resources[key],'schemaUrl':SCHEMA_URL,'scopeLogs':[{'scope':SCOPE,
            'logRecords':[r for service,r in self.logs if service==key]}]} for key in dict(self.logs)]} if self.logs else None


def planned_call(session, key, start):
    g=session.execution
    r=next(r for r in g.requests if r.key==key)
    node=g.get(r.client)
    expected=session.causal_base+node.start
    if abs(start-expected)>1:raise ValueError('Causal request started outside its planned interval')
    method,route,_,_=ROUTES[r.route]
    path=route.replace('{accountId}','a000000000000001').replace('{transactionId}','b000000000000001').replace('{operationId}',g.operation_id())
    ep=Endpoint(method,route,path,None)
    tb=Trace(g,node.trace,session.causal_base,session.causal_metadata)
    c=Call(ep,'https://'+session.api_host+path,r.status,'ok' if r.status and r.status<400 else 'error',r.response,
        g.trace_id(node.trace),g.span_id(node.key),{},expected,session.causal_base+node.end,tb if tb.spans else None)
    c.transport=r.transport;c.context=session.causal_metadata;c.companions=[]
    c.request_bytes=240 if method=='POST' else 0
    c.response_bytes=0 if r.transport else 320+len(json.dumps(r.response))
    evidence={'key':key,'status':r.status,'trace_id':c.trace_id,'client_span_id':c.span_id,'method':method,
        'route':route,'url':c.url,'traceparent':c.traceparent,'start':c.start,'end':c.end,'part_id':session.part.id,
        'incident':g.scenario if g.variant=='affected' else None,'context':c.context,'transport':c.transport,'data':r.response,
        'expected_absence':r.expected_absence,'first_backend_span_id':tb.spans[0][3]['spanId'] if tb.spans else None,
        'backend_errors':sum(s['status']['code']==2 for _,_,_,s in tb.spans)}
    session.plan.evidence.append(evidence)
    return c


def client_exception(s, event, platform):
    contract=event.name=='contract'; fatal=event.kind=='fatal'
    attrs={'response.schema':'transaction-detail-v2','response.merchant.display_name':'null'} if contract else {'view.state':'unavailable'}
    if platform=='web':
        s.exception(handled=True, family='contract_null' if contract else 'view_state', **attrs)
    elif platform=='android':
        typ='java.lang.NullPointerException' if fatal else 'kotlinx.serialization.MissingFieldException' if contract else 'java.lang.IllegalStateException'
        message='merchant.displayName must not be null' if contract else 'Transfer view state is unavailable'
        fn='TransactionDetailMapper.map(TransactionDetailMapper.kt:42)' if contract else 'TransferViewModel.buildState(TransferViewModel.kt:68)'
        frames=['com.vantra.money.'+fn,'com.vantra.money.TransactionDetailViewModel.onResponse(TransactionDetailViewModel.kt:91)',
            'kotlin.coroutines.jvm.internal.BaseContinuationImpl.resumeWith(ContinuationImpl.kt:33)',
            'kotlinx.coroutines.DispatchedTask.run(DispatchedTask.kt:100)']
        # Null is not a missing key. A serializer rejecting explicit null uses JsonDecodingException.
        if contract and not fatal:typ='kotlinx.serialization.json.internal.JsonDecodingException'
        if fatal:s._crash(typ,message,frames)
        else:
            attrs.update(s._log_common());attrs.update({'emb.type':'sys.exception','emb.exception_handling':'handled',
                'emb.private.send_mode':'IMMEDIATE','exception.type':typ.rsplit('.',1)[-1],
                'exception.message':message,'exception.stacktrace':json.dumps(frames)})
            s.part.logs.append((s.t,{'time_unix_nano':nanos(s.t),'severity_number':17,'severity_text':'ERROR','body':message,'attributes':kv(attrs)}))
    else:
        message='valueNotFound(Swift.String, codingPath: merchant.displayName)' if contract else 'Transfer view state is unavailable'
        if fatal:s._crash('SwiftFatalError','Unexpectedly found nil while unwrapping an Optional value',
            ['VantraMoney.TransactionDetailMapper.map(_:) (TransactionDetailMapper.swift:42)'])
        else:
            # Apple captures a caught Swift error through the public log API; it is not a crash record.
            s.log('error',message,**attrs,**{'exception.type':'Swift.DecodingError.valueNotFound' if contract else 'VantraMoney.ViewStateError',
                'exception.message':message,'error.domain':'VantraMoney.ResponseContract','error.code':1002})


def create(target, spec, start_ms, index, namespace, choice=None):
    config=copy.deepcopy(spec['causal'])
    seed=spec.get('seed') if spec.get('seed') is not None else 7331
    scenario,variant=choice or select(config,seed,index,start_ms)
    platform=target['platform']; rng=stream(seed,index,'platform:'+platform)
    graph=build(scenario,variant,platform,seed,index,namespace,config)
    profile=copy.deepcopy(load(graph.domain))
    make={'web':_new_persona,'android':_new_android_persona,'ios':_new_ios_persona}[platform]
    persona=make(graph.domain,rng)
    persona['traits'] = {'segment':'mass','mfa':'push','tier':'silver','membership':'standard'}
    rng.identity_rng = stream(namespace,index,'sdk-identity:'+platform)
    persona['ip']='192.0.2.42'
    network=config.get('network','wifi')
    persona.update(rtt={'wifi':35,'cellular':95,'constrained':220}[network],mbps={'wifi':60,'cellular':15,'constrained':3}[network])
    # Explicit synthetic stable identities, never personal account data.
    persona['user_id']=ident('synthetic-personas',seed,str(index%64))
    if scenario=='signin-outage':persona['user_id']=None
    persona['device_id']=ident('synthetic-devices',seed,platform+str(index%64)).upper()
    from ..spec_runner import app_overrides
    overrides=app_overrides(target,spec)[graph.domain]
    if platform == 'web':
        from ..browser_session import releases
        versions=[r['version'] for r in releases(target['web'],start_ms)]
    else:
        versions=[v['version'] for v in target[platform]['versions']]
    build_version=versions[0] if variant=='affected' else versions[-1]
    if platform=='web':
        origin=target['web']['estate'][graph.domain]['origin'].rstrip('/')
        persona['web_origins']={origin:{'device_id':ident('synthetic-web-devices',seed,origin+str(index%64)).upper(),
            'user_session_number':index//64,'session_part_number':index//64,'previous_user_session_id':'','exception_number':0,'external_user_id':None}}
        s=BrowserSession(profile,persona,target['embrace_app_id'],start_ms,rng=rng,web=target['web'],release=build_version,
            api_host=target['api_domains'][graph.domain],app_name=target['app_name'],environment=target['environment'])
        s.pages=copy.deepcopy(s.pages)
        for page in s.pages.values():
            # Page paint measures the shell. Business data rendering belongs to
            # the graph and must not get an unrelated random completion time.
            page.elements=()
            page.measures=()
            page.lcp=('main h1','h1',None)
        s.pages['detail']=WebPage('detail','/money/transactions/{transactionId}','detail','client','Transaction details',path=lambda p:'/money/transactions/b000000000000001',chunk='account')
        s.web['sdk_upload_spans']=False
    else:
        overrides=dict(overrides,ui='views',screens=dict(profile.android['screens']))
        overrides['screens'].update(dashboard='AccountsOverviewFragment' if platform=='android' else 'AccountsOverviewViewController',
            account='TransactionHistoryFragment' if platform=='android' else 'TransactionHistoryViewController',
            detail='TransactionDetailFragment' if platform=='android' else 'TransactionDetailViewController')
        profile.pages['detail']=Page('/transactions/{transactionId}','Transaction details')
        cls=AndroidSession if platform=='android' else IOSSession
        version=(build_version,'1031001' if variant=='affected' else '1031002','9.3.1' if platform=='android' else '6.21.0')
        s=cls(profile,persona,target['embrace_app_id'],start_ms,rng=rng,shared=True,app=overrides,version=version)
    s.plan=SimpleNamespace(evidence=[],web_faults=[],primary='clean',client_fault=None,ring='stable',crash_family=0)
    s.forced_cold_start=True
    # These are the requested scenario population, not legacy out-of-band probes.
    s.verification_case=None
    s.traces=spec['destinations']['mode']=='end_to_end'
    s.execution=graph; s.causal_metadata=graph.metadata(s.app_version)
    s.properties.update({k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in s.causal_metadata.items()})
    first='signin' if scenario=='signin-outage' else 'home' if graph.domain=='travel' else 'dashboard'
    s.page_view(first)
    s.causal_base=round(s.t)
    s.t=s.causal_base
    queue=[(e.at,0,e) for e in graph.events]+[(graph.get(r.client).start,1,r) for r in graph.requests]
    # Non-network client work is derived from the same graph, never marked on the successful HTTP span.
    for n in graph.spans:
        if n.service=='frontend' and n.kind==1:queue.append((n.end,2,n))
    try:
        for offset,kind,item in sorted(queue,key=lambda row:(row[0],row[1])):
            when=s.causal_base+offset
            if s.t>when+1:raise ValueError(f'Frontend action exceeded planned gap: {item}')
            s.t=when
            if kind==1:
                s._request(item.key,{},when)  # no independently randomized call/outcome
                # _request emits the completed span; following events occur at their planned time.
            elif kind==2:
                attrs={**s.causal_metadata,**item.attributes,'emb.type':'perf','app.operation':item.name}
                ids={'trace_id':graph.trace_id(item.trace),'span_id':graph.span_id(item.key)}
                if item.error: attrs['error.type']=item.error
                if platform=='web':
                    span=s._span('traces',item.name,s.causal_base+item.start,when,attrs,status=2 if item.error else 0,**ids)
                    span['links']=[{'traceId':graph.trace_id(graph.get(k).trace),'spanId':graph.span_id(k),'attributes':[]} for k in item.links]
                else:
                    span=s._span(item.name,s.causal_base+item.start,when,attrs,status='Error' if item.error else 'Unset',**ids)
                    if platform=='ios':span['links']=[{'trace_id':graph.trace_id(graph.get(k).trace),'span_id':graph.span_id(k),'attributes':[]} for k in item.links]
            elif item.kind=='click':
                s.click('<button>'+item.name+'</button>')
                # Adapter's ordinary think time is replaced by the graph's explicit quiet intervals.
                s.t=when
            elif item.kind=='blocked_click' and platform=='web':
                s.page['vitals'].inp=item.data['duration']
                s.click('<button>'+item.name+'</button>')
                if s.caps['loaf'] and s.part.frames:
                    s.part.frames[-1]['script']='https://support.example/widget.js'
                s.t=when
            elif item.kind=='provider' and platform=='web':
                endpoint='https://analytics.example/collect' if item.name=='analytics' else 'https://support.example/bootstrap'
                s._span('fetch','POST' if item.name=='analytics' else 'GET',when,when+item.data['duration'],
                    {'http.request.method':'POST' if item.name=='analytics' else 'GET','url.full':endpoint,
                     'server.address':endpoint.split('/')[2],'server.port':443,'http.response.status_code':204 if item.name=='analytics' else 200,
                     'emb.type':'perf.network_request'},kind=3)
            elif item.kind=='navigate':
                params={'accountId':'a000000000000001','account':'a000000000000001','label':'Checking','city':'Sydney','checkin':'2026-11-14','checkout':'2026-11-17'}
                s.page_view(item.name,**params)
            elif item.kind in ('exception','fatal'):client_exception(s,item,platform)
            else:
                if item.name=='Signed in':s.login(ident('synthetic-personas',seed,str(index%64)))
                s.breadcrumb(item.name)
        s.t=max(s.t,s.causal_base+graph.final_ms)
    except SessionEnded:
        pass
    # Export asynchronous worker traces separately, preserving links and completion time.
    if s.traces:
        for trace in sorted({n.trace for n in graph.spans if n.service!='frontend'}-{graph.get(r.client).trace for r in graph.requests}):
            tb=Trace(graph,trace,s.causal_base,s.causal_metadata)
            due=max(int(x[3]['endTimeUnixNano']) for x in tb.spans)/1e6+1000
            s._deliver(due,'otlp','traces',tb.traces_request())
            if tb.logs_request():s._deliver(due,'otlp','logs',tb.logs_request())
    deliveries=s.finish()
    s.causal_evidence=graph.manifest()
    s.causal_evidence['lifecycle']['process_id']=getattr(s,'process_id',getattr(s,'instance_id',None))
    s.causal_evidence['lifecycle']['launch']='document_load' if platform=='web' else 'cold_start'
    s.causal_evidence.update(session_id=s.user_session_id,device_id=getattr(s,'device_id',s.persona['device_id']),
        app_id=s.app_id,base_ms=s.causal_base,session_properties=s.properties,frontend_requests=s.plan.evidence,
        part_ids=getattr(s,'sdk_session_ids',[])+[p.id for p in getattr(s,'parts',[])])
    s.causal_evidence['evidence']['fixture_validated']=False # serialized validator sets this after checking
    from .validation import validate_serialized
    validate_serialized(s,deliveries)
    s.causal_evidence['evidence']['fixture_validated']=True
    return s,scenario,deliveries


def select(config,seed,index,start_ms):
    scenario=config['scenario'];variant=config.get('variant','affected')
    rng=stream(seed,index,'cohort')
    if config.get('mode','single')=='mixed':
        weights=config.get('mix',{'healthy':70,'hero':15,'slow':10,'other':5})
        choice=rng.choices(list(weights),weights=list(weights.values()))[0]
        if choice=='healthy':return ('travel-provider' if scenario=='travel-provider' else 'healthy'),'control'
        if choice=='slow':return ('travel-provider' if scenario=='travel-provider' else 'slow-history'),'affected'
        if choice=='other':return scenario,'recovered'
    if variant=='scheduled':
        from ..spec import parse_time
        start=parse_time(config['incident_start']);end=start+config.get('incident_minutes',20)*60000
        targeted=rng.random()<config.get('cohort_share',.5)
        variant='control' if start_ms<start or not targeted else 'recovered' if start_ms>=end else 'affected'
    return scenario,variant
