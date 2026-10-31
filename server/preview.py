"""Isolated deterministic preview process: no network sender is constructed."""
import copy
import json
import random
import sys
import tempfile
import time
from xcorgen import limits, linter, spec as specs
from xcorgen.spec_runner import SpecRun
from xcorgen.url_rollup import preview as rollup
from xcorgen.web_observation import attrs


class Collector:
    dry = True
    stats = {'errors': 0}
    class Pool:
        def shutdown(self, wait=True):
            pass
    pool = Pool()
    def __init__(self):
        self.deliveries = []
    def submit(self, d):
        self.deliveries.append(d)


def preview(spec):
    s = copy.deepcopy(specs.normalized(spec))
    requested_count = s['traffic'].get('count')
    preview_now = time.time() * 1000
    if s['traffic']['mode'] == 'live':
        windows = [specs.incident_window(i) for i in s.get('incidents', [])]
        start = min([preview_now] + [w['start_ms'] for w in windows])
        end = max([preview_now + 8 * 60000] + [w['start_ms'] + w['onset_ms'] + w['peak_ms'] + w['recovery_ms'] for w in windows])
        if end - start > 24 * 3600000:
            raise ValueError('Preview incidents within one 24-hour window at a time.')
        preview_now = end
        s['traffic']['window'] = {'start': specs.iso(start), 'end': specs.iso(end)}
    s['traffic'] = dict(s['traffic'], mode='burst', count=min(requested_count or 60, 120))
    s['traffic'].pop('rate_per_minute', None)
    s['traffic'].pop('duration_minutes', None)
    s['traffic'].setdefault('window', {'last_minutes': 8})
    random.seed(s.get('seed') or 7331)
    cfg = {'embrace': {'apps': {}}, 'chronosphere': {}, 'otlp': {}}
    with tempfile.TemporaryDirectory() as state:
        run = SpecRun(s, cfg, persist=False, state_dir=state, now_ms=preview_now)
        collector = Collector()
        _, allocated = run.run_batch(collector, quiet=True)
    timelines, waterfall, urls, vitals, exception = {}, [], [], [], None
    for d in collector.deliveries:
        p = d['payload']
        if d.get('fmt') == 'envelope':
            rows = p.get('data', {}).get('spans', [])
            platform=p.get('resource',{}).get('sdk_platform','android')
            if rows and platform not in timelines:
                timelines[platform] = [{'name': sp['name'], 'start': sp.get('start_time_unix_nano'), 'end': sp.get('end_time_unix_nano'), 'attributes': sp.get('attributes', [])} for sp in rows]
            continue
        for rs in p.get('resourceSpans', []):
            resource = attrs(rs.get('resource', {}))
            for ss in rs['scopeSpans']:
                for sp in ss['spans']:
                    a = attrs(sp)
                    if a.get('url.full'):
                        urls.append(a['url.full'])
                    row = {'name': sp['name'], 'start': sp['startTimeUnixNano'], 'end': sp['endTimeUnixNano'],
                           'id': sp['spanId'], 'parent': sp.get('parentSpanId'), 'attributes': a, 'service': resource.get('service.name')}
                    if d['target'] == 'embrace' and len(timelines.get('web', [])) < 100:
                        timelines.setdefault('web', []).append(row)
                    if d['target'] == 'otlp' and (not waterfall or sp['traceId'] == waterfall[0]['trace_id']):
                        waterfall.append(dict(row, trace_id=sp['traceId']))
        for rs in p.get('resourceLogs', []):
            for ss in rs['scopeLogs']:
                for log in ss['logRecords']:
                    a = attrs(log)
                    if a.get('emb.type') == 'sys.exception' and exception is None:
                        exception = a
                    if a.get('emb.type') == 'ux.web_vital':
                        vitals.append(a)
    collector.pool.shutdown(wait=True)
    return {'preview_clock': specs.iso(preview_now), 'sample_count': s['traffic']['count'], 'requested_count': requested_count,
            'expected_counts': allocated, 'report': run.emitted.summary(), 'timelines': timelines,
            'trace_waterfall': waterfall, 'vitals': vitals[:1000], 'exception': exception,
            'url_rollup': rollup(urls), 'linter': linter.lint_payloads(collector.deliveries),
            'limits': limits.capabilities(), 'backfill_notes': limits.BACKFILL_NOTES, 'trace_caveat': limits.TRACE_CAVEAT}


if __name__ == '__main__':
    print(json.dumps(preview(json.load(sys.stdin))))
