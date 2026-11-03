"""Evidence-backed readiness and an exportable dashboard setup recipe."""
import json
import time
from pathlib import Path
from xcorgen import limits, milestones, web_sites
from xcorgen.causal.registry import SERVICES

ROOT = Path(__file__).resolve().parents[1]


def setup_kit(target):
    t = target.get('target', target)
    if target.get('kind') == 'chronosphere':
        return {'target': target['id'], 'tenant': target['tenant'], 'items': [
            {'id': 'retention', 'title': 'Dataset and Allow behavior', 'services':list(SERVICES), 'instructions': 'Include the canonical Vantra services and every forwarded app service in the scoped demo dataset. Preserve existing services and unrelated assignments; inspect the exact configuration diff before applying it.'}]}
    service = limits.forwarded_service_name(t.get('app_name', 'Vantra'), t['platform'], t.get('environment', 'production'))
    services = [service, 'shop-experience-api', 'banking-experience-api', 'travel-experience-api', *SERVICES]
    items = [
        {'id': 'nsf', 'title': 'Network span forwarding', 'instructions': f"Include app {t['embrace_app_id']} with the Cortex XCOR destination and tenant; enable trace links for the organization.", 'domains': list(t['api_domains'].values())},
        {'id': 'retention', 'title': 'Dataset and retention', 'instructions': 'Add these services to vantra-end-to-end with a managed Allow behavior.', 'services': services},
        {'id': 'probe', 'title': 'Forwarding probe', 'instructions': 'Send a small confirmed end-to-end burst, wait at least five minutes, verify every known trace ID with the app span as root, and inspect Forwarded plus View trace in Cortex XCOR on the network request. HTTP 200 alone is insufficient.'},
        {'id': 'flows', 'title': 'User Flows', 'definitions': milestones.definitions(t['embrace_app_id'])},
        {'id': 'filters', 'title': 'Saved session filters', 'definitions': json.loads((ROOT / 'docs/session-filters.json').read_text())['filters']},
        {'id': 'verification', 'title': 'Verification access', 'instructions': 'Save an Embrace service account API token in Settings, or use the documented Embrace MCP tools and attach results.'},
    ]
    if t['platform'] == 'web':
        web = t['web']
        items += [
            {'id': 'origins', 'title': 'Allowed origins', 'policy': web.get('origin_header', {'policy': 'none'}), 'origins': sorted({v['origin'] for v in web['estate'].values()}),
             'instructions': 'If Origin enforcement is enabled, allow every Origin header this target will send.'},
            {'id': 'pages', 'title': 'Page Groups', 'rules': [{'name': n, 'pattern': r} for n, r in web_sites.PAGE_GROUP_RULES]},
            {'id': 'symbols', 'title': 'Source maps', 'instructions': 'When symbolication is enabled, upload each release map with the symbol token before emitting exceptions. Upload requires explicit confirmation.'},
        ]
    return {'target': target.get('id', t['id']), 'app_id': t['embrace_app_id'], 'forwarded_service': service,
            'items': items, 'trace_caveat': limits.TRACE_CAVEAT, 'backfill_notes': limits.BACKFILL_NOTES}


def check(store, target):
    kit = setup_kit(target)
    saved = {r['item']: r for r in store.list('dashboard_setup') if r['target_id'] == target['id']}
    items = []
    for item in kit['items']:
        evidence = saved.get(item['id'], {})
        items.append(dict(item, state=evidence.get('state', 'unknown'), evidence=evidence.get('evidence'), checked_at=evidence.get('checked_at')))
    return {'target_id': target['id'], 'checked_at': time.time(), 'ready': all(i['state'] == 'ok' for i in items if i['id'] in ('nsf', 'retention', 'probe', 'origins')),
            'items': items, 'trace_caveat': limits.TRACE_CAVEAT}


def markdown(kit):
    lines = ['# Dashboard setup', '', f"Target: {kit['target']}", '']
    for item in kit['items']:
        lines.extend([f"## {item['title']}", '', item.get('instructions', ''), '', '```json', json.dumps(item, indent=2), '```', ''])
    lines.extend([kit.get('trace_caveat', ''), '', *kit.get('backfill_notes', [])])
    return '\n'.join(lines)
