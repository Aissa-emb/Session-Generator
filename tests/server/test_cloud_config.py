"""Cloud inputs, UI baseline parity and serialized native/web contracts.

All generation is local. The UI parity case uses the already installed build
dependencies, never installs packages or executes TypeScript in production.
"""
from collections import Counter, defaultdict
import copy
import itertools
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server.cloud_config import CloudConfigurationError, LEGACY_TRANSPORT_VARIABLES, load_config
from xcorgen import catalog, spec as specs
from xcorgen.ios_contract import check as check_ios
from xcorgen.linter import lint_payloads
from xcorgen.runner import load_config as transport_config
from xcorgen.spec_runner import SpecRun
from xcorgen.web_contract import check as check_web
from tools.check_output import check_envelope


ROOT = Path(__file__).resolve().parents[2]
APP_IDS = {'web': 'web01', 'android': 'and01', 'ios': 'ios01'}
PLATFORMS = tuple(APP_IDS)
COMBINATIONS = [p for n in (1, 2, 3) for p in itertools.combinations(PLATFORMS, n)]
NOW = 1794000000000


def inputs(platforms=PLATFORMS, **extra):
    return {'SG_CLOUD_RUNNER': '1', **{'SG_' + p.upper() + '_APP_ID': APP_IDS[p] for p in platforms}, **extra}


def attributes(row):
    result = {}
    for attr in row.get('attributes', []):
        value = attr['value']
        result[attr['key']] = next(iter(value.values())) if isinstance(value, dict) else value
    return result


def spans(delivery):
    if delivery.get('fmt') == 'envelope':
        return delivery['payload']['data'].get('spans', [])
    return [s for resource in delivery['payload'].get('resourceSpans', [])
            for scope in resource.get('scopeSpans', []) for s in scope['spans']]


class CloudConfigurationTests(unittest.TestCase):
    def test_disabled_is_safe_with_missing_or_broken_inputs_and_secrets(self):
        for toggle in (None, '', '0'):
            env = inputs(SG_CHRONOSPHERE_TOKEN_FILE='/missing/private-token', SG_RATE_PER_MINUTE='bad',
                         SG_CONFIG_FILE='/missing/legacy-settings')
            env.update({key: 'ignored-legacy-value' for key in LEGACY_TRANSPORT_VARIABLES})
            if toggle is None:
                env.pop('SG_CLOUD_RUNNER')
            else:
                env['SG_CLOUD_RUNNER'] = toggle
            with patch('server.cloud_config.read_token_file', side_effect=AssertionError('Must not read secrets')):
                config = load_config(env)
            self.assertFalse(config.enabled)
            self.assertIsNone(config.spec)
            self.assertEqual(config.credentials, {})

    def test_all_seven_platform_combinations_have_simple_defaults(self):
        for platforms in COMBINATIONS:
            with self.subTest(platforms=platforms):
                config = load_config(inputs(platforms))
                self.assertTrue(config.enabled)
                self.assertEqual([t['platform'] for t in config.spec['targets']], list(platforms))
                self.assertEqual({t['embrace_app_id'] for t in config.spec['targets']}, {APP_IDS[p] for p in platforms})
                self.assertEqual(config.spec['traffic'], {'mode': 'live', 'rate_per_minute': 10, 'duration_minutes': 0})
                self.assertEqual(config.spec['population']['platform_mix'], {'vantra-' + p: 1 for p in platforms})
                self.assertEqual(config.spec['population']['model'], 'varied')
                self.assertEqual(config.spec['destinations'], {'mode': 'sessions_only', 'backend_logs': False})
                self.assertEqual(config.spec['saved_credentials'], {'chronosphere': None, 'symbol_upload': None})
                self.assertEqual(specs.errors(specs.validate(config.spec)), [])
                self.assertEqual(config.fingerprint, load_config(inputs(platforms)).fingerprint)

    def test_invalid_configuration_is_actionable_and_never_echoes_values(self):
        for update, field in [
                ({'SG_CLOUD_RUNNER': 'yes'}, 'SG_CLOUD_RUNNER'),
                ({'SG_CONFIG_FILE': '/private/legacy-settings'}, 'SG_CONFIG_FILE'),
                ({'SG_WEB_APP_ID': 'secret-value-invalid-id'}, 'SG_WEB_APP_ID'),
                ({'SG_IOS_APP_ID': 'ios00'}, 'SG_IOS_APP_ID'),
                ({'SG_IOS_APP_ID': APP_IDS['android']}, 'different app ID'),
                ({'SG_WEB_INGEST_REGION': 'invalid-region-secret'}, 'SG_WEB_INGEST_REGION'),
                ({'SG_WEB_APP_NAME': 'Vantra\nprivate-token'}, 'SG_WEB_APP_NAME'),
                ({'SG_RATE_PER_MINUTE': 'nan'}, 'SG_RATE_PER_MINUTE'),
                ({'SG_RATE_PER_MINUTE': 'inf'}, 'SG_RATE_PER_MINUTE'),
                ({'SG_RATE_PER_MINUTE': '0'}, 'SG_RATE_PER_MINUTE'),
                ({'SG_RATE_PER_MINUTE': '601'}, 'SG_RATE_PER_MINUTE'),
                ({'SG_CHRONOSPHERE_TENANT': 'https://secret:token@acme.chronosphere.io'}, 'SG_CHRONOSPHERE_TENANT'),
                ({'SG_CHRONOSPHERE_TENANT': 'acme'}, 'together'),
                ({'SG_CHRONOSPHERE_TOKEN': 'secret-value-token'}, 'together')]:
            with self.subTest(field=field, update_keys=list(update)):
                with self.assertRaisesRegex(CloudConfigurationError, field) as error:
                    load_config(inputs(**update))
                self.assertNotIn('secret-value', str(error.exception))
                self.assertNotIn('private-token', str(error.exception))
                self.assertNotIn('secret:token', str(error.exception))
        with self.assertRaisesRegex(CloudConfigurationError, 'at least one'):
            load_config({'SG_CLOUD_RUNNER': '1'})

    def test_optional_overrides_and_backend_pair_are_explicit(self):
        config = load_config(inputs(('web',), SG_WEB_APP_NAME='Vantra Storefront', SG_WEB_INGEST_REGION='EU',
                                    SG_RATE_PER_MINUTE='12.5', SG_CHRONOSPHERE_TENANT='https://ACME.chronosphere.io/',
                                    SG_CHRONOSPHERE_TOKEN='fixture-credential'))
        self.assertEqual(config.spec['chronosphere'], {'tenant': 'acme'})
        self.assertEqual(config.spec['destinations']['mode'], 'end_to_end')
        self.assertEqual(config.spec['targets'][0]['app_name'], 'Vantra Storefront')
        self.assertEqual(config.spec['targets'][0]['region'], 'eu')
        self.assertEqual(config.spec['traffic']['rate_per_minute'], 12.5)
        self.assertEqual(config.credentials['chronosphere'], 'fixture-credential')
        self.assertNotIn('fixture-credential', repr(config))
        self.assertNotIn('fixture-credential', json.dumps(config.spec))
        self.assertNotIn('fixture-credential', json.dumps(config.summary))
        changed = load_config(inputs(('web',), SG_WEB_APP_NAME='Vantra Storefront', SG_WEB_INGEST_REGION='EU',
                                     SG_RATE_PER_MINUTE='12.5', SG_CHRONOSPHERE_TENANT='acme',
                                     SG_CHRONOSPHERE_TOKEN='replacement-credential'))
        self.assertEqual(config.fingerprint, changed.fingerprint, 'Public fingerprints must not hash secrets')
        self.assertNotEqual(config.fingerprint, load_config(inputs(('web',))).fingerprint)

    def test_shared_tenant_addresses_normalize_without_changing_the_scenario(self):
        canonical = load_config(inputs(SG_CHRONOSPHERE_TENANT='acme',
                                       SG_CHRONOSPHERE_TOKEN='fixture-credential'))
        for address in ('acme', 'ACME', 'acme.chronosphere.io', 'acme.chronosphere.io/',
                        'acme.chronosphere.io:443', 'acme.chronosphere.io:443/',
                        'https://ACME.chronosphere.io', 'https://acme.chronosphere.io/',
                        'https://acme.chronosphere.io:443', 'https://acme.chronosphere.io:443/'):
            with self.subTest(address=address):
                config = load_config(inputs(SG_CHRONOSPHERE_TENANT=address,
                                            SG_CHRONOSPHERE_TOKEN='fixture-credential'))
                self.assertEqual(config.spec, canonical.spec)
                self.assertEqual(config.fingerprint, canonical.fingerprint)
                self.assertEqual(config.summary['tenant'], 'acme')

    def test_tenant_addresses_reject_ambiguous_or_insecure_inputs(self):
        for address in ('http://acme.chronosphere.io', 'http://acme.chronosphere.io:443',
                        'ftp://acme.chronosphere.io', 'acme.chronosphere.io:80',
                        'https://acme.chronosphere.io:4317', 'acme.chronosphere.io:',
                        'https://secret-value@acme.chronosphere.io',
                        'https://:secret-value@acme.chronosphere.io',
                        'https://@acme.chronosphere.io', 'acme.chronosphere.io/secret-value',
                        'acme.chronosphere.io?token=secret-value', 'acme.chronosphere.io?',
                        'acme.chronosphere.io#secret-value', 'acme.chronosphere.io#',
                        'acme.chronosphere.io.evil.invalid', 'acme.evil.invalid',
                        'https://sub.acme.chronosphere.io', 'https://[bad-host',
                        'acme.chrono\nsphere.io', 'acme.chrono\tsphere.io', 'acme:443'):
            with self.subTest(address=address):
                with self.assertRaisesRegex(CloudConfigurationError, 'SG_CHRONOSPHERE_TENANT') as error:
                    load_config(inputs(SG_CHRONOSPHERE_TENANT=address,
                                       SG_CHRONOSPHERE_TOKEN='fixture-credential'))
                self.assertNotIn(address, str(error.exception))
                self.assertNotIn('secret-value', str(error.exception))
                self.assertNotIn('fixture-credential', str(error.exception))

    def test_legacy_transport_inputs_cannot_override_cloud_destinations(self):
        for key in LEGACY_TRANSPORT_VARIABLES:
            with self.subTest(key=key), self.assertRaisesRegex(CloudConfigurationError, key) as error:
                load_config(inputs(**{key: 'private-legacy-value'}))
            self.assertNotIn('private-legacy-value', str(error.exception))

    def test_source_map_key_only_affects_web_and_never_reads_global_keys(self):
        for platforms in COMBINATIONS:
            config = load_config(inputs(platforms, SG_SYMBOL_UPLOAD_TOKEN='fixture-symbol-key',
                                       SG_EMBRACE_API_TOKEN='not-required-for-sending'))
            for target in config.spec['targets']:
                if target['platform'] == 'web':
                    self.assertTrue(target['web']['symbolication'])
                else:
                    self.assertNotIn('web', target)
            self.assertEqual(bool(config.credentials['symbol_upload']), 'web' in platforms)
            self.assertNotIn('not-required-for-sending', repr(config))
        config = load_config(inputs(('ios',), SG_SYMBOL_UPLOAD_TOKEN_FILE='/missing/irrelevant-file'))
        self.assertEqual(config.summary['source_maps'], 'not_applicable')

    def test_token_files_honor_private_file_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_text('fixture-file-token\n')
            path.chmod(0o600)
            env = inputs(SG_CHRONOSPHERE_TENANT='acme', SG_CHRONOSPHERE_TOKEN_FILE=str(path),
                         SG_SYMBOL_UPLOAD_TOKEN_FILE=str(path))
            config = load_config(env)
            self.assertEqual(config.credentials, {'chronosphere': 'fixture-file-token', 'symbol_upload': 'fixture-file-token'})
            self.assertNotIn(str(path), repr(config))
            with self.assertRaisesRegex(CloudConfigurationError, 'not both'):
                load_config(dict(env, SG_CHRONOSPHERE_TOKEN='fixture-inline-token'))
            path.chmod(0o644)
            with self.assertRaisesRegex(CloudConfigurationError, 'mode 600 or 400'):
                load_config(env)
            path.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaisesRegex(CloudConfigurationError, 'mode 600 or 400'):
                load_config(dict(env, SG_CHRONOSPHERE_TOKEN_FILE=str(link)))
            path.write_text('fixture-file-token\nsecond-line')
            with self.assertRaises(CloudConfigurationError) as error:
                load_config(env)
            self.assertNotIn('fixture-file-token', str(error.exception))

    def test_generated_spec_matches_actual_ui_production_baseline(self):
        web = ROOT / 'apps/web'
        if not shutil.which('node') or not (web / 'node_modules/typescript/lib/typescript.js').exists():
            self.skipTest('Install apps/web build dependencies to run UI/Python parity')
        caps = {'default_spec': specs.default_spec(), 'web_target': specs.WEB_TARGET,
                'ios_target': specs.IOS_TARGET, 'faults': catalog.describe()}
        cases = []
        for platforms in COMBINATIONS:
            for backend, symbolication in ((False, False), (True, True)):
                env = inputs(platforms)
                if backend:
                    env.update(SG_CHRONOSPHERE_TENANT='acme', SG_CHRONOSPHERE_TOKEN='fixture-credential')
                if symbolication:
                    env['SG_SYMBOL_UPLOAD_TOKEN'] = 'fixture-symbol-key'
                config = load_config(env)
                cases.append({'spec': config.spec, 'symbolication': symbolication,
                              'apps': [{'platform': t['platform'], 'appId': t['embrace_app_id'],
                                        'name': t['app_name'], 'region': t['region']} for t in config.spec['targets']]})
        script = """
import fs from 'node:fs';
import ts from './node_modules/typescript/lib/typescript.js';
const source = fs.readFileSync('./src/workflow.ts', 'utf8');
const {outputText} = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}});
const {defaults, scenario} = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));
const {caps, cases} = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(c => scenario(caps, {...structuredClone(defaults), apps: c.apps,
    mode: 'live', duration: 0, destination: c.spec.destinations.mode, tenant: 'acme'}, c.symbolication))));
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=web,
                                input=json.dumps({'caps': caps, 'cases': cases}), text=True,
                                capture_output=True, check=True, timeout=30)
        for case, expected in zip(cases, json.loads(result.stdout), strict=True):
            actual = copy.deepcopy(case['spec'])
            actual.pop('saved_credentials')  # Cloud explicitly disables interactive vault fallback.
            self.assertEqual(actual, expected)


class CloudPlatformPayloadTests(unittest.TestCase):
    def test_all_seven_combinations_emit_native_or_web_payloads_and_linked_traces(self):
        for platforms in COMBINATIONS:
            with self.subTest(platforms=platforms), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
                config = load_config(inputs(platforms, SG_CHRONOSPHERE_TENANT='acme',
                                           SG_CHRONOSPHERE_TOKEN='fixture-token'))
                spec = copy.deepcopy(config.spec)
                # Finite local probe; production config remains live with a total 10/min request.
                spec['traffic'] = {'mode': 'burst', 'count': 60, 'window': {'last_minutes': 5}}
                random.seed(7331)
                run = SpecRun(spec, transport_config(None), persist=False, state_dir=directory,
                              now_ms=NOW, rng=random.Random(7331))
                skeletons, allocations, _ = run.plan_batch()
                counts, failures, link_counts = Counter(), Counter(), Counter()
                for skeleton, faults in zip(skeletons, allocations):
                    target, session, _, deliveries, _ = run.session(skeleton['start_ms'], skeleton, faults)
                    # Inspect exactly the JSON-serializable payload documents, not planner annotations.
                    deliveries = json.loads(json.dumps(deliveries))
                    self.assertEqual(lint_payloads(deliveries), [])
                    p = target['platform']
                    counts[p] += 1
                    clients, backend = {}, defaultdict(list)
                    for delivery in deliveries:
                        if delivery['target'] == 'otlp':
                            for span in spans(delivery):
                                backend[span['traceId']].append(span)
                            continue
                        self.assertEqual(delivery['app_id'], APP_IDS[p])
                        problems = []
                        if p == 'web':
                            self.assertNotEqual(delivery.get('fmt'), 'envelope')
                            check_web(delivery['payload'], problems.append)
                            resource_groups = delivery['payload'].get('resourceSpans', []) + delivery['payload'].get('resourceLogs', [])
                            self.assertTrue(resource_groups)
                            for group in resource_groups:
                                resource = attributes(group['resource'])
                                self.assertIn('user_agent.original', resource)
                                self.assertEqual(resource['sdk_platform'], 'web')
                                self.assertEqual(resource['telemetry.sdk.name'], 'embrace-web-sdk')
                        elif p == 'ios':
                            check_ios(delivery, problems.append)
                            self.assertEqual(delivery['payload']['resource']['sdk_platform'], 'ios')
                        else:
                            check_envelope(delivery, problems.append, {}, {})
                            self.assertEqual(delivery['payload']['version'], '0.1.0')
                            self.assertEqual(delivery['payload']['resource']['os_name'], 'android')
                        self.assertEqual(problems, [])
                        for span in spans(delivery):
                            a = attributes(span)
                            if a.get('emb.type') != 'perf.network_request':
                                continue
                            if a.get('error.type'):
                                failures[p] += 1
                            parent = a.get('emb.w3c_traceparent')
                            if parent:
                                _, trace_id, client_id, flags = parent.split('-')
                                self.assertEqual(flags, '01')
                                # iOS/Android use envelope identity; web traces have SDK-owned IDs
                                # and propagate the distributed network-request trace separately.
                                if p != 'web':
                                    self.assertEqual(trace_id, span['trace_id'])
                                    self.assertEqual(client_id, span['span_id'])
                                clients[trace_id] = client_id
                    for trace_id, rows in backend.items():
                        local_ids = {row['spanId'] for row in rows}
                        roots = [row for row in rows if row.get('parentSpanId') not in local_ids]
                        self.assertEqual(len(roots), 1)
                        if trace_id in clients:
                            self.assertEqual(roots[0]['parentSpanId'], clients[trace_id])
                            link_counts[p] += 1
                        else:
                            # The payment pool fault also emits independent background
                            # worker traces: these must have a genuine root, not a
                            # dangling frontend parent pretending to be linked.
                            self.assertFalse(roots[0].get('parentSpanId'))
                self.assertEqual(set(counts), set(platforms))
                self.assertEqual(sum(counts.values()), 60)
                for p in platforms:
                    self.assertGreater(counts[p], 0)
                    self.assertGreater(failures[p], 0, f'Production baseline must emit network errors for {p}')
                    self.assertGreater(link_counts[p], 0)


if __name__ == '__main__':
    unittest.main()
