"""Backend distributed trace engine.

A profile describes each API endpoint as a tree of Nodes (server handlers,
outgoing HTTP/gRPC hops, database, cache and Kafka calls). build_trace() lays
the tree out in time and turns it into OTLP spans that continue the browser's
traceparent, so the backend root span's parent is the browser network span
that Embrace forwards to Chronosphere.
"""

import random
import zlib

from . import otlp
from .util import hex_id, lat, ns, uuid4

LANG = {
    "java": dict(sdk="1.56.0", runtime=("OpenJDK Runtime Environment", "21.0.8+9-LTS"),
                 distro=("opentelemetry-java-instrumentation", "2.21.0"), ua="Java-http-client/21.0.8"),
    "go": dict(sdk="1.38.0", runtime=("go", "go1.25.3"), ua="Go-http-client/1.1"),
    "nodejs": dict(sdk="2.2.0", runtime=("nodejs", "22.21.0"), ua="undici"),
    "python": dict(sdk="1.38.0", runtime=("cpython", "3.12.12"), ua="python-httpx/0.28.1"),
    "dotnet": dict(sdk="1.13.1", runtime=(".NET", "9.0.10"), ua=None),
}

# Instrumentation scope per language and role, as the real OTel libraries name them.
SCOPES = {
    "java": {"server": ("io.opentelemetry.spring-webmvc-6.0", "2.21.0-alpha"),
             "http_client": ("io.opentelemetry.java-http-client", "2.21.0-alpha"),
             "grpc": ("io.opentelemetry.grpc-1.6", "2.21.0-alpha"),
             "postgresql": ("io.opentelemetry.jdbc", "2.21.0-alpha"),
             "redis": ("io.opentelemetry.lettuce-5.1", "2.21.0-alpha"),
             "cassandra": ("io.opentelemetry.cassandra-4.4", "2.21.0-alpha"),
             "kafka": ("io.opentelemetry.kafka-clients-2.6", "2.21.0-alpha"),
             "internal": ("io.opentelemetry.opentelemetry-instrumentation-annotations-1.16", "2.21.0-alpha")},
    "go": {"server": ("go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp", "0.63.0"),
           "http_client": ("go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp", "0.63.0"),
           "grpc": ("go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc", "0.63.0"),
           "postgresql": ("github.com/exaring/otelpgx", "0.9.3"),
           "redis": ("github.com/redis/go-redis/extra/redisotel", "9.14.0"),
           "kafka": ("github.com/twmb/franz-go/plugin/kotel", "1.6.0"),
           "elasticsearch": ("github.com/elastic/go-elasticsearch", "9.1.0")},
    "nodejs": {"server": ("@opentelemetry/instrumentation-http", "0.206.0"),
               "http_client": ("@opentelemetry/instrumentation-undici", "0.17.0"),
               "grpc": ("@opentelemetry/instrumentation-grpc", "0.206.0"),
               "postgresql": ("@opentelemetry/instrumentation-pg", "0.59.0"),
               "redis": ("@opentelemetry/instrumentation-ioredis", "0.54.0"),
               "mongodb": ("@opentelemetry/instrumentation-mongodb", "0.59.0"),
               "kafka": ("@opentelemetry/instrumentation-kafkajs", "0.16.0")},
    "python": {"server": ("opentelemetry.instrumentation.fastapi", "0.59b0"),
               "http_client": ("opentelemetry.instrumentation.httpx", "0.59b0"),
               "grpc": ("opentelemetry.instrumentation.grpc", "0.59b0"),
               "postgresql": ("opentelemetry.instrumentation.psycopg", "0.59b0"),
               "redis": ("opentelemetry.instrumentation.redis", "0.59b0"),
               "kafka": ("opentelemetry.instrumentation.confluent_kafka", "0.59b0")},
    "dotnet": {"server": ("Microsoft.AspNetCore", ""),
               "http_client": ("System.Net.Http", ""),
               "grpc": ("Grpc.Net.Client", ""),
               "postgresql": ("Npgsql", "9.0.4"),
               "redis": ("OpenTelemetry.Instrumentation.StackExchangeRedis", "1.13.0-beta.2"),
               "kafka": ("Confluent.Kafka.Extensions.Diagnostics", "0.6.0")},
}

POD_ALPHABET = "bcdfghjklmnpqrstvwxz2456789"


class Service:
    def __init__(self, name, lang, version, k8s_ns, namespace, cluster, region="us-east-1", replicas=3, app_scope=None):
        self.name, self.lang, self.version = name, lang, version
        self.k8s_ns, self.namespace, self.cluster, self.region = k8s_ns, namespace, cluster, region
        self.app_scope = app_scope or name
        # Stable pod names per service so the same instances recur across traces.
        r = random.Random(zlib.crc32(name.encode()))
        rs = "".join(r.choice(POD_ALPHABET) for _ in range(10))[:r.choice([9, 10])]
        self.pods = []
        for i in range(replicas):
            suffix = "".join(r.choice(POD_ALPHABET) for _ in range(5))
            self.pods.append(dict(
                pod=f"{name}-{rs}-{suffix}",
                node=f"ip-10-{r.randint(20, 60)}-{r.randint(0, 255)}-{r.randint(1, 254)}.ec2.internal",
                zone=f"{region}{'abc'[i % 3]}",
                ip=f"10.{r.randint(20, 60)}.{r.randint(0, 255)}.{r.randint(1, 254)}",
                instance=uuid4(r),
            ))
        self._resources = {}

    def resource(self, pod):
        key = pod["pod"]
        if key not in self._resources:
            lang = LANG[self.lang]
            a = {
                "service.name": self.name,
                "service.namespace": self.namespace,
                "service.version": self.version,
                "service.instance.id": pod["instance"],
                "deployment.environment.name": "production",
                "telemetry.sdk.name": "opentelemetry",
                "telemetry.sdk.language": self.lang,
                "telemetry.sdk.version": lang["sdk"],
                "process.runtime.name": lang["runtime"][0],
                "process.runtime.version": lang["runtime"][1],
                "host.name": pod["pod"],
                "k8s.pod.name": pod["pod"],
                "k8s.namespace.name": self.k8s_ns,
                "k8s.deployment.name": self.name,
                "k8s.node.name": pod["node"],
                "k8s.cluster.name": self.cluster,
                "cloud.provider": "aws",
                "cloud.platform": "aws_eks",
                "cloud.region": self.region,
                "cloud.availability_zone": pod["zone"],
            }
            if "distro" in lang:
                a["telemetry.distro.name"], a["telemetry.distro.version"] = lang["distro"]
            self._resources[key] = otlp.attrs(a)
        return self._resources[key]

    def scope(self, role):
        name, version = SCOPES[self.lang].get(role, (self.app_scope, self.version))
        return {"name": name, "version": version} if version else {"name": name}


class Node:
    """One span in the backend call tree."""

    def __init__(self, svc, name, kind="internal", role="internal", attrs=None, ms=(0.5, 2.0), children=(),
                 parallel=False, error=None, async_lag=None, timeout_ms=None, min_ms=None):
        self.svc, self.name, self.kind, self.role = svc, name, kind, role
        self.attrs = dict(attrs or {})
        self.ms, self.children, self.parallel = ms, [c for c in children if c], parallel
        self.error = error            # dict(message, type, exc_message, stack) -> ERROR status + exception event
        self.async_lag = async_lag    # (p50, p95) ms: laid out after the parent ends (Kafka consumers)
        self.timeout_ms = timeout_ms  # caller gives up: span ends at the deadline even if the child runs on
        self.min_ms = min_ms
        self.logs = []                # (severity, body, attrs) emitted as backend logs
        self.start = self.end = 0.0

    def log(self, severity, body, **attrs):
        self.logs.append((severity, body, attrs))
        return self


def layout(node, start, rng):
    self_ms = lat(*node.ms, rng)
    pre = self_ms * rng.uniform(0.25, 0.6)
    t = start + pre
    sync = [c for c in node.children if c.async_lag is None]
    if node.parallel and sync:
        t = max(layout(c, t + rng.uniform(0.02, 0.4), rng) for c in sync)
    else:
        for c in sync:
            t = layout(c, t + rng.uniform(0.03, 0.25), rng)
    end = t + (self_ms - pre)
    if node.min_ms and end - start < node.min_ms:
        end = start + node.min_ms * rng.uniform(1.0, 1.08)
    if node.timeout_ms and end - start > node.timeout_ms:
        end = start + node.timeout_ms + rng.uniform(0.1, 2.0)
    node.start, node.end = start, end
    for c in node.children:
        if c.async_lag is not None:
            layout(c, end + lat(*c.async_lag, rng), rng)
    return end


def _exception_event(node):
    e = node.error
    return {"timeUnixNano": str(ns(node.end - 0.05)), "name": "exception", "attributes": otlp.attrs({
        "exception.type": e.get("type"),
        "exception.message": e.get("exc_message", e.get("message")),
        "exception.stacktrace": e.get("stack"),
    })}


class TraceBuild:
    def __init__(self, trace_id, rng):
        self.trace_id, self.rng = trace_id, rng
        self.spans = []  # (service, pod, scope, span)
        self.logs = []   # (service, pod, record)
        self._pods = {}

    def pod(self, svc):
        if svc.name not in self._pods:
            self._pods[svc.name] = self.rng.choice(svc.pods)
        return self._pods[svc.name]

    def emit(self, node, parent_span_id):
        span_id = hex_id(8, self.rng)
        span = {
            "traceId": self.trace_id, "spanId": span_id, "parentSpanId": parent_span_id,
            "name": node.name, "kind": otlp.SPAN_KIND[node.kind],
            "startTimeUnixNano": str(ns(node.start)), "endTimeUnixNano": str(ns(node.end)),
            "attributes": otlp.attrs(node.attrs), "flags": 257,
            "status": {"code": otlp.STATUS_UNSET},
        }
        if node.error:
            span["status"] = {"code": otlp.STATUS_ERROR, "message": node.error.get("message", "")}
            if node.error.get("type"):
                span["events"] = [_exception_event(node)]
        pod = self.pod(node.svc)
        self.spans.append((node.svc, pod, node.svc.scope(node.role), span))
        for sev, body, extra in node.logs:
            self.logs.append((node.svc, pod, _log_record(self.trace_id, span_id, node, sev, body, extra)))
        for c in node.children:
            self.emit(c, span_id)
        return span_id

    def traces_request(self):
        return {"resourceSpans": _group(self.spans, "scopeSpans", "spans")}

    def logs_request(self):
        if not self.logs:
            return None
        items = [(svc, pod, {"name": svc.app_scope}, rec) for svc, pod, rec in self.logs]
        return {"resourceLogs": _group(items, "scopeLogs", "logRecords")}


SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}


def _log_record(trace_id, span_id, node, sev, body, extra):
    t = node.end if sev == "ERROR" else node.start + (node.end - node.start) * 0.5
    return {"timeUnixNano": str(ns(t)), "observedTimeUnixNano": str(ns(t + 0.4)),
            "severityNumber": SEVERITY[sev], "severityText": sev, "body": {"stringValue": body},
            "attributes": otlp.attrs(extra), "traceId": trace_id, "spanId": span_id, "flags": 1}


def _group(items, scope_key, list_key):
    by_res = {}
    for svc, pod, scope, item in items:
        res = by_res.setdefault(pod["pod"], {"resource": {"attributes": svc.resource(pod)}, scope_key: {}})
        sk = (scope["name"], scope.get("version", ""))
        res[scope_key].setdefault(sk, {"scope": scope, list_key: []})[list_key].append(item)
    return [{"resource": r["resource"], scope_key: list(r[scope_key].values())} for r in by_res.values()]


def build_trace(root, trace_id, parent_span_id, start_ms, rng):
    """Lay out root at start_ms and emit spans that continue the given traceparent."""
    layout(root, start_ms, rng)
    tb = TraceBuild(trace_id, rng)
    tb.emit(root, parent_span_id)
    return tb


# --- builders for common hops ------------------------------------------------

def _err(message, type_=None, exc_message=None, stack=None):
    return dict(message=message, type=type_, exc_message=exc_message or message, stack=stack)


def server(svc, method, route, path, status=200, ms=(1.5, 6), children=(), parallel=False, error=None,
           client_ip=None, user_agent=None, extra=None):
    a = {"http.request.method": method, "http.route": route, "url.path": path, "url.scheme": "http",
         "http.response.status_code": status, "server.address": svc.name, "server.port": 8080,
         "network.protocol.version": "1.1", "client.address": client_ip, "user_agent.original": user_agent}
    if status >= 500:
        a["error.type"] = str(status)
    a.update(extra or {})
    if status >= 500 and not error:
        error = _err(f"HTTP {status}")
    return Node(svc, f"{method} {route}", "server", "server", a, ms, children, parallel, error)


def http(caller, callee, method, route, path, status=200, ms=(1.5, 6), children=(), parallel=False,
         error=None, client_error=None, timeout_ms=None, server_extra=None):
    """Outgoing HTTP client span in caller wrapping the callee's server span."""
    srv = server(callee, method, route, path, status, ms, children, parallel, error,
                 client_ip=random.choice(caller.pods)["ip"], user_agent=LANG[caller.lang]["ua"], extra=server_extra)
    a = {"http.request.method": method, "url.full": f"http://{callee.name}.{callee.k8s_ns}.svc.cluster.local:8080{path}",
         "server.address": f"{callee.name}.{callee.k8s_ns}.svc.cluster.local", "server.port": 8080,
         "network.protocol.version": "1.1", "http.response.status_code": None if client_error else status}
    if status >= 400 or client_error:
        a["error.type"] = client_error["error_type"] if client_error else str(status)
        client_error = client_error or _err(f"HTTP {status}")
    return Node(caller, method, "client", "http_client", a, (0.2, 0.9), [srv], error=client_error, timeout_ms=timeout_ms)


GRPC_CODES = {0: "OK", 3: "INVALID_ARGUMENT", 4: "DEADLINE_EXCEEDED", 5: "NOT_FOUND", 7: "PERMISSION_DENIED",
              9: "FAILED_PRECONDITION", 10: "ABORTED", 13: "INTERNAL", 14: "UNAVAILABLE", 16: "UNAUTHENTICATED"}
# gRPC server spans are only marked as errors for these codes (OTel semconv); client spans for any non-OK code.
GRPC_SERVER_ERRORS = {2, 4, 12, 13, 14, 15}


def grpc(caller, callee, service, method, code=0, ms=(1.0, 5), children=(), parallel=False, error=None,
         timeout_ms=None):
    name = f"{service}/{method}"
    base = {"rpc.system": "grpc", "rpc.service": service, "rpc.method": method, "rpc.grpc.status_code": code}
    srv_err = error if error else (_err(GRPC_CODES[code]) if code in GRPC_SERVER_ERRORS else None)
    srv = Node(callee, name, "server", "grpc", dict(base, **{"server.address": callee.name, "server.port": 50051}),
               ms, children, parallel, srv_err)
    cli = Node(caller, name, "client", "grpc", dict(base, **{
        "server.address": f"{callee.name}.{callee.k8s_ns}.svc.cluster.local", "server.port": 50051}),
        (0.15, 0.7), [srv], error=_err(GRPC_CODES[code]) if code else None, timeout_ms=timeout_ms)
    return cli


def sql(svc, op, table, statement, db="app", host=None, ms=(0.8, 4), error=None, rows=None):
    a = {"db.system.name": "postgresql", "db.namespace": db, "db.collection.name": table,
         "db.operation.name": op, "db.query.text": statement, "server.address": host or f"{db}-db.internal",
         "server.port": 5432, "db.response.returned_rows": rows}
    return Node(svc, f"{op} {db}.{table}", "client", "postgresql", a, ms, error=error)


def redis(svc, op, key, host, ms=(0.3, 1.4), error=None):
    a = {"db.system.name": "redis", "db.operation.name": op, "db.query.text": f"{op} {key}", "db.namespace": "0",
         "server.address": host, "server.port": 6379}
    return Node(svc, op, "client", "redis", a, ms, error=error)


def nosql(svc, system, op, collection, ms=(1, 6), host=None, statement=None, error=None):
    a = {"db.system.name": system, "db.operation.name": op, "db.collection.name": collection,
         "db.query.text": statement, "server.address": host}
    return Node(svc, f"{op} {collection}", "client", system if system in ("mongodb", "cassandra", "elasticsearch") else "internal",
                a, ms, error=error)


def kafka(producer_svc, topic, consumer_svc, group, key, consumer_children=(), lag=(25, 180), ms=(0.4, 2.5),
          consumer_ms=(1.5, 8)):
    base = {"messaging.system": "kafka", "messaging.destination.name": topic,
            "messaging.kafka.message.key": key, "server.address": "b-1.msk-prod.kafka.us-east-1.amazonaws.com",
            "server.port": 9098}
    partition = str(random.randint(0, 11))
    consumer = Node(consumer_svc, f"process {topic}", "consumer", "kafka", dict(base, **{
        "messaging.operation.type": "process", "messaging.operation.name": "process",
        "messaging.consumer.group.name": group, "messaging.destination.partition.id": partition,
        "messaging.kafka.offset": random.randint(10_000_000, 99_999_999)}), consumer_ms, consumer_children)
    consumer.async_lag = lag
    return Node(producer_svc, f"send {topic}", "producer", "kafka", dict(base, **{
        "messaging.operation.type": "send", "messaging.operation.name": "send",
        "messaging.destination.partition.id": partition}), ms, [consumer])


def external(svc, method, url, peer, status=200, ms=(80, 250), error=None, timeout_ms=None):
    host = url.split("/")[2]
    a = {"http.request.method": method, "url.full": url, "server.address": host, "server.port": 443,
         "peer.service": peer, "http.response.status_code": None if (error and status == 0) else status,
         "network.protocol.version": "1.1"}
    if status >= 400 or error:
        a["error.type"] = error.get("error_type", str(status)) if error else str(status)
        error = error or _err(f"HTTP {status}")
    return Node(svc, method, "client", "http_client", a, ms, error=error, timeout_ms=timeout_ms)


def internal(svc, name, ms=(0.3, 2), children=(), parallel=False, error=None, attrs=None):
    return Node(svc, name, "internal", "internal", attrs, ms, children, parallel, error)


def err(message, type_=None, exc_message=None, stack=None, error_type=None):
    e = _err(message, type_, exc_message, stack)
    if error_type:
        e["error_type"] = error_type
    return e


def stack(lang, exc_type, message, frames):
    """Language-shaped stack trace text from a list of (function, file, line)."""
    if lang == "java":
        return f"{exc_type}: {message}\n" + "\n".join(f"\tat {f}({fl}:{ln})" for f, fl, ln in frames)
    if lang == "go":
        return f"{message}\n\ngoroutine {random.randint(200, 9000)} [running]:\n" + "\n".join(
            f"{f}(...)\n\t{fl}:{ln} +0x{random.randint(16, 900):x}" for f, fl, ln in frames)
    if lang == "python":
        body = "\n".join(f'  File "{fl}", line {ln}, in {f}' for f, fl, ln in reversed(frames))
        return f"Traceback (most recent call last):\n{body}\n{exc_type}: {message}"
    if lang == "dotnet":
        return f"{exc_type}: {message}\n" + "\n".join(f"   at {f} in {fl}:line {ln}" for f, fl, ln in frames)
    return f"{exc_type}: {message}\n" + "\n".join(f"    at {f} ({fl}:{ln})" for f, fl, ln in frames)
