import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import BadStatusLine
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from xcorgen import otlp
from xcorgen.personas import PersonaPool
from xcorgen.runner import Generator, Sender, load_config
from xcorgen.security import SecurityError, TransportError, post, read_token_file, tenant_url, validate_endpoint


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self):
        return load_config(None)

    def test_keychain_lookup_keeps_service_and_account_with_their_flags(self):
        from xcorgen.secrets import resolve
        for reference, expected in (
            ('keychain:Session-Generator/chronosphere', ['-s', 'Session-Generator', '-a', 'chronosphere', '-w']),
            ('keychain:Session-Generator', ['-s', 'Session-Generator', '-w']),
        ):
            with self.subTest(reference=reference), patch('xcorgen.secrets.subprocess.run', return_value=Mock(stdout='fixture-value\n')) as lookup:
                self.assertEqual(resolve(reference), 'fixture-value')
                lookup.assert_called_once_with(['/usr/bin/security', 'find-generic-password', *expected],
                    capture_output=True, text=True, timeout=10, check=True)

    def test_rejects_untrusted_url_forms(self):
        for url in ("file:///etc/passwd", "ftp://example.com", "http://example.com",
                    "https://user:password@example.com", "https://example.com?token=secret",
                    "https://example.com/#fragment", "https://example.com\n", "https://example.com:bad",
                    "https://example.com\\@other.example", "https://example.com?"):
            with self.subTest(url=url), self.assertRaises(SecurityError):
                validate_endpoint(url)

    def test_loopback_collector_requires_no_credentials(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            url = f"http://{host}:4318"
            self.assertEqual(validate_endpoint(url), url)
            with self.assertRaises(SecurityError):
                validate_endpoint(url, {"API-Token": "sensitive"})

    def test_tenant_cannot_override_destination(self):
        for tenant in ("bad.example/?", "other.example/#", "user@other.example", "a/b", "a\n", "a.b"):
            with self.subTest(tenant=tenant), self.assertRaises(SecurityError):
                tenant_url(tenant)
        self.assertEqual(tenant_url("demo-sandbox"), "https://demo-sandbox.chronosphere.io")

    def test_headers_reject_injection_and_host_overrides(self):
        for headers in ({"API-Token": "value\r\nInjected: yes"}, {"Bad\nName": "value"},
                        {"Host": "other.example"}, {"Content-Length": "999"}):
            with self.subTest(headers=headers), self.assertRaises(SecurityError):
                validate_endpoint("https://example.com", headers)

    def test_redirects_never_contact_second_endpoint(self):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(int(self.path[1:]))
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leaked")
                self.end_headers()

            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        class Server(ThreadingHTTPServer):
            def server_bind(self):
                # Avoid macOS reverse DNS at bind time.
                self.socket.bind(self.server_address)
                self.server_address = self.socket.getsockname()
                self.server_port = self.server_address[1]

        with Server(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for code in (301, 302, 303, 307, 308):
                    with self.assertRaises(TransportError):
                        post(f"http://127.0.0.1:{server.server_port}/{code}", b"private payload", {})
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(hits, [f"/{code}" for code in (301, 302, 303, 307, 308)])

    def test_http_errors_do_not_read_or_echo_server_body(self):
        payload = Mock()
        error = HTTPError("https://example.com", 401, "server-supplied-secret", {}, payload)
        opener = Mock()
        opener.open.side_effect = error
        with patch("xcorgen.security.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(TransportError) as raised:
                post("https://example.com", b"body", {"API-Token": "private-value"})
        payload.read.assert_not_called()
        self.assertEqual(str(raised.exception), "HTTP 401 from telemetry endpoint")

    def test_network_exception_does_not_echo_url_or_credentials(self):
        for error in (URLError("private-value"), BadStatusLine("private-value")):
            opener = Mock()
            opener.open.side_effect = error
            with patch("xcorgen.security.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(TransportError) as raised:
                    post("https://example.com", b"body", {})
            self.assertNotIn("private-value", str(raised.exception))

    def test_response_read_is_bounded(self):
        response = Mock()
        response.read.return_value = b"x" * 33
        opener = Mock()
        opener.open.return_value = contextlib.nullcontext(response)
        with patch("xcorgen.security.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(TransportError):
                post("https://example.com", b"", {}, max_response_bytes=32)
        response.read.assert_called_once_with(33)

    def test_private_token_file_and_environment_precedence(self):
        token_path = self.root / "credential"
        token_path.write_text("file-value\n")
        token_path.chmod(0o600)
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"chronosphere": {"api_token_file": "credential"}}))
        self.assertEqual(load_config(str(config_path))["chronosphere"]["api_token"], "file-value")
        with patch.dict(os.environ, {"CHRONOSPHERE_API_TOKEN": "environment-value"}):
            self.assertEqual(load_config(str(config_path))["chronosphere"]["api_token"], "environment-value")
        if os.name == "posix":
            token_path.chmod(0o644)
            with self.assertRaises(SecurityError):
                read_token_file(token_path)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_inline_credentials_require_private_config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"chronosphere": {"api_token": "local-value"}}))
        path.chmod(0o644)
        with self.assertRaises(SecurityError):
            load_config(str(path))
        path.chmod(0o600)
        self.assertEqual(load_config(str(path))["chronosphere"]["api_token"], "local-value")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW required")
    def test_token_symlink_is_refused(self):
        path = self.root / "credential"
        path.write_text("local-value")
        path.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(SecurityError):
            read_token_file(link)

    def test_app_id_is_validated_before_state_is_opened(self):
        cfg = self.config()
        cfg["embrace"]["apps"]["default"] = "../../outside"
        with patch("xcorgen.runner.PersonaPool") as pool, self.assertRaises(SecurityError):
            Generator(["banking", "travel"], cfg)
        pool.assert_not_called()

    def test_logs_do_not_echo_backend_rejection(self):
        cfg = self.config()
        cfg["chronosphere"].update(tenant="example", api_token="private-value")
        sender = Sender(cfg, verbose=True)
        self.addCleanup(sender.close)
        response = otlp._len(1, otlp._uint(1, 1) + otlp._str(2, "private-value"))
        output = io.StringIO()
        with patch.object(sender, "_post", return_value=response), patch.object(otlp, "encode_traces", return_value=b""), contextlib.redirect_stderr(output):
            sender._send({"target": "otlp", "kind": "traces", "payload": {}})
        self.assertEqual(sender.stats["errors"], 1)
        self.assertNotIn("private-value", output.getvalue() + str(sender.last_errors))

    def test_throttling_is_retried_but_authentication_errors_are_not(self):
        sender = Sender(self.config())
        self.addCleanup(sender.close)
        with patch("xcorgen.runner.post", side_effect=[TransportError("limited", 429), b""]) as send, patch("xcorgen.runner.time.sleep"):
            sender._post("https://example.com", b"", {})
            self.assertEqual(send.call_count, 2)
        with patch("xcorgen.runner.post", side_effect=TransportError("denied", 401)) as send, self.assertRaises(TransportError):
            sender._post("https://example.com", b"", {})
        self.assertEqual(send.call_count, 1)

    def test_malformed_protobuf_is_rejected_quickly(self):
        for body in (b"\x80" * 4096, b"\x0a\xff", b"\x0a\x05\x08\x01", b"\x00", b"\x0a\x02\x08\x80", b"\x08\x01"):
            with self.subTest(body=body[:10]), self.assertRaises(ValueError):
                otlp.partial_success(body)
        self.assertEqual(otlp.partial_success(b"\x0a\x02\x08\x03"), (3, ""))
        self.assertEqual(otlp.partial_success(b"\x10\x01\x0a\x02\x08\x03"), (3, ""))

    def test_persona_save_does_not_follow_predictable_temp_symlink(self):
        pool = PersonaPool("test", str(self.root))
        victim = self.root / "untouched"
        victim.write_text("original")
        Path(pool.path + ".tmp").symlink_to(victim)
        pool.save()
        self.assertEqual(victim.read_text(), "original")
        if os.name == "posix":
            self.assertEqual(Path(pool.path).stat().st_mode & 0o777, 0o600)

    def test_cli_rejects_nonfinite_or_negative_rates_before_sending(self):
        for args in (("--rate", "nan"), ("--rate", "inf"), ("--rate", "-1"), ("--sessions", "0"), ("--duration", "-1")):
            result = subprocess.run([sys.executable, "generate.py", *args], capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
