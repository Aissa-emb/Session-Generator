"""Credential-safe configuration and HTTP transport shared by the CLI tools."""

import ipaddress
import http.client
import os
import re
import stat
import ssl
import threading
from collections import OrderedDict
import urllib.error
import urllib.parse
import urllib.request


class SecurityError(ValueError):
    """Safe to display: messages never contain configuration values or response bodies."""


class TransportError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def require_private_file(file):
    info = os.fstat(file.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise SecurityError("Credentials must be stored in a regular file.")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SecurityError("Credential file must be owned by the current user with mode 600 or 400.")


def read_token_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as f:
            require_private_file(f)
            value = f.read(16385)
    except (OSError, UnicodeError):
        raise SecurityError("Cannot read the credential file; check its path, ownership and permissions.") from None
    if len(value) > 16384 or not value.strip() or any(c.isspace() for c in value.strip()):
        raise SecurityError("Credential file must contain one nonempty token.")
    return value.strip()


def tenant_url(tenant):
    if not isinstance(tenant, str) or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", tenant):
        raise SecurityError("Chronosphere tenant must be a DNS label, not a URL.")
    return f"https://{tenant}.chronosphere.io"


def validate_headers(headers):
    if not isinstance(headers, dict):
        raise SecurityError("HTTP headers must be a mapping.")
    forbidden = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}
    for key, value in headers.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or key.lower() in forbidden or not isinstance(value, str)
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise SecurityError("Invalid or reserved HTTP header.")


def validate_endpoint(url, headers=None):
    """Only TLS, or credential-free HTTP to a loopback collector; never URL credentials."""
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise SecurityError("Invalid telemetry endpoint.")
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise SecurityError("Invalid telemetry endpoint.") from None
    if (not host or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "?" in url or "#" in url):
        raise SecurityError("Endpoint must not contain credentials, a query, or a fragment.")
    if parts.scheme not in ("https", "http"):
        raise SecurityError("Telemetry endpoints must use HTTPS.")
    validate_headers(headers or {})
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        safe_headers = {"content-type", "content-encoding", "accept", "user-agent"}
        if not loopback or any(k.lower() not in safe_headers for k in (headers or {})):
            raise SecurityError("HTTP is allowed only for a loopback collector without credentials or identifying headers.")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise TransportError("HTTP redirect refused; configure the final HTTPS endpoint.", status=code)


class ResponseBytes(bytes):
    """Response body with status metadata; compatible with existing byte decoders."""
    def __new__(cls, body, status):
        value = super().__new__(cls, body)
        value.status = status
        return value


class ConnectionPool:
    """Sender-owned keep-alive connections; one per thread/host, no automatic replay.

    urllib remains the proxy-aware path when a configured proxy applies. Connections
    are closed after the sender has joined its threads, including failed requests.
    """
    def __init__(self):
        self.local = threading.local()
        self.lock = threading.Lock()
        self.connections = set()
        self.context = ssl.create_default_context()

    def close(self):
        with self.lock:
            for connection in self.connections:
                connection.close()
            self.connections.clear()

    def post(self, url, body, headers, timeout, limit):
        parts = urllib.parse.urlsplit(url)
        cache = getattr(self.local, "connections", None)
        if cache is None:
            cache = self.local.connections = OrderedDict()
        key = (parts.scheme, parts.hostname, parts.port, timeout)
        connection = cache.pop(key, None)
        if connection is None:
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            connection = cls(parts.hostname, parts.port, timeout=timeout,
                             **({"context": self.context} if parts.scheme == "https" else {}))
            with self.lock:
                self.connections.add(connection)
        cache[key] = connection
        if len(cache) > 4:
            _, old = cache.popitem(last=False)
            old.close()
            with self.lock:
                self.connections.discard(old)
        try:
            connection.request("POST", parts.path or "/", body=body, headers=headers)
            response = connection.getresponse()
            # Never follow redirects or copy their Location/remote body into an exception.
            if 300 <= response.status < 400:
                raise TransportError("HTTP redirect refused; configure the final HTTPS endpoint.", status=response.status)
            if response.status >= 400:
                raise TransportError(f"HTTP {response.status} from telemetry endpoint", status=response.status)
            result = response.read(limit + 1)
            if len(result) > limit:
                raise TransportError("HTTP response exceeds the configured size limit.")
            return ResponseBytes(result, response.status)
        except (TransportError, OSError, ValueError, http.client.HTTPException) as error:
            cache.pop(key, None)
            connection.close()
            with self.lock:
                self.connections.discard(connection)
            if isinstance(error, TransportError):
                raise
            raise TransportError("Telemetry request failed; check TLS, network connectivity and endpoint settings.") from None


def post(url, body, headers, *, timeout=30, max_response_bytes=1024 * 1024, connection_pool=None):
    validate_endpoint(url, headers)
    parts = urllib.parse.urlsplit(url)
    if connection_pool is not None and (parts.scheme == "http" or not urllib.request.getproxies().get(parts.scheme)
                                         or urllib.request.proxy_bypass(parts.hostname)):
        return connection_pool.post(url, body, headers, timeout, max_response_bytes)
    handlers = [_NoRedirect()]
    if urllib.parse.urlsplit(url).scheme == "http":
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        # URL schemes and redirects are restricted above.
        with opener.open(request, timeout=timeout) as response:  # nosec B310
            result = response.read(max_response_bytes + 1)
            if len(result) > max_response_bytes:
                raise TransportError("HTTP response exceeds the configured size limit.")
            return ResponseBytes(result, response.status)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise TransportError(f"HTTP {status} from telemetry endpoint", status=status) from None
    except (OSError, ValueError, http.client.HTTPException):
        # Network exceptions and server error bodies may echo credentials or URLs.
        raise TransportError("Telemetry request failed; check TLS, network connectivity and endpoint settings.") from None
