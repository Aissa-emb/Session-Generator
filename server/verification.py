"""Verify known IDs, including missing IDs in every denominator."""
import base64
import json
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from xcorgen import limits
from xcorgen.security import post, tenant_url, TransportError
from . import secrets


def attrs(values):
    return {a['key']: next(iter(a['value'].values())) if isinstance(a['value'], dict) else a['value'] for a in values}


def hexid(value):
    if not value:
        return ''
    return value if re.fullmatch(r'[a-f0-9]{16}|[a-f0-9]{32}', value) else base64.b64decode(value).hex()


def unpack(trace):
    spans = []
    for rs in trace.get('resource_spans', trace.get('resourceSpans', [])):
        common = attrs(rs.get('resource', {}).get('attributes', []))
        for ss in rs.get('scope_spans', rs.get('scopeSpans', [])):
            for sp in ss.get('spans', []):
                parent=hexid(sp.get('parent_span_id',sp.get('parentSpanId')))
                spans.append({'trace': hexid(sp.get('trace_id', sp.get('traceId'))), 'id': hexid(sp.get('span_id', sp.get('spanId'))),
                              'parent': parent if parent.strip('0') else '',
                              'links': {(hexid(l.get('trace_id',l.get('traceId'))),hexid(l.get('span_id',l.get('spanId')))) for l in sp.get('links',[])},
                              'attrs': dict(common, **attrs(sp.get('attributes', [])))})
    return spans


def traces(spec, sessions, token, transport=post):
    targets = {t['id']: t for t in spec['targets']}
    expected = {tid: s for s in sessions for tid in s.get('trace_ids', []) if s.get('traces')}
    ids = sorted(expected)
    observed = {}
    raw_traces = []
    not_found_queries = 0
    query_errors = {}
    tenant = (spec.get('chronosphere') or {}).get('tenant')
    if not token or not tenant:
        return {'state': 'needs_action', 'expected': len(ids), 'message': 'Configure the Cortex XCOR token to query every known trace ID.'}
    def query(trace_ids):
        nonlocal not_found_queries
        body = {'query_type': 'TRACE_IDS', 'trace_ids': trace_ids}
        try:
            for attempt in range(4):
                try:
                    response = json.loads(transport(tenant_url(tenant) + '/api/v1/data/traces', json.dumps(body).encode(),
                        {'API-Token': token, 'Content-Type': 'application/json'}, max_response_bytes=32 * 1024 * 1024))
                    break
                except TransportError as error:
                    if error.status not in (429,502,503,504):raise
                    if attempt==3:
                        query_errors.update({tid:error.status for tid in trace_ids})
                        return []
                    time.sleep(2**attempt)
            return response.get('traces', [])
        except TransportError as error:
            if error.status != 404:
                raise
            not_found_queries += 1
            # Some Data API deployments reject a batch if any ID is absent.
            # Query each ID before concluding that every trace is missing.
            return [tr for tid in trace_ids for tr in query([tid])] if len(trace_ids) > 1 else []
    for offset in range(0, len(ids), 8):
        for tr in query(ids[offset:offset + 8]):
            raw_traces.append(tr)
            records = unpack(tr)
            if records:
                observed[records[0]['trace']] = records
    rows, groups = [], defaultdict(lambda: {'expected': 0, 'retained': 0, 'complete': 0})
    for tid, session in expected.items():
        spans = observed.get(tid, [])
        target = session.get('_target') or targets[session['target']]
        app = limits.forwarded_service_name(target['app_name'], target['platform'], target['environment'])
        roots = [s for s in spans if not s['parent'] or set(s['parent']) == {'0'}]
        all_ids = {s['id'] for s in spans}
        missing = [s['parent'] for s in spans if s['parent'] and set(s['parent']) != {'0'} and s['parent'] not in all_ids]
        complete = len(roots) == 1 and roots[0]['attrs'].get('service.name') == app and not missing
        causal=session.get('causal')
        async_trace=bool(causal and tid not in {r['trace_id'] for r in causal['requests']})
        if causal:
            nodes={n['key']:n for n in causal['graph']}
            required=[n for n in nodes.values() if n['trace_id']==tid and n['service']!='frontend']
            actual={s['id']:s for s in spans}
            payload_complete=all(n['span_id'] in actual and actual[n['span_id']]['parent']==
                (nodes[n['parent']]['span_id'] if n['parent'] else '') for n in required)
            payload_complete=payload_complete and all({(nodes[k]['trace_id'],nodes[k]['span_id']) for k in n['links']} <= actual[n['span_id']]['links'] for n in required)
            complete=complete and payload_complete
        if async_trace:
            expected_ids={n['span_id'] for n in causal['graph'] if n['trace_id']==tid and n['service']!='frontend'}
            complete=bool(expected_ids) and payload_complete and not missing
        sampling = {k: v for s in spans for k, v in s['attrs'].items() if k in ('behavior_dataset_slug', 'tail_sampling_rate') or 'sampling' in k or 'dataset' in k}
        row = {'trace_id': tid, 'retained': bool(spans), 'complete': complete, 'missing_parents': missing,
               'query_error':query_errors.get(tid),
               'kind': 'asynchronous' if async_trace else 'request',
               'sampling': sampling, 'url': f'https://{tenant}.chronosphere.io/traces/trace-detail/by-id/{tid}' if spans else None}
        rows.append(row)
        g = groups[target['platform'] + ':' + str(session.get('line_of_business'))]
        g['expected'] += 1
        g['retained'] += bool(spans)
        g['complete'] += complete
    from xcorgen.causal.validation import verify_returned_trace
    causal_rows=[{'session_id':s['session_id'],'platform':s['platform'],
        'navigation':verify_returned_trace(s['causal'],raw_traces)} for s in sessions if s.get('causal')]
    return {'state': ('verified' if all(r['complete'] for r in rows) else 'incomplete') if ids else 'no_eligible_traces',
            'expected': len(ids), 'retained': sum(r['retained'] for r in rows),
            'complete': sum(r['complete'] for r in rows), 'groups': dict(groups), 'traces': rows,
            'not_found_queries':not_found_queries, 'query_errors':query_errors, 'causal_sessions':causal_rows}


def counted_sessions(sessions):
    """Apple foreground sessions are separate SDK sessions; Android/web use user sessions."""
    for row in sessions:
        if row.get("platform") == "ios" and row.get("sdk_sessions"):
            for part in row["sdk_sessions"]:
                yield dict(row, start_ms=part["start_ms"], session_id=part["id"])
        else:
            yield row


def manifest_sessions(path):
    """Keep every known session, including CLI live rows and failed delivery attempts.

    A terminal record supersedes its planned record, including removal of expired
    trace IDs. Transport failures must never shrink the verification denominator.
    """
    records = {}
    path = Path(path)
    journal = path.with_name('run.sqlite3')
    if journal.is_symlink():
        raise ValueError('Run journal must not be a symbolic link.')
    if journal.exists():
        # Interrupted bounded runs can have known IDs only in the durable plan.
        # Opening read-only never resumes or acknowledges their deliveries.
        db = sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            for record, in db.execute('SELECT record FROM sessions ORDER BY idx'):
                row = json.loads(record)
                records[(row.get('run_id'), row['index'])] = dict(row, stage='planned')
        finally:
            db.close()
    for source in (path.with_name(path.name + '.1'), path):
        if not source.exists():
            continue
        with source.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith('\n'):
                        break  # A worker may be midway through appending its last record.
                    raise
                if all(key in row for key in ('target', 'session_id', 'index')):
                    records[(row.get('run_id'), row['index'])] = row
    return list(records.values())


async def verify(store, run):
    spec = store.spec_version(run['spec_id'], run['spec_version'])['spec']
    path = Path(run['manifest_path'])
    sessions = manifest_sessions(path)
    versions={}
    for row in sessions:
        number=row.get('spec_version',run['spec_version'])
        if number not in versions:versions[number]=store.spec_version(run['spec_id'],number)['spec']
        row['_target']=next(t for t in versions[number]['targets'] if t['id']==row['target'])
    mature = [s for s in sessions if s['end_ms'] <= time.time() * 1000 - limits.FORWARDING_REFRESH_MS]
    token = secrets.for_spec(store, spec, 'chronosphere')
    import asyncio
    result = {'run_id': run['id'], 'history_scope': run.get('history_scope', 'complete run'), 'checked_at': time.time(), 'settling_sessions': len(sessions) - len(mature),
              'chronosphere': await asyncio.to_thread(traces, spec, mature, token) if any(s.get('traces') for s in mature) else {'state': 'sessions_only'},
              'backfill_notes': limits.BACKFILL_NOTES, 'trace_caveat': limits.TRACE_CAVEAT}
    calls = []
    for target in spec['targets']:
        args = {'app_id': target['embrace_app_id'], 'time_window': 'last_24_hours'}
        calls += [('get_app_details', args), ('get_session_distribution', dict(args, group_by='app_version')),
                  ('list_network_domains', args), ('list_root_spans', args)]
        if target['platform'] == 'web':
            calls += [('get_web_vital_distribution', dict(args, name=name, dimension=dimension)) for name in ('TTFB','FCP','LCP','CLS','INP','TBD') for dimension in ('device_type','surface_label')]
            calls += [('get_web_vital_distribution', dict(args, name='LCP', dimension='browser_name')),
                      ('get_web_vital_timeseries', dict(args, name='LCP')), ('list_exceptions', args)]
    embrace_token = secrets.get(store, 'embrace_api')
    if embrace_token:
        from .embrace import query
        result['embrace'] = {'state': 'observed', 'results': await query(embrace_token, calls)}
    else:
        result['embrace'] = {'state': 'needs_action', 'message': 'Add an Embrace API token or run these read-only MCP calls and attach the results.',
                             'calls': [{'tool': n, 'arguments': a} for n, a in calls]}
    result['planned_hourly_counts'] = dict(__import__('collections').Counter(str(int(s['start_ms'] // 3600000)) for s in counted_sessions(sessions) if s.get('stage') == 'sent'))
    result['expected_vitals'] = run.get('report', {}).get('web_vitals', [])
    return store.put('verifications', result)


def compare_observations(store, run, observations):
    """Compare operator/API observations without misrepresenting transport acceptance."""
    if observations.get('session_unit') != 'user_sessions' or not observations.get('source'):
        raise ValueError('Identify the evidence source and use user_sessions, not session parts.')
    path = Path(run['manifest_path'])
    sessions = [r for r in manifest_sessions(path) if r.get('stage') == 'sent']
    from collections import Counter
    planned = Counter((s['target'], int(s['start_ms'] // 3600000)) for s in counted_sessions(sessions))
    hourly = []
    for item in observations.get('hourly_counts', []):
        key = (item['target'], int(item['hour_start_ms'] // 3600000))
        expected = planned.get(key, 0)
        actual = item['count']
        if not isinstance(actual, int) or actual < 0:
            raise ValueError('Observed session counts must be nonnegative integers.')
        hourly.append(dict(item, expected=expected, delta=actual - expected, within_tolerance=abs(actual - expected) <= max(1, expected * .02)))
    vital_rows = []
    fields = ('line_of_business', 'browser_family', 'device_type', 'page_group', 'metric')
    expected_vitals = {tuple(v[k] for k in fields): v for v in run.get('report', {}).get('web_vitals', [])}
    for item in observations.get('vitals', []):
        expected = expected_vitals.get(tuple(item.get(k) for k in fields))
        if expected:
            actual, planned_p75 = float(item['p75']), expected['p75']
            if not __import__('math').isfinite(actual) or actual < 0:
                raise ValueError('Observed p75 must be finite and nonnegative.')
            vital_rows.append(dict(item, expected_p75=planned_p75, within_tolerance=abs(actual - planned_p75) <= planned_p75 * .1))
    return store.put('verifications', {'run_id': run['id'], 'checked_at': time.time(), 'source': str(observations['source'])[:2000],
        'embrace': {'state': 'compared', 'hourly_counts': hourly, 'vitals': vital_rows,
                    'exception_groups': observations.get('exception_groups', []),
                    'history_scope': run.get('history_scope', 'complete run'),
                    'coverage_complete': not run.get('history_truncated') and set(planned) <= {(r['target'], int(r['hour_start_ms'] // 3600000)) for r in hourly}},
        'backfill_notes': limits.BACKFILL_NOTES,
        'note': 'Use rolling ranges and a cohort containing these known sessions. Other traffic in the app can affect unfiltered comparisons.'})
