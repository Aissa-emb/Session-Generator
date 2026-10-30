import base64
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from server import worker
from server.service import Service
from server.store import Store
from server.verification import traces
from xcorgen import spec


class NoDNS(HTTPServer):
    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = 'localhost', self.server_address[1]


class DeliveryTests(unittest.TestCase):
    def test_burst_sends_only_to_local_receivers_and_records_status(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            def log_message(self, *_):
                pass
        http = NoDNS(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        self.addCleanup(http.server_close)
        with tempfile.TemporaryDirectory() as directory:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
            import datetime, ipaddress, ssl
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256())
            cert_path, key_path = os.path.join(directory, 'cert.pem'), os.path.join(directory, 'key.pem')
            with open(cert_path, 'wb') as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            with open(key_path, 'wb') as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            http.socket = context.wrap_socket(http.socket, server_side=True)
            thread.start()
            self.addCleanup(http.shutdown)
            store = Store(directory)
            self.addCleanup(store.engine.dispose)
            svc = Service(store, launch_jobs=False)
            s = spec.default_spec()
            s['traffic'] = {'mode': 'burst', 'count': 3, 'window': {'last_minutes': 5}}
            s['destinations'] = {'mode': 'end_to_end'}
            s['chronosphere'] = {'tenant': 'demo-sandbox', 'token_ref': 'env:LOCAL_RECEIVER_TOKEN'}
            saved = svc.create_spec(s)
            config_path = os.path.join(directory, 'receiver.json')
            with open(config_path, 'w') as f:
                json.dump({'embrace': {'data_url': f'https://127.0.0.1:{http.server_port}'},
                           'otlp': {'endpoint': f'https://127.0.0.1:{http.server_port}', 'encoding': 'json'}}, f)
            os.chmod(config_path, 0o600)
            with patch.dict(os.environ, {'SG_CONFIG_FILE': config_path, 'LOCAL_RECEIVER_TOKEN': 'loopback-only', 'SSL_CERT_FILE': cert_path}):
                run = svc.start_run(saved['id'], confirm=True)
                worker.execute(store, run['id'])
            self.assertEqual(store.get('runs', run['id'])['status'], 'completed', store.events(run['id']))
            self.assertTrue(any(p.startswith('/v2/') for p, _ in requests))
            self.assertTrue(any(p == '/v1/traces' for p, _ in requests))
            self.assertTrue(all(r['http_status'] == 200 for r in store.list('receipts')))

    def test_verification_counts_missing_ids_and_checks_root_parent_chain(self):
        s = spec.default_spec()
        s['chronosphere'] = {'tenant': 'demo-sandbox'}
        sessions = [{'target': 'vantra-android', 'line_of_business': 'retail', 'traces': True,
                     'trace_ids': [f'{i:032x}' for i in range(1, 20)]}]
        calls = []
        def transport(url, body, headers, **kwargs):
            payload = json.loads(body)
            calls.append(payload)
            self.assertLessEqual(len(payload['trace_ids']), 10)
            self.assertNotIn('start_time', payload)
            tid = payload['trace_ids'][0]
            return json.dumps({'traces': [{'resourceSpans': [{'resource': {'attributes': [
                {'key': 'service.name', 'value': {'stringValue': 'vantra-android-production'}},
                {'key': 'behavior_dataset_slug', 'value': {'stringValue': 'vantra-end-to-end'}}]},
                'scopeSpans': [{'spans': [{'traceId': tid, 'spanId': '1' * 16, 'name': 'GET'}]}]}]}]}).encode()
        result = traces(s, sessions, 'credential', transport)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['expected'], 19)
        self.assertEqual(result['complete'], 3)
        self.assertEqual(result['groups']['android:retail']['expected'], 19)
        self.assertTrue(any(r['sampling'].get('behavior_dataset_slug') == 'vantra-end-to-end' for r in result['traces']))
