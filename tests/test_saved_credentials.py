import unittest
from types import SimpleNamespace
from unittest.mock import patch
import generate
from xcorgen import spec


class SavedCredentialBoundaryTests(unittest.TestCase):
    def test_cli_cannot_silently_replace_saved_destination_keys_with_its_defaults(self):
        s = spec.default_spec()
        s['saved_credentials'] = {'chronosphere': 'a' * 32, 'symbol_upload': None}
        with patch('generate.load_config', side_effect=AssertionError('No config may be used')) as config:
            with self.assertRaisesRegex(SystemExit, 'Studio server'):
                generate.run_spec(SimpleNamespace(dry_run=None), s)
            config.assert_not_called()

    def test_contract_accepts_only_opaque_ids_and_explicit_missing_keys(self):
        s = spec.default_spec()
        s['saved_credentials'] = {'chronosphere': 'a' * 32, 'symbol_upload': None}
        self.assertEqual(spec.errors(spec.validate(s)), [])
        for value in ('actual-private-token', {}, {'chronosphere': 'a' * 32},
                      {'chronosphere': 'not-an-id', 'symbol_upload': None}):
            s['saved_credentials'] = value
            self.assertTrue(spec.errors(spec.validate(s)))
