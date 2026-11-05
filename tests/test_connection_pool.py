from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import threading
import unittest
from xcorgen.security import ConnectionPool, SecurityError, TransportError, post


class ConnectionPoolTests(unittest.TestCase):
    def test_keepalive_reuses_connections_without_redirects_or_oversized_responses(self):
        clients, paths = [], []
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                clients.append(self.client_address)
                paths.append(self.path)
                status=302 if self.path=='/redirect' else 200
                body=b'x'*50 if self.path=='/large' else b'{}'
                self.send_response(status)
                self.send_header('Location','/forbidden')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*_): pass
        class Server(ThreadingHTTPServer):
            def server_bind(self):
                self.socket.bind(self.server_address)
                self.server_address=self.socket.getsockname()
                self.server_name,self.server_port='localhost',self.server_address[1]
        server=Server(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        pool=ConnectionPool()
        try:
            endpoint=f'http://127.0.0.1:{server.server_port}'
            for _ in range(2): self.assertEqual(post(endpoint+'/ok',b'',{},connection_pool=pool),b'{}')
            self.assertEqual(clients[0],clients[1])
            with self.assertRaisesRegex(TransportError,'redirect refused'):
                post(endpoint+'/redirect',b'',{},connection_pool=pool)
            self.assertNotIn('/forbidden',paths)
            with self.assertRaisesRegex(TransportError,'size limit'):
                post(endpoint+'/large',b'',{},max_response_bytes=10,connection_pool=pool)
            with self.assertRaises(SecurityError):
                post(endpoint+'/ok',b'',{'Authorization':'secret'},connection_pool=pool)
            self.assertEqual(pool.context.verify_mode,ssl.CERT_REQUIRED)
            self.assertTrue(pool.context.check_hostname)
        finally:
            pool.close()
            self.assertEqual(len(pool.connections),0)
            server.shutdown()
            server.server_close()
            thread.join()
