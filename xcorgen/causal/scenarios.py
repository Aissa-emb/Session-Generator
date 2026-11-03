"""Business executions. All offsets are fixture parameters, not measurements."""
from .model import Execution, Request, stream
from .registry import ROUTES, SCENARIOS


def request(g, key, route, at, duration, status=200, transport=None):
    method, public, service, internal = ROUTES[route]
    c = g.add(key + '.client', 'frontend', method, 3, at, at + duration, trace=key,
        attributes={'http.request.method': method, **({'http.response.status_code': status} if status else {})},
        error=transport or (str(status) if status and status >= 400 else None))
    r = Request(key, route, c.key, status, transport, operation=g.operation_id())
    g.requests.append(r)
    def add(label, owner, name, kind, start, end, parent='client', **kw):
        attrs = kw.pop('attributes', {})
        return g.add(key + '.' + label, owner, name, kind, at + start, at + end,
            key + '.' + parent if parent else None, trace=kw.pop('trace', key), attributes=attrs, **kw)
    return r, add


def http(method, route, status, server=True, host=None):
    return {'http.request.method': method, 'http.response.status_code': status,
            **({'http.route': route} if server else {'server.address': host, 'url.full': 'https://' + host + route}),
            **({'error.type': str(status)} if status >= (500 if server else 400) else {})}


def cache(add, label, service, parent, start, end, name, outcome='miss', command='GET'):
    return add(label, service, command, 3, start, end, parent,
        attributes={'db.system.name': 'redis', 'db.operation.name': command, 'server.address': name + '.redis.internal',
                    'demo.cache.name': name, 'demo.cache.outcome': outcome})


def sql(add, label, service, parent, start, end, table, operation='SELECT', rows=1):
    statement = {'SELECT': f'SELECT id FROM {table.split()[0]} WHERE owner_id = $1 LIMIT $2',  # nosec B608 -- emitted fixture text, never executed
                 'INSERT': f'INSERT INTO {table} (operation_id) VALUES ($1)',  # nosec B608 -- emitted fixture text, never executed
                 'UPDATE': f'UPDATE {table} SET state = $1 WHERE operation_id = $2'}[operation]  # nosec B608 -- emitted fixture text, never executed
    if table == 'accounts account_balances':
        statement = 'SELECT a.id, b.available_minor FROM accounts a JOIN account_balances b ON a.id = b.account_id WHERE a.owner_id = $1'
    return add(label, service, operation + ' ' + table, 3, start, end, parent,
        attributes={'db.system.name': 'postgresql', 'db.namespace': service.replace('-', '_'),
            'db.operation.name': operation, 'db.query.summary': operation + ' ' + table,
            'db.query.text': statement, 'db.response.returned_rows': rows, 'server.address': service + '.postgres.internal'})


def overview(g, at=2500, warm=False):
    r, a = request(g, 'overview', 'overview', at, 310 if not warm else 190)
    finish = 0 if not warm else 120
    a('edge', 'vantra-edge', 'GET /api/v1/accounts', 2, 25, 275-finish,
      attributes=http('GET', '/api/v1/accounts', 200))
    a('auth', 'vantra-edge', 'identity.verify_access_token', 1, 30, 33, 'edge')
    cache(a, 'limit', 'vantra-edge', 'edge', 34, 36, 'account-rate-limit', 'hit', 'EVALSHA')
    a('out', 'vantra-edge', 'GET', 3, 40, 265-finish, 'edge', attributes=http('GET', '/internal/v1/account-overviews', 200, False, 'account-overview-api'))
    a('api', 'account-overview-api', 'GET /internal/v1/account-overviews', 2, 45, 260-finish, 'out', attributes=http('GET', '/internal/v1/account-overviews', 200))
    rpc = 'vantra.customer.v1.DirectoryService/GetProfile'
    rpcattrs = {'rpc.system': 'grpc', 'rpc.service': 'vantra.customer.v1.DirectoryService', 'rpc.method': 'GetProfile', 'rpc.grpc.status_code': 0}
    a('profile-out', 'account-overview-api', rpc, 3, 50, 104, 'api', attributes=dict(rpcattrs, **{'server.address': 'customer-directory'}))
    a('profile', 'customer-directory', rpc, 2, 55, 100, 'profile-out', attributes=rpcattrs)
    cache(a, 'profile-cache', 'customer-directory', 'profile', 60, 63, 'customer-profile', 'hit')
    route = '/internal/v1/customers/{customerId}/accounts'
    a('portfolio-out', 'account-overview-api', 'GET', 3, 52, 240-finish, 'api', attributes=http('GET', route, 200, False, 'account-portfolio'))
    a('portfolio', 'account-portfolio', 'GET '+route, 2, 57, 235-finish, 'portfolio-out', attributes=http('GET', route, 200))
    cache(a, 'accounts-cache', 'account-portfolio', 'portfolio', 63, 65, 'account-portfolio', 'hit' if warm else 'miss')
    if not warm:
        a('pool', 'account-portfolio', 'accounts.read_pool.acquire', 1, 66, 73, 'portfolio')
        sql(a, 'query', 'account-portfolio', 'portfolio', 74, 218, 'accounts account_balances', rows=3)
        cache(a, 'fill', 'account-portfolio', 'portfolio', 220, 223, 'account-portfolio', 'stored', 'SET')
    a('map', 'account-portfolio', 'accounts.balance_projection.map', 1, 225-finish, 231-finish, 'portfolio')
    a('compose', 'account-overview-api', 'accounts.overview.compose', 1, 242-finish, 253-finish, 'api',
      dependencies=['overview.profile-out', 'overview.portfolio-out'])
    r.response = {'account_count': 3}
    return r


def signin(g, at=2500):
    bad = g.variant == 'affected'
    status = 503 if bad else 200
    cut = 0 if bad else 2900
    r, a = request(g, 'signin', 'signin', at, 3130-cut, status)
    public = ROUTES['signin'][1]
    def h(label, service, name, kind, start, end, parent, route, host=None):
        return a(label, service, name, kind, start, end, parent,
                 attributes=http('POST', route, status, kind==2, host), error='503' if bad else None)
    h('edge', 'vantra-edge', 'POST '+public, 2, 35, 3090-cut, 'client', public)
    a('limit', 'vantra-edge', 'edge.enforce_login_rate_limit', 1, 40, 44, 'edge')
    cache(a, 'redis', 'vantra-edge', 'limit', 41, 43, 'login-rate-limit', 'hit', 'EVALSHA')
    h('out', 'vantra-edge', 'POST', 3, 48, 3083-cut, 'edge', '/internal/v1/sessions', 'identity-sessions')
    h('identity', 'identity-sessions', 'POST /internal/v1/sessions', 2, 52, 3078-cut, 'out', '/internal/v1/sessions')
    a('authenticate', 'identity-sessions', 'identity.authenticate_credentials', 1, 57, 3074-cut, 'identity', error='ConnectionAcquisitionTimeout' if bad else None)
    cache(a, 'policy', 'identity-sessions', 'authenticate', 60, 62, 'login-policy', 'miss')
    a('pool', 'identity-sessions', 'identity.session_pool.acquire', 1, 65, 3065 if bad else 75, 'authenticate',
        error='ConnectionAcquisitionTimeout' if bad else None,
        diagnostic='Session store connection acquisition exceeded 3000 ms' if bad else None,
        attributes={'pool.name': 'identity-session', 'pool.max_connections': 24,
            'pool.active_connections': 24 if bad else 8, 'pool.waiters': 12 if bad else 0, 'pool.acquire.timeout_ms': 3000})
    if not bad:
        sql(a, 'credentials', 'identity-sessions', 'authenticate', 78, 110, 'authentication_records')
        a('issue', 'identity-sessions', 'identity.issue_session', 1, 115, 158, 'authenticate')
        sql(a, 'persist', 'identity-sessions', 'issue', 120, 150, 'authentication_sessions', 'INSERT')
    else: g.causal_node = 'signin.pool'
    return r


def simple(g, key, route, at, duration=220, status=200, transport=None):
    r, a = request(g, key, route, at, duration, status if not transport else None, transport)
    method, public, service, internal = ROUTES[route]
    server_end = duration-35 if not transport else 250
    a('edge', 'vantra-edge', method+' '+public, 2, 30, server_end, attributes=http(method, public, status), error=str(status) if status>=500 else None)
    a('verify', 'vantra-edge', 'identity.verify_access_token', 1, 35, 38, 'edge')
    a('out', 'vantra-edge', method, 3, 40, server_end-5, 'edge', attributes=http(method, internal, status, False, service), error=str(status) if status>=400 else None)
    a('api', service, method+' '+internal, 2, 45, server_end-10, 'out', attributes=http(method, internal, status), error=str(status) if status>=500 else None)
    return r, a


def history(g, at=16500, slow=False, transport=None):
    # Deterministic reference is 2100 ms; other seeds vary within 1.8–2.8 s.
    query_ms = 2100 if g.config.get('reference', True) else round(stream(g.seed, g.index, 'query').triangular(1800, 2800, 2100))
    q = query_ms if slow else 35
    duration = q + 180
    if transport:
        duration = 5000
    r, a = request(g, 'history', 'history', at, duration, None if transport else 200, transport)
    method, public, svc, internal = ROUTES['history']
    end = q+145
    a('edge', 'vantra-edge', 'GET '+public, 2, 40, end, attributes=http('GET', public, 200))
    a('auth', 'vantra-edge', 'identity.verify_access_token', 1, 45, 48, 'edge')
    a('out', 'vantra-edge', 'GET', 3, 50, end-10, 'edge', attributes=http('GET', internal, 200, False, svc))
    a('api', svc, 'GET '+internal, 2, 55, end-15, 'out', attributes=http('GET', internal, 200))
    a('load', svc, 'transactions.history.load_page', 1, 60, q+110, 'api')
    cache(a, 'cache', svc, 'load', 65, 68, 'history-page', 'miss')
    a('pool', svc, 'transactions.read_pool.acquire', 1, 70, 78, 'load')
    n = sql(a, 'query', svc, 'load', 85, 85+q, 'posted_transactions', rows=g.config.get('result_count', 25))
    n.attributes.update({'query.plan': 'posted_at_scan' if slow else 'account_posted_at_index', 'db.query.text': 'SELECT id, posted_at, amount_minor FROM posted_transactions WHERE account_id = $1 AND posted_at >= $2 ORDER BY posted_at DESC LIMIT $3'})
    if slow:
        n.diagnostic = 'History page completed using posted_at_scan'
        g.causal_node = n.key
    a('map', svc, 'transactions.history.map_page', 1, q+88, q+100, 'load', dependencies=[n.key])
    a('serialize', svc, 'transactions.history.serialize', 1, q+112, q+122, 'api', dependencies=['history.load'])
    r.response = {'result_count': g.config.get('result_count', 25)}
    return r


def transfer(g, at=26500):
    lost = g.scenario == 'transfer-response-loss' and g.variant == 'affected'
    backlog = g.scenario == 'worker-backlog' and g.variant == 'affected'
    r, a = simple(g, 'submit', 'transfer', at, 5000 if lost else 290, 202, 'timeout' if lost else None)
    for node in g.spans:
        if node.key == 'submit.edge': node.end = at+250
        elif node.key == 'submit.out': node.end = at+245
        elif node.key == 'submit.api': node.end = at+240
    svc='transfer-orchestrator'
    a('validate', svc, 'transfers.validate_request', 1, 50, 65, 'api')
    a('limits', svc, 'transfers.check_limits', 1, 67, 85, 'api')
    a('commit', svc, 'transfers.persist_intent', 1, 100, 200, 'api', attributes={'transaction.atomic': True})
    sql(a, 'intent', svc, 'commit', 110, 130, 'transfer_intents', 'INSERT')
    sql(a, 'key', svc, 'commit', 132, 150, 'idempotency_records', 'INSERT')
    sql(a, 'outbox', svc, 'commit', 152, 185, 'outbox_events', 'INSERT')
    # A distinct asynchronous trace with a creation link, never a fabricated frontend root.
    queue = 12000 if backlog else 135
    consume_start = 365 + queue
    def asyncnode(label, owner, name, kind, start, end, parent=None, **kw):
        return a(label, owner, name, kind, start, end, parent, trace='processing', **kw)
    asyncnode('relay', 'transfer-outbox-relay', 'transfers.outbox.publish', 1, 320, 375, None, dependencies=['submit.commit'], links=['submit.commit'])
    asyncnode('claim', 'transfer-outbox-relay', 'SELECT outbox_events', 3, 325, 342, 'relay', attributes={'db.system.name':'postgresql', 'db.operation.name':'SELECT', 'db.query.summary':'SELECT outbox_events'})
    msg = {'messaging.system':'kafka', 'messaging.destination.name':'money.transfers.requested'}
    asyncnode('produce', 'transfer-outbox-relay', 'money.transfers.requested publish', 4, 350, 365, 'relay', attributes=dict(msg, **{'messaging.operation.name':'publish', 'messaging.operation.type':'send'}))
    asyncnode('consume', 'ledger-posting-worker', 'money.transfers.requested process', 5, consume_start, consume_start+400, None,
        links=['submit.produce'], dependencies=['submit.produce'], attributes=dict(msg, **{'messaging.operation.name':'process', 'messaging.operation.type':'process', 'queue.wait_ms':queue}))
    asyncnode('ledger', 'ledger-posting-worker', 'ledger.post_transfer', 1, consume_start+20, consume_start+350, 'consume', attributes={'transaction.atomic':True, 'ledger.logical_transactions':1, 'ledger.posting_rows':2})
    for label, table, lo, hi in [('transaction','ledger_transactions',40,100),('postings','ledger_postings',120,260)]:
        asyncnode(label, 'ledger-posting-worker', 'INSERT '+table, 3, consume_start+lo, consume_start+hi, 'ledger', attributes={'db.system.name':'postgresql','db.operation.name':'INSERT','db.query.summary':'INSERT '+table})
    asyncnode('posted', 'ledger-posting-worker', 'money.transfers.posted publish', 4, consume_start+365, consume_start+390, 'consume', dependencies=['submit.ledger'], attributes={'messaging.system':'kafka','messaging.destination.name':'money.transfers.posted','messaging.operation.type':'send','messaging.operation.name':'publish'})
    asyncnode('completion', svc, 'money.transfers.posted process', 5, consume_start+420, consume_start+470, None, links=['submit.posted'], dependencies=['submit.posted'], attributes={'messaging.system':'kafka','messaging.destination.name':'money.transfers.posted','messaging.operation.type':'process','messaging.operation.name':'process'})
    asyncnode('update', svc, 'UPDATE transfer_intents', 3, consume_start+430, consume_start+460, 'completion', attributes={'db.system.name':'postgresql','db.operation.name':'UPDATE','db.query.summary':'UPDATE transfer_intents'})
    complete=at+consume_start+470
    from .business import Ledger
    ledger=Ledger()
    ledger.accept('synthetic-owner',g.operation_id(),g.operation_id('idempotency'))
    ledger.accept('synthetic-owner',g.operation_id(),g.operation_id('idempotency'))
    ledger.post(g.operation_id())
    ledger.post(g.operation_id())  # at-least-once message redelivery has no second effect
    ledger.complete(g.operation_id())
    posted=ledger.transactions[g.operation_id()]
    g.effects={'operation_id':g.operation_id(), 'reference_created_at':at-1500, 'idempotency_key':g.operation_id('idempotency'),
        'transfer_intents':len(ledger.intents), 'ledger_transactions':len(ledger.transactions), **posted, 'completed_at':complete,
        'kind':'controlled_internal_book_transfer'}
    r.response = {} if lost else {'state':'Pending', 'operation_reference':g.operation_id()}
    g.event(at+5050 if lost else at+320, 'state', 'Checking status' if lost else 'Pending')
    polls = [5400] if lost else [1500, 5000, 10000, 14000] if backlog else [1500]
    for i, offset in enumerate(polls):
        state = 'Completed' if at+offset >= complete else 'Pending'
        rr, aa = simple(g, f'status{i}', 'status', at+offset, 140)
        aa('reconcile', svc, 'transfers.reconcile_operation', 1, 50, 85, 'api', attributes={'transfer.state':state})
        sql(aa, 'read', svc, 'reconcile', 55, 75, 'transfer_intents')
        rr.response={'state':state, 'operation_reference':g.operation_id()}
        g.event(at+offset+200, 'state', state)
    if g.config.get('retry'):
        rr, aa=simple(g,'retry','transfer',at+polls[-1]+1000,180,200)
        aa('idempotency',svc,'transfers.reconcile_operation',1,50,120,'api',attributes={'idempotency.outcome':'replayed'})
        sql(aa,'read',svc,'idempotency',55,100,'idempotency_records')
        rr.response={'state':'Completed','operation_reference':g.operation_id()}
    if lost: g.causal_node='submit.client'
    if backlog: g.causal_node='submit.consume'
    g.outcome='completed_after_reconciliation'
    return at+polls[-1]+3000


def travel(g, warm=False):
    r,a=request(g,'autocomplete','autocomplete',2500,95)
    a('edge','vantra-edge','GET /api/v3/locations',2,15,75,attributes=http('GET','/api/v3/locations',200))
    rpc='vantra.travel.catalog.v1.DestinationService/Autocomplete'
    rpcattrs={'rpc.system':'grpc','rpc.service':'vantra.travel.catalog.v1.DestinationService','rpc.method':'Autocomplete','rpc.grpc.status_code':0}
    a('out','vantra-edge',rpc,3,20,68,'edge',attributes=dict(rpcattrs,**{'server.address':'destination-catalog'}))
    a('catalog','destination-catalog',rpc,2,24,62,'out',attributes=rpcattrs)
    cache(a,'cache','destination-catalog','catalog',28,35,'destination-prefix','hit','ZRANGEBYLEX')
    bad=g.variant=='affected'
    partial=g.config.get('partial_results',True)
    status=200 if not bad or partial else 503
    r,a=simple(g,'search','search',16500,1800 if bad else 530,status)
    svc='travel-search-api'; end=1700 if bad else 430
    for label,owner,service,method,start,stop,parent in [
        ('inventory-out',svc,'inventory-availability','Search',55,end-100,'api'),
        ('inventory','inventory-availability','inventory-availability','Search',60,end-105,'inventory-out')]:
        rpc='vantra.travel.inventory.v1.AvailabilityService/Search'
        a(label,owner,rpc,3 if label.endswith('out') else 2,start,stop,parent,
          attributes={'rpc.system':'grpc','rpc.service':'vantra.travel.inventory.v1.AvailabilityService','rpc.method':method,'rpc.grpc.status_code':0 if status==200 else 14},error='UNAVAILABLE' if status==503 else None)
    for i in range(3):
        fail=bad and i==2
        a('supplier'+str(i),'inventory-availability','GET',3,70+i*2,1574 if fail else 200+i*40,'inventory',
            attributes={'http.request.method':'GET','server.address':f'supplier-{i+1}.example',
                'url.full':f'https://supplier-{i+1}.example/availability', **({} if fail else {'http.response.status_code':200}),
                'request.deadline_ms':1500},error='TimeoutError' if fail else None,
            diagnostic='Optional availability supplier exceeded its deadline; returning eligible results' if fail and partial else None)
    if not bad or partial:
        rpc='vantra.travel.pricing.v1.QuoteService/GetQuote'
        a('quote-out',svc,rpc,3,end-90,end-45,'api',dependencies=['search.inventory-out'],attributes={'rpc.system':'grpc','rpc.service':'vantra.travel.pricing.v1.QuoteService','rpc.method':'GetQuote','rpc.grpc.status_code':0})
        a('quote','rate-quote',rpc,2,end-85,end-50,'quote-out',attributes={'rpc.system':'grpc','rpc.service':'vantra.travel.pricing.v1.QuoteService','rpc.method':'GetQuote','rpc.grpc.status_code':0})
        a('calculate','rate-quote','travel.quote.calculate',1,end-80,end-55,'quote',attributes={'currency':'USD'})
        a('merge',svc,'travel.search.merge_results',1,end-40,end-20,'api',dependencies=['search.quote-out'])
        a('rank',svc,'travel.search.rank_options',1,end-18,end-5,'api',dependencies=['search.merge'])
    r.response={'partial_results':bad and partial,'eligible_options':12 if bad and partial else 0 if bad else 18,'criteria':'two_adults_one_room'}
    if bad:g.causal_node='search.supplier2'
    g.outcome='partial_results' if bad and partial else 'search_unavailable' if bad else 'completed'
    g.event(18350 if bad else 17100,'state','Some options unavailable' if bad and partial else 'Search unavailable' if bad else 'Results ready')
    return 23000


def build(scenario, variant, platform, seed, index, namespace, config=None):
    cfg=dict(config or {})
    g=Execution(scenario,variant,platform,seed,index,namespace,cfg)
    bad=variant=='affected'; warm=cfg.get('cache')=='warm'
    # Individual streams keep client identity, think-time and query sampling independent.
    g.event(1000,'click','Sign in' if scenario=='signin-outage' else 'Review accounts' if g.domain=='banking' else 'Find a destination')
    if scenario=='signin-outage':
        r=signin(g); g.event(2400,'state','Signing in'); g.event(g.get(r.client).end+20,'state','Sign-in unavailable' if bad else 'Signed in')
        g.outcome='abandoned_after_signin_error' if bad else 'signed_in'; end=13000
    elif scenario=='travel-provider':
        g.event(12000,'click','Search stays');g.event(14500,'navigate','hotels');g.event(16400,'state','Searching');end=travel(g,warm)
    else:
        overview(g,warm=warm);g.event(2900,'state','Accounts ready');end=30000
        if scenario in ('healthy','slow-history','connectivity'):
            g.event(11000,'click','Transaction history');g.event(12500,'navigate','account')
            g.event(16400,'state','Loading history')
            if scenario=='connectivity' and bad and cfg.get('connectivity_phase','pre_send')=='pre_send':
                r,_=request(g,'history','history',16500,450,None,'offline');r.expected_absence=True
                g.causal_node='history.client'; g.event(16000,'connectivity','offline');g.event(17000,'state','Offline — try again');g.outcome='offline_abandoned'
            else:
                r=history(g,slow=scenario=='slow-history' and bad,transport='reset' if scenario=='connectivity' and bad else None)
                if r.transport:g.causal_node=r.client;g.event(16000,'connectivity','connection_lost')
                g.event(g.get(r.client).end+50,'state','Connection interrupted' if r.transport else 'History ready')
                if r.transport:g.outcome='connection_interrupted'
        elif scenario=='contract-regression':
            g.event(11000,'click','Transaction details');g.event(12500,'navigate','detail')
            r,a=simple(g,'detail','detail',16500)
            sql(a,'query','transaction-history','api',55,110,'transaction_details')
            a('serialize','transaction-history','transactions.detail.serialize',1,115,140,'api',attributes={'response.schema':'transaction-detail-v2','response.merchant.display_name':'null'})
            r.response={'merchant':{'displayName':None},'schema':'transaction-detail-v2'}
            g.add('decode','frontend','transactions.decode_detail',1,16725,16735,trace='decode',links=['detail.client'],dependencies=['detail.client'],error='ContractViolation' if bad else None,attributes={'response.schema':'transaction-detail-v2','response.merchant.display_name':'null'})
            if bad:
                g.causal_node='decode';fatal=cfg.get('client_failure')=='fatal' and platform!='web'
                g.event(16740,'fatal' if fatal else 'exception','contract',dependency='detail.client')
                if not fatal:g.event(16800,'state','Transaction details unavailable')
                g.outcome='process_terminated' if fatal else 'handled_contract_error';end=16740 if fatal else 23000
            else:g.event(16800,'state','Transaction ready — merchant unavailable');end=23000
        elif scenario in ('transfer-response-loss','worker-backlog'):
            g.event(11000,'click','Transfer');g.event(12500,'navigate','transfer')
            g.event(22000,'click','Review transfer');g.event(23000,'navigate','transfer_review')
            g.event(25000,'reference','Operation reference created');g.event(25500,'click','Submit transfer')
            end=transfer(g)
        elif scenario=='frontend-regression':
            g.event(11000,'click','Transfer');g.event(12500,'navigate','transfer')
            if bad and platform == 'web' and cfg.get('web_widget'):
                duration=round(stream(seed,index,'widget').uniform(300,600))
                g.add('widget','frontend','support.render_widget',1,15000,15000+duration,trace='widget',attributes={'script.origin':'https://support.example','interaction.processing_ms':duration})
                g.event(15000,'blocked_click','Open support',duration=duration)
                g.event(15700,'state','Support ready')
                g.causal_node='widget';g.outcome='slow_interaction'
            elif bad:
                g.add('render','frontend','transfers.build_view_state',1,15000,15015,trace='render',error='ViewStateError')
                g.causal_node='render';g.event(15020,'exception','view');g.event(15100,'state','Transfer form unavailable');g.outcome='failed_before_submission'
            else:g.event(15100,'state','Transfer form ready')
            end=22000
    operations={'signin':'auth.sign_in','overview':'accounts.load_overview','history':'transactions.load_history',
        'detail':'transactions.load_detail','transfer':'transfers.submit','status':'transfers.reconcile',
        'autocomplete':'travel.autocomplete','search':'travel.search_availability'}
    for r in g.requests:
        n=g.get(r.client)
        g.add(r.key+'.operation','frontend',operations[r.route],1,max(0,n.start-50),n.end+10,
            trace=r.key+'.operation',links=[r.client],error=n.error,
            attributes={'app.operation':operations[r.route]})
    if cfg.get('provider', 'none') != 'none' and platform == 'web':
        # Consent precedes analytics; support only loads after the explicit open action.
        provider=cfg['provider']; at=8000
        g.event(at-200,'consent' if provider=='analytics' else 'click','Analytics consent granted' if provider=='analytics' else 'Open support')
        g.event(at,'provider',provider,duration=90 if provider=='analytics' else 180)
    if not cfg.get('reference', True):
        # Vary reading/decision time in quiet intervals, never scale deadlines or SQL durations.
        rng=stream(seed,index,'human-pauses')
        for threshold in (9000, 500):
            delay=round(rng.triangular(300,2500,900))
            for n in g.spans:
                if n.start>=threshold:n.start+=delay
                if n.end>=threshold:n.end+=delay
            for e in g.events:
                if e.at>=threshold:e.at+=delay
            for key in ('completed_at','reference_created_at'):
                if g.effects.get(key,0)>=threshold:g.effects[key]+=delay
            end+=delay
    # Network profile adds only modeled transport wait, never invented DNS/TLS observations.
    extra={'wifi':0,'cellular':120,'constrained':480}[cfg.get('network','wifi')]
    if extra:
        # Transport failures already have a fixed offline detection/deadline interval.
        # Slower connectivity cannot extend the caller's configured timeout.
        for request_node in sorted((g.get(r.client) for r in g.requests if not r.transport),key=lambda n:n.start,reverse=True):
            threshold=request_node.start+1
            for n in g.spans:
                if n.start>=threshold:n.start+=extra
                if n.end>=threshold:n.end+=extra
            for e in g.events:
                if e.at>=threshold:e.at+=extra
            if g.effects.get('completed_at',0)>=threshold:g.effects['completed_at']+=extra
            if g.effects.get('reference_created_at',0)>=threshold:g.effects['reference_created_at']+=extra
            end+=extra
    g.final_ms=end
    g.events.sort(key=lambda e:e.at)
    return g.validate()
