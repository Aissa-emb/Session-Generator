"""Named local destinations. SQLite holds metadata; credentials stay in the vault."""
import re
import time
from . import secrets
from .locks import serialized
from .store import identifier

KINDS = ('chronosphere', 'symbol_upload')


def public(store, row):
    credentials = {}
    for kind in KINDS:
        id = row['credentials'].get(kind)
        try:
            credential = store.get('settings', 'credential:' + id) if id else {}
        except KeyError:
            credential = {}
        credentials[kind] = {'id': id, 'configured': bool(credential), 'last_four': credential.get('last_four')}
    return {k: row[k] for k in ('id', 'name', 'apps', 'tenant', 'destination', 'version')} | {'credentials': credentials, 'updated': row.get('updated', 0)}


def listing(store):
    rows = [public(store, r) for r in store.list('settings') if r['id'].startswith('destination:')]
    try:
        selected = store.get('settings', 'destination-selection')['value']
    except KeyError:
        selected = None
    return {'destinations': rows, 'selected_id': selected}


def validated(value):
    allowed = {'name', 'apps', 'tenant', 'destination', 'credentials', 'use_legacy', 'source_credentials', 'expected_version'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('Unsupported saved destination fields.')
    name = value.get('name')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError('Name your saved destination using 1–80 characters.')
    apps = value.get('apps')
    if not isinstance(apps, list) or not 1 <= len(apps) <= 3:
        raise ValueError('Choose one to three applications.')
    platforms, ids = set(), set()
    clean = []
    for app in apps:
        if not isinstance(app, dict) or set(app) != {'platform', 'appId', 'name', 'region'}:
            raise ValueError('Invalid saved application fields.')
        if app['platform'] not in ('android', 'web', 'ios') or app['platform'] in platforms:
            raise ValueError('Choose each platform once.')
        if not isinstance(app['appId'], str) or not re.fullmatch(r'[a-zA-Z0-9]{5}', app['appId'].strip()):
            raise ValueError('Each application needs a five-character app ID.')
        if app['appId'].strip() in ids:
            raise ValueError('Each platform needs its own app ID.')
        if app['region'] not in ('us', 'eu') or not isinstance(app['name'], str) or not 1 <= len(app['name'].strip()) <= 80:
            raise ValueError('Enter an app name and choose US or EU.')
        platforms.add(app['platform']); ids.add(app['appId'].strip())
        clean.append(dict(app, appId=app['appId'].strip(), name=app['name'].strip()))
    tenant = value.get('tenant', '')
    mode = value.get('destination')
    if mode not in ('end_to_end', 'sessions_only') or not isinstance(tenant, str):
        raise ValueError('Choose a destination mode and tenant.')
    tenant = tenant.strip()
    if (tenant or mode == 'end_to_end') and not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', tenant):
        raise ValueError('Enter the Chronosphere tenant name without its URL.')
    values = value.get('credentials', {})
    if not isinstance(values, dict) or set(values) - set(KINDS):
        raise ValueError('Unsupported credential type.')
    for token in values.values():
        if token is not None and (not isinstance(token, str) or not 1 <= len(token.strip()) <= 8192):
            raise ValueError('Enter a valid credential or remove it explicitly.')
        if token is not None:
            try:
                secrets.validate_headers({'Authorization': token.strip()})
            except secrets.SecurityError:
                raise ValueError('Credentials cannot contain header control characters.') from None
    if 'use_legacy' in value and not isinstance(value['use_legacy'], bool):
        raise ValueError('Invalid existing-credential choice.')
    sources = value.get('source_credentials', {})
    if not isinstance(sources, dict) or set(sources) - set(KINDS) or any(v is not None and (not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{32}', v)) for v in sources.values()):
        raise ValueError('Invalid saved credential selection.')
    return {'name': name.strip(), 'apps': clean, 'tenant': tenant, 'destination': mode}


@serialized('run-launch')
def save(svc, value, id=None):
    data = validated(value)
    prior = svc.store.get('settings', id) if id else None
    if id and not id.startswith('destination:'):
        raise ValueError('Invalid saved destination.')
    if prior and value.get('expected_version') != prior['version']:
        raise ValueError('This destination changed in another window. Reload before saving.')
    rows = listing(svc.store)['destinations']
    if not prior and len(rows) >= 50:
        raise ValueError('Use Manage saved values to remove an unused set before saving another (limit 50).')
    if any(r['name'].casefold() == data['name'].casefold() and r['id'] != id for r in rows):
        raise ValueError('Choose a different saved destination name.')
    id = id or 'destination:' + identifier()
    refs = dict(prior['credentials']) if prior else {k: None for k in KINDS}
    new = []
    try:
        for kind in KINDS:
            values = value.get('credentials', {})
            if kind in values and values[kind] is None:
                refs[kind] = None
                continue
            token = values.get(kind)
            sources = value.get('source_credentials', {})
            if token is None and kind in sources:
                if sources[kind] is None:
                    refs[kind] = None
                    continue
                if sources[kind] == refs[kind]:
                    continue
                token = secrets.for_spec(svc.store, {'saved_credentials': sources}, kind)
            if token is None and kind not in sources and value.get('use_legacy'):
                token = secrets.get(svc.store, kind)
                if token is None:
                    refs[kind] = None
                    continue
            if token is None:
                continue
            key = identifier()
            ref = secrets.write('destination-' + key, token.strip())
            record = {'id': 'credential:' + key, 'kind': kind, 'destination_id': id,
                      'ref': ref, 'last_four': secrets.suffix(token.strip())}
            new.append(record)
            svc.store.put('settings', record)
            refs[kind] = key
        row = svc.store.put('settings', dict(data, id=id, credentials=refs,
                         version=prior['version'] + 1 if prior else 1, updated=time.time()))
    except Exception:
        for record in new:
            secrets.remove(record['ref'])
            svc.store.delete('settings', record['id'])
        raise
    svc.store.put('settings', {'id': 'destination-selection', 'value': id})
    return public(svc.store, row)


@serialized('run-launch')
def select(svc, id):
    if not isinstance(id, str) or not id.startswith('destination:'):
        raise ValueError('Invalid saved destination.')
    row = svc.store.get('settings', id)
    svc.store.put('settings', {'id': 'destination-selection', 'value': id})
    return public(svc.store, row)


@serialized('run-launch')
def remove(svc, id):
    if not id.startswith('destination:'):
        raise ValueError('Invalid saved destination.')
    svc.store.get('settings', id)
    credentials = [r for r in svc.store.list('settings') if r.get('destination_id') == id and r['id'].startswith('credential:')]
    ids = {r['id'].split(':', 1)[1] for r in credentials}
    for run in svc.store.list('runs'):
        if run['status'] in ('queued', 'running', 'paused'):
            spec = svc.store.spec_version(run['spec_id'], run['spec_version'])['spec']
            if ids.intersection((spec.get('saved_credentials') or {}).values()):
                raise ValueError('Stop runs using this destination before removing its credentials.')
    for credential in credentials:
        secrets.remove(credential['ref'])
        svc.store.delete('settings', credential['id'])
    svc.store.delete('settings', id)
    if listing(svc.store)['selected_id'] == id:
        svc.store.delete('settings', 'destination-selection')
    return {'removed': True}
