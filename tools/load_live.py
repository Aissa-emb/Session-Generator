#!/usr/bin/env python3
"""Exercise the real worker against a certificate-verified loopback TLS receiver only.

Run with the server venv: python tools/load_live.py --minutes 10 --rate 600
All state, receipts and certificates live in an automatically removed temp directory.
No application credentials, remote endpoints or source-map uploads are used.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import resource
import socketserver
import ssl
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', default='tools/fixtures/load-web.json')
    parser.add_argument('--rate', type=float, default=600)
    parser.add_argument('--minutes', type=float, default=10)
    parser.add_argument('--latency-ms', type=float, default=150)
    args = parser.parse_args()
    if not (0 < args.rate <= 600 and 0 < args.minutes <= 120 and 0 <= args.latency_ms <= 30000):
        parser.error('Use rate 1–600/min, duration up to 120 minutes, and latency 0–30000 ms.')
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from server.store import Store
    from server import worker
    from xcorgen.runner import load_config
    from xcorgen.spec_runner import SpecRun
    received = [0]
    measurements = defaultdict(lambda: defaultdict(list))
    started = time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            time.sleep(args.latency_ms / 1000)
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
            received[0] += 1
        def log_message(self, *_):
            pass

    class Receiver(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
        request_queue_size = 256
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]
        def finish_request(self, request, client_address):
            try:
                with tls.wrap_socket(request, server_side=True) as connection:
                    super().finish_request(connection, client_address)
            except OSError:
                pass

    with tempfile.TemporaryDirectory(prefix='sg-load-') as directory:
        root = Path(directory)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=2)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = root / 'cert.pem', root / 'key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert_path, key_path)
        receiver = Receiver(('127.0.0.1', 0), Handler)
        threading.Thread(target=receiver.serve_forever, daemon=True).start()
        store = Store(root / 'state')
        cfg = load_config(None)
        cfg['embrace']['data_url'] = f'https://127.0.0.1:{receiver.server_port}'
        cfg['otlp'] = {'endpoint': cfg['embrace']['data_url'], 'encoding': 'json', 'headers': {}}
        cfg['chronosphere'] = {'tenant': None, 'api_token': None}  # nosec B105: local receiver only
        spec = json.loads(Path(args.spec).read_text())
        spec['traffic'] = {'mode': 'live', 'rate_per_minute': args.rate, 'duration_minutes': args.minutes}
        spec['chronosphere'] = {'tenant': 'vantra'}
        saved = store.save_spec(spec)
        run_dir = root / 'run'
        run_dir.mkdir()
        run = store.put('runs', dict(id='load', spec_id=saved['id'], spec_version=1, mode='live', destination=spec['destinations']['mode'],
                        status='running', desired_status='running', dry_run=False, started=time.time(), ended=None,
                        manifest_path=str(run_dir / 'manifest.jsonl'), counters={}))
        done = threading.Event()
        def snapshot():
            row = store.get('runs', 'load')
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss / (1024 * 1024 if sys.platform == 'darwin' else 1024)
            return dict(elapsed_s=round(time.monotonic() - started, 1), sessions=row.get('counters', {}).get('sessions', 0),
                        skipped_sessions=row.get('skipped_sessions', 0),
                        received=received[0], rate=row.get('achieved_rate_per_minute'), capacity_limited=row.get('capacity_limited'),
                        reason=row.get('capacity_reason'), max_rss_mb=round(rss_mb, 1),
                        database_mb=round(sum(p.stat().st_size for p in (root / 'state').glob('control.sqlite3*')) / 2**20, 2),
                        queued_payloads=row.get('queued_payloads'), queued_mb=round(row.get('queued_bytes', 0)/2**20, 2))
        def monitor():
            while not done.wait(30):
                print(json.dumps(snapshot()), flush=True)
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        def timed(name, function):
            def invoke(*a, **kw):
                begin = time.perf_counter()
                try:
                    return function(*a, **kw)
                finally:
                    minute = int((time.monotonic() - started) // 60)
                    measurements[minute][name].append((time.perf_counter() - begin) * 1000)
            return invoke
        try:
            with patch.dict(os.environ, SSL_CERT_FILE=str(cert_path)), patch.object(worker, 'config', return_value=cfg), \
                 patch.object(SpecRun, 'session', timed('generate_ms', SpecRun.session)), \
                 patch.object(SpecRun, 'record', timed('record_ms', SpecRun.record)), \
                 patch.object(worker, 'lint_payloads', timed('lint_ms', worker.lint_payloads)):
                worker.execute(store, 'load')
            result = store.get('runs', 'load')
            print(json.dumps(dict(snapshot(), final=True, status=result['status'], failure=result.get('failure'),
                                  achieved_per_minute=result.get('counters', {}).get('sessions', 0) / args.minutes,
                                  missed_arrivals=result.get('missed_arrivals'), counters=result.get('counters'),
                                  timings_by_minute={m: {k: round(sum(v)/len(v), 3) for k,v in row.items()} for m,row in measurements.items()})), flush=True)
            counters = result.get('counters', {})
            if (result['status'] != 'completed' or result.get('skipped_sessions', 0)
                    or counters.get('errors', 0) or counters.get('rejected', 0)):
                raise SystemExit(1)
        finally:
            done.set()
            monitor_thread.join()
            receiver.shutdown()
            receiver.server_close()
            store.close()


if __name__ == '__main__':
    main()
