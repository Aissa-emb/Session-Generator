import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from server.app import create_app
from server.service import Service
from server.store import Store
from server import secrets, worker
from xcorgen import spec
from xcorgen.security import SecurityError


class SavedDestinationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'SG_SECRET_DIR': self.tmp.name + '/vault'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.platform = patch('server.secrets.sys.platform', 'linux')
        self.platform.start(); self.addCleanup(self.platform.stop)
        self.store = Store(self.tmp.name + '/state')
        self.addCleanup(self.store.close)
        self.svc = Service(self.store, launch_jobs=False)
        self.client = TestClient(create_app(self.svc))
        self.addCleanup(self.client.close)
        self.body = {'name': 'Production', 'apps': [
            {'platform': p, 'appId': id, 'name': 'Vantra ' + p, 'region': 'us'}
            for p, id in [('android', 'and01'), ('ios', 'ios01'), ('web', 'web01')]],
            'tenant': 'tenant-one', 'destination': 'end_to_end',
            'credentials': {'chronosphere': 'local-private-chron-1234', 'symbol_upload': 'local-private-symbol-5678'}}

    def create(self, body=None):
        response = self.client.post('/api/destinations', json=body or self.body)
        self.assertEqual(response.status_code, 201, response.text)
        for token in self.body['credentials'].values():
            self.assertNotIn(token, response.text)
        return response.json()

    def bound_spec(self, row):
        s = spec.default_spec()
        s['chronosphere'] = {'tenant': row['tenant']}
        s['destinations']['mode'] = 'end_to_end'
        s['saved_credentials'] = {k: v['id'] for k, v in row['credentials'].items()}
        return s

    def test_survives_new_store_and_four_weeks_without_disclosing_keys(self):
        row = self.create()
        self.assertEqual(row['credentials']['chronosphere']['last_four'], '1234')
        with patch('time.time', return_value=time.time() + 28 * 86400):
            reopened = Store(self.tmp.name + '/state')
            self.addCleanup(reopened.close)
            with TestClient(create_app(Service(reopened, launch_jobs=False))) as client:
                response = client.get('/api/destinations')
                restored = response.json()['destinations'][0]
                self.assertEqual(restored['apps'], self.body['apps'])
                self.assertEqual(response.json()['selected_id'], row['id'])
                for token in self.body['credentials'].values():
                    self.assertNotIn(token, response.text)
                    self.assertNotIn(token, json.dumps(reopened.list('settings')))
                self.assertEqual(secrets.for_spec(reopened, self.bound_spec(row), 'chronosphere'), self.body['credentials']['chronosphere'])
        for file in Path(self.tmp.name + '/vault').glob('*'):
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_selection_rotation_and_missing_keys_never_borrow_global_credentials(self):
        a = self.create()
        old = self.bound_spec(a)
        b = self.create(dict(self.body, name='Second', tenant='tenant-two', credentials={'chronosphere': 'second-private-9876'}))
        with patch('server.secrets.get', return_value='wrong-global-key') as legacy:
            self.assertEqual(worker.config(self.store, old, False)['chronosphere']['api_token'], 'local-private-chron-1234')
            self.assertEqual(worker.config(self.store, self.bound_spec(b), False)['chronosphere']['api_token'], 'second-private-9876')
            self.assertIsNone(secrets.for_spec(self.store, self.bound_spec(b), 'symbol_upload'))
            legacy.assert_not_called()
        update = dict(self.body, expected_version=1, credentials={'chronosphere': 'replacement-private-2222'})
        result = self.client.put('/api/destinations/' + a['id'], json=update)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(secrets.for_spec(self.store, old, 'chronosphere'), 'local-private-chron-1234')
        self.assertEqual(secrets.for_spec(self.store, self.bound_spec(result.json()), 'chronosphere'), 'replacement-private-2222')
        self.assertEqual(self.client.put('/api/destinations/' + a['id'], json=update).status_code, 422)
        self.client.post('/api/destinations/' + b['id'] + '/select')
        self.assertEqual(self.client.get('/api/destinations').json()['selected_id'], b['id'])

    def test_failed_save_keeps_previous_destination_and_cleans_new_keys(self):
        a = self.create()
        files = set(Path(self.tmp.name + '/vault').glob('*'))
        original = secrets.write
        def write(name, value):
            if value == 'fail-this-save': raise SecurityError('Vault unavailable')
            return original(name, value)
        with patch('server.destinations.secrets.write', side_effect=write):
            response = self.client.put('/api/destinations/' + a['id'], json=dict(self.body, expected_version=1,
                credentials={'chronosphere': 'new-private-0000', 'symbol_upload': 'fail-this-save'}))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(set(Path(self.tmp.name + '/vault').glob('*')), files)
        self.assertEqual(self.client.get('/api/destinations').json()['destinations'][0], a)

    def test_field_selection_copies_keys_without_changing_past_runs_or_borrowing_defaults(self):
        a = self.create()
        b = self.create(dict(self.body, name='Other', tenant='tenant-two', credentials={'chronosphere': 'other-private-9876'}))
        old = self.bound_spec(a)
        sources = {'chronosphere': b['credentials']['chronosphere']['id'], 'symbol_upload': None}
        update = dict(self.body, credentials={}, source_credentials=sources, use_legacy=True, expected_version=1)
        with patch('server.secrets.get', return_value='wrong-global-key') as legacy:
            response = self.client.put('/api/destinations/' + a['id'], json=update)
            self.assertEqual(response.status_code, 200, response.text)
            legacy.assert_not_called()
        changed = response.json()
        self.assertNotEqual(changed['credentials']['chronosphere']['id'], sources['chronosphere'])
        self.assertIsNone(changed['credentials']['symbol_upload']['id'])
        self.assertEqual(secrets.for_spec(self.store, old, 'chronosphere'), 'local-private-chron-1234')
        self.assertEqual(self.client.delete('/api/destinations/' + b['id']).status_code, 200)
        self.assertEqual(secrets.for_spec(self.store, self.bound_spec(changed), 'chronosphere'), 'other-private-9876')
        # Saving unchanged references does not keep creating vault records.
        sources = {k: v['id'] for k, v in changed['credentials'].items()}
        with patch('server.secrets.write') as write:
            response = self.client.put('/api/destinations/' + a['id'], json=dict(update, expected_version=2, source_credentials=sources))
            self.assertEqual(response.status_code, 200, response.text)
            write.assert_not_called()

    def test_partial_field_selection_can_preserve_an_unmodified_legacy_key(self):
        secrets.save(self.store, 'symbol_upload', 'legacy-symbol-1234')
        row = self.create(dict(self.body, credentials={}, use_legacy=True, source_credentials={'chronosphere': None}))
        self.assertIsNone(secrets.for_spec(self.store, self.bound_spec(row), 'chronosphere'))
        self.assertEqual(secrets.for_spec(self.store, self.bound_spec(row), 'symbol_upload'), 'legacy-symbol-1234')

    def test_fresh_form_does_not_silently_reuse_an_existing_combinations_keys(self):
        row = self.create()
        response = self.client.put('/api/destinations/' + row['id'], json=dict(self.body,
            credentials={}, use_legacy=True, expected_version=1))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['credentials']['symbol_upload']['id'])
        self.assertIsNone(response.json()['credentials']['chronosphere']['id'])
        self.assertEqual(secrets.for_spec(self.store, self.bound_spec(row), 'symbol_upload'), 'local-private-symbol-5678')

    def test_removal_blocks_active_runs_then_deletes_all_rotations(self):
        row = self.create()
        s = self.bound_spec(row)
        saved = self.store.save_spec(s)
        self.store.put('runs', {'id': 'active', 'status': 'paused', 'spec_id': saved['id'], 'spec_version': 1})
        url = '/api/destinations/' + row['id']
        self.assertEqual(self.client.delete(url).status_code, 422)
        self.store.update('runs', 'active', status='completed')
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(self.client.get('/api/destinations').json()['destinations'], [])
        self.assertEqual(list(Path(self.tmp.name + '/vault').glob('*')), [])
        with self.assertRaises(SecurityError): secrets.for_spec(self.store, s, 'chronosphere')

    def test_invalid_input_origin_and_auth_are_rejected_without_echoing_tokens(self):
        token = self.body['credentials']['chronosphere']
        for patch_body in ({'credentials': {'unknown': token}}, {'credentials': {'chronosphere': token + '\r\nextra'}},
                           {'source_credentials': {'chronosphere': token}}, {'apps': []}):
            response = self.client.post('/api/destinations', json=dict(self.body, **patch_body))
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn(token, response.text)
        self.assertEqual(self.client.post('/api/destinations', json=self.body, headers={'Origin': 'https://untrusted.invalid'}).status_code, 403)
        with patch.dict(os.environ, {'SG_REQUIRE_AUTH': '1'}):
            self.assertEqual(self.client.get('/api/destinations').status_code, 401)
            self.assertEqual(self.client.post('/api/destinations', json=self.body).status_code, 401)

    def test_legacy_import_is_explicit_and_short_keys_are_fully_masked(self):
        secrets.save(self.store, 'chronosphere', 'abcd')
        row = self.create(dict(self.body, credentials={}, use_legacy=True))
        self.assertIsNone(row['credentials']['chronosphere']['last_four'])
        self.assertEqual(secrets.for_spec(self.store, self.bound_spec(row), 'chronosphere'), 'abcd')
        self.assertNotIn('abcd', self.client.get('/api/destinations').text)
        second = self.create(dict(self.body, name='No keys', credentials={}))
        self.assertIsNone(secrets.for_spec(self.store, self.bound_spec(second), 'chronosphere'))
        self.assertIsNone(worker.config(self.store, self.bound_spec(second), False)['chronosphere']['api_token'])

    def test_macos_keychain_uses_native_api_and_failure_keeps_metadata(self):
        import keyring
        vault = {}
        with patch('server.secrets.sys.platform', 'darwin'), patch.dict(os.environ, {}, clear=True), \
             patch('keyring.set_password', side_effect=lambda service, account, value: vault.update({account: value})), \
             patch('keyring.get_password', side_effect=lambda service, account: vault.get(account)), \
             patch('keyring.delete_password', side_effect=lambda service, account: vault.pop(account)), \
             patch('server.secrets.resolve') as cli:
            row = self.create()
            self.assertEqual(secrets.for_spec(self.store, self.bound_spec(row), 'chronosphere'), self.body['credentials']['chronosphere'])
            self.assertEqual(len(vault), 2)
            with patch('keyring.delete_password', side_effect=keyring.errors.PasswordDeleteError('private vault diagnostic')):
                response = self.client.delete('/api/destinations/' + row['id'])
                self.assertEqual(response.status_code, 502)
                self.assertNotIn('private vault diagnostic', response.text)
                self.assertEqual(len(self.client.get('/api/destinations').json()['destinations']), 1)
            self.assertEqual(self.client.delete('/api/destinations/' + row['id']).status_code, 200)
            self.assertEqual(vault, {})
            cli.assert_not_called()
