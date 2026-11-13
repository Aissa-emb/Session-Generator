"""The cloud runner's small, environment-only input contract.

This module is deliberately side-effect free except for reading explicitly named
secret files. It never reads the interactive vault or starts a worker. Secrets
are returned separately so the controller can bind an immutable credential
snapshot to the generated spec before calling the existing Service.
"""
import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
from urllib.parse import urlsplit

from xcorgen import catalog, spec as specs
from xcorgen.security import SecurityError, read_token_file, validate_headers


PLATFORMS = ('web', 'android', 'ios')
TARGETS = {'web': specs.WEB_TARGET, 'android': specs.ANDROID_TARGET, 'ios': specs.IOS_TARGET}
RATES = {'crashes': 8, 'exceptions': 20, 'freezes': 15, 'network': 15,
         'payments': 12, 'lcp': 25, 'inp': 15, 'cls': 15, 'fcp': 10, 'ttfb': 10}
SEVERITY = {'lcp': 3800, 'inp': 450, 'cls': .3, 'fcp': 3200, 'ttfb': 2200}
LEGACY_TRANSPORT_VARIABLES = ('SG_CONFIG_FILE', 'CHRONOSPHERE_TENANT',
                              'CHRONOSPHERE_API_TOKEN', 'CHRONOSPHERE_API_TOKEN_FILE',
                              'OTLP_ENDPOINT', 'OTLP_HEADERS')


class CloudConfigurationError(ValueError):
    """Safe operator-facing errors: no submitted values or credentials."""


@dataclass(frozen=True)
class CloudConfig:
    enabled: bool
    spec: dict | None = None
    fingerprint: str | None = None
    summary: dict = field(default_factory=dict)
    # Never dump dataclasses.asdict(config); only summary/spec are public.
    credentials: dict = field(default_factory=dict, repr=False)


def production_baseline(targets, tenant=None, rate=10, symbolication=False):
    """Mirror workflow.ts scenario(defaults), with live, indefinite traffic.

    The parity test executes the actual UI builder with the same inputs. Faults
    are gated by the engine catalog exactly as in the UI; for example a native
    iOS crash is not a JVM crash, and web vitals never go to native apps.
    """
    s = specs.default_spec()
    s['name'] = 'Production baseline · All journeys'
    s['targets'] = copy.deepcopy(targets)
    for target in s['targets']:
        if target['platform'] == 'web':
            target['web']['symbolication'] = symbolication
    s['population'].update(platform_mix={t['id']: 1 for t in targets}, model='varied',
                           returning_share=.45, native_cold_start_share=.35,
                           native_background_share=.12)
    s['traffic'] = {'mode': 'live', 'rate_per_minute': rate, 'duration_minutes': 0}
    s['chronosphere'] = {'tenant': tenant} if tenant else None
    s['destinations'] = {'mode': 'end_to_end' if tenant else 'sessions_only', 'backend_logs': False}
    s['web_performance'] = []
    # Presence explicitly prevents borrowing credentials saved by UI users.
    s['saved_credentials'] = {'chronosphere': None, 'symbol_upload': None}

    def add(key, kind, platform, details=None):
        if platform not in catalog.CLIENT_FAULTS[kind]['supported']:
            return
        fault = {'id': key + '-' + platform, 'type': kind, 'platform': platform,
                 'rate': RATES[key] / 100, 'quota': 'exact' if key == 'network' else 'probability',
                 'filters': {}, 'details': details or {}}
        if key == 'network':
            fault['steer_journey'] = False
        s['client_faults'].append(fault)

    for target in targets:
        p = target['platform']
        add('crashes', 'ios_crash' if p == 'ios' else 'jvm_crash', p)
        add('exceptions', 'js_exception' if p == 'web' else 'handled_exception', p,
            {'family': 'property_undefined', 'handling': 'unhandled_error'} if p == 'web' else {})
        add('freezes', 'main_thread_blockage', p, {'min_ms': 6000, 'max_ms': 10000})
        add('network', 'network_failure', p, {'kinds': ['timeout', 'reset']})
        add('lcp', 'lcp_regression', p, {'p75_ms': SEVERITY['lcp'], 'cause': 'hero_image'})
        add('inp', 'interaction_latency', p, {'p75_ms': SEVERITY['inp']})
        add('cls', 'layout_instability', p, {'p75': SEVERITY['cls']})
        add('fcp', 'poor_web_vitals', p, {'targets': {'fcp': SEVERITY['fcp']}})
        add('ttfb', 'slow_server_response', p, {'p75_ms': SEVERITY['ttfb']})
    for variant in ('payments-db-pool', 'rails-outage'):
        s['backend_faults'].append({'id': variant, 'variant': variant,
                                    'type': catalog.BACKEND_VARIANTS[variant]['type'],
                                    'rate': RATES['payments'] / 100, 'quota': 'probability', 'filters': {}})
    return s


def _token(env, name):
    key = 'SG_' + name.upper() + '_TOKEN'
    value, path = env.get(key, ''), env.get(key + '_FILE', '')
    if value and path:
        raise CloudConfigurationError(f'Set either {key} or {key}_FILE, not both.')
    if path:
        try:
            value = read_token_file(path)
        except SecurityError:
            raise CloudConfigurationError(f'Cannot read {key}_FILE; use an owned, regular token file with mode 600 or 400.') from None
    if not value:
        return None
    if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() for c in value):
        raise CloudConfigurationError(f'{key} must contain one nonempty token, at most 8192 characters.')
    try:
        validate_headers({'Authorization': value})
    except SecurityError:
        raise CloudConfigurationError(f'{key} contains invalid token characters.') from None
    return value


def _tenant(value):
    value = value.strip().lower()
    if not value:
        return None
    error = 'SG_CHRONOSPHERE_TENANT must be a tenant name or its HTTPS Chronosphere address (optional :443).'
    if any(c.isspace() for c in value) or '?' in value or '#' in value:
        raise CloudConfigurationError(error)
    if '://' in value or '.chronosphere.io' in value:
        try:
            url = urlsplit(value if '://' in value else 'https://' + value)
            host = url.hostname or ''
            # Shared tenant Secrets use host:443 for other clients. Normalize
            # that address here without changing the shared Secret's value.
            if (url.scheme != 'https' or url.username is not None or url.password is not None or
                    url.netloc not in (host, host + ':443') or
                    url.path not in ('', '/') or not host.endswith('.chronosphere.io')):
                raise ValueError()
            value = host.removesuffix('.chronosphere.io')
        except ValueError:
            raise CloudConfigurationError(error) from None
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise CloudConfigurationError(error)
    return value


def load_config(environ=None):
    """Read explicit cloud inputs; disabled mode never accesses credentials.

    Supplying IDs alone does not opt in. Turning cloud mode off remains possible
    even after the previous IDs, secret files or credentials have been removed.
    """
    env = os.environ if environ is None else environ
    enabled = env.get('SG_CLOUD_RUNNER', '').strip()
    if enabled in ('', '0'):
        return CloudConfig(enabled=False, summary={'enabled': False})
    if enabled != '1':
        raise CloudConfigurationError('SG_CLOUD_RUNNER must be 1 to enable cloud generation, or 0 to disable it.')
    conflicts = [key for key in LEGACY_TRANSPORT_VARIABLES if env.get(key)]
    if conflicts:
        raise CloudConfigurationError('Remove legacy transport settings ' + ', '.join(conflicts) +
                                      ' for cloud generation; use the documented SG_ app IDs, tenant and token inputs.')
    targets, ids = [], set()
    for platform in PLATFORMS:
        prefix = 'SG_' + platform.upper()
        app_id = env.get(prefix + '_APP_ID', '').strip()
        if not app_id:
            continue
        if not re.fullmatch(r'[A-Za-z0-9]{5}', app_id) or app_id.lower() == 'ios00':
            raise CloudConfigurationError(prefix + '_APP_ID must be the five-character ID of an existing app; placeholders are not accepted.')
        if app_id.lower() in ids:
            raise CloudConfigurationError('Each enabled platform must use a different app ID.')
        ids.add(app_id.lower())
        target = copy.deepcopy(TARGETS[platform])
        target['embrace_app_id'] = app_id
        target['app_name'] = env.get(prefix + '_APP_NAME', target['app_name']).strip()
        target['region'] = env.get(prefix + '_INGEST_REGION', 'us').strip().lower()
        if not target['app_name'] or len(target['app_name']) > 64 or any(ord(c) < 32 for c in target['app_name']):
            raise CloudConfigurationError(prefix + '_APP_NAME must contain 1–64 printable characters.')
        if target['region'] not in ('us', 'eu'):
            raise CloudConfigurationError(prefix + '_INGEST_REGION must be us or eu.')
        targets.append(target)
    if not targets:
        raise CloudConfigurationError('Set at least one of SG_WEB_APP_ID, SG_ANDROID_APP_ID or SG_IOS_APP_ID.')
    try:
        rate = float(env.get('SG_RATE_PER_MINUTE', '10'))
        if not math.isfinite(rate) or not 0 < rate <= 600:
            raise ValueError()
    except (ValueError, TypeError):
        raise CloudConfigurationError('SG_RATE_PER_MINUTE must be greater than 0 and at most 600, total across enabled platforms.') from None
    tenant, token = _tenant(env.get('SG_CHRONOSPHERE_TENANT', '')), _token(env, 'chronosphere')
    if bool(tenant) != bool(token):
        raise CloudConfigurationError('Supply SG_CHRONOSPHERE_TENANT and SG_CHRONOSPHERE_TOKEN (or _FILE) together, or omit both for sessions only.')
    web = any(t['platform'] == 'web' for t in targets)
    symbol_token = _token(env, 'symbol_upload') if web else None
    spec = production_baseline(targets, tenant, rate, bool(symbol_token))
    errors = specs.errors(specs.validate(spec))
    if errors:
        # Validator messages may quote supplied values. Report only paths/codes.
        locations = ', '.join(sorted({e['path'] + ' (' + e['code'] + ')' for e in errors}))
        raise CloudConfigurationError('Cloud scenario failed validation at ' + locations + '.')
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    summary = {'enabled': True, 'apps': [{k: t[k] for k in ('platform', 'embrace_app_id', 'app_name', 'region')}
                                        for t in targets],
               'destination': spec['destinations']['mode'], 'tenant': tenant,
               'rate_per_minute': rate, 'rate_basis': 'total_across_enabled_platforms',
               'duration_minutes': 0, 'profile': 'Production baseline',
               'source_maps': 'enabled' if symbol_token else 'Original-source mapping not enabled' if web else 'not_applicable'}
    return CloudConfig(True, spec, fingerprint, summary,
                       {'chronosphere': token, 'symbol_upload': symbol_token})
