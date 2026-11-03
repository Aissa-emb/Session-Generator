"""Scenario-driven cache, admission, asynchronous and deployment failures."""
from . import backend as b
from .generic_faults import EXPERIENCE, FAILURES, _frames
from .profiles import platform


VARIANTS = {
    "catalog-cache-outage": dict(type="cache_outage", service="product-catalog-service", story="backend",
        endpoints={"retail": "get_product"}, summary="Catalog Redis is unreachable; product reads fall back to PostgreSQL."),
    "accounts-cache-outage": dict(type="cache_outage", service="account-summary-service", story="backend",
        endpoints={"banking": "accounts"}, summary="Balance cache is unreachable; account reads fall back to PostgreSQL."),
    "gateway-rate-limited": dict(type="rate_limited", service="api-gateway", story="mobile",
        endpoints={"retail": "search", "banking": "accounts", "travel": "flight_search"},
        summary="The gateway returns 429 and Retry-After before calling an experience API."),
    "fulfillment-consumer-lag": dict(type="kafka_consumer_lag", service="customer-notification-service", story="backend",
        endpoints={"retail": "place_order", "banking": "create_transfer", "travel": "create_booking"},
        summary="Kafka consumers run tens of seconds later; operation status remains pending until processing completes."),
    "catalog-deploy-regression": dict(type="deploy_regression", service="product-catalog-service", story="mobile",
        endpoints={"retail": "get_product"}, regression_version="5.23.0",
        summary="The new catalog serializer fails only on version 5.23.0; rollback restores healthy reads."),
}
EVIDENCE = {
    "catalog-cache-outage": {"client": {"log_contains": "Cache unavailable; database fallback"}, "backend": {"cache_outage": True}},
    "accounts-cache-outage": {"client": {"log_contains": "Cache unavailable; database fallback"}, "backend": {"cache_outage": True}},
    "gateway-rate-limited": {"client": {"status": 429}},
    "fulfillment-consumer-lag": {"client": {"log_contains": "Processing is pending"}, "backend": {"consumer_lag": True}},
    "catalog-deploy-regression": {"client": {"status": 500}, "backend": {"error_contains": "Serializer"}},
}


def walk(root):
    yield root
    for child in root.children:
        yield from walk(child)


def build(ctx, ep, key, fault):
    kind, d = fault["type"], fault["details"]
    if kind == "rate_limited":
        seconds = d.get("retry_after_seconds", 3)
        gw = ctx.svc["api-gateway"]
        limit = b.redis(gw, "EVALSHA", "ratelimit:<client>:<route>", platform.REDIS.format("edge-ratelimit"))
        limit.attrs.update({"ratelimit.allowed": False, "ratelimit.remaining": 0})
        root = ctx.edge(ep.method, ep.route, ep.path(ctx.params), 429,
                        [b.internal(gw, "authn.ValidateAccessToken", ms=(.2, .9)), limit])
        root.attrs["http.response.header.retry_after"] = [str(seconds)]
        return root, 429, {"retry_after_seconds": seconds}
    if kind == "cache_outage":
        # Preserve the successful response's business data, while rebuilding its dependency path.
        _, _, data = ep.build(ctx)
        svc, exp = ctx.svc[fault["service"]], ctx.svc[EXPERIENCE[ctx.session.profile.key]]
        host = platform.REDIS.format("catalog-cache" if key == "get_product" else "accounts-cache")
        exc, msg = {"java": ("io.lettuce.core.RedisConnectionException", f"Unable to connect to {host}:6379"),
                    "go": ("net.OpError", f"dial tcp {host}:6379: i/o timeout"),
                    "python": ("redis.exceptions.ConnectionError", f"Error connecting to {host}:6379"),
                    "nodejs": ("Error", f"connect ECONNREFUSED {host}:6379"),
                    "dotnet": ("StackExchange.Redis.RedisConnectionException", "No connection is active/available to service this operation")}[svc.lang]
        cache = b.redis(svc, "GET", "product:current" if key == "get_product" else "balances:current", host,
            ms=(300, 480), error=b.err(msg, exc, stack=b.stack(svc.lang, exc, msg, _frames(svc, "read"))))
        cache.attrs.update({"cache.hit": False, "circuit_breaker.state": "open"})
        cache.log("WARN", "Cache connection failed; circuit breaker opened", **{"circuit_breaker.state": "open"})
        fallback = d.get("fallback", True)
        table, db = ("products", "catalog") if key == "get_product" else ("accounts", "accounts")
        children = [cache]
        if fallback:
            children.append(b.sql(svc, "SELECT", table, f"SELECT * FROM {table} WHERE id = ?", db,  # nosec B608 -- telemetry text; table is a fixed internal constant
                                  platform.RDS.format(db + "-prod"), ms=(180, 600), rows=1))
        status = 200 if fallback else 503
        route = "/v1/products/{id}" if key == "get_product" else "/v2/customers/{id}/accounts"
        call = b.http(exp, svc, "GET", route, route, status, (2, 8), children)
        root = platform.gateway(ctx, exp, ep.method, ep.route, ep.path(ctx.params), [call], status=status)
        return root, status, dict(data, cache_fallback=fallback) if fallback else {}
    if kind == "deploy_regression":
        from .generic_faults import http_error
        root, code, data = http_error(ctx, ep, key, {"status": 500, "service": fault["service"]})
        svc = ctx.svc[fault["service"]]
        typ, msg = ("java.lang.NullPointerException", 'Cannot invoke "String.toUpperCase()" because "currency" is null') if svc.lang == "java" else FAILURES["internal"][svc.lang]
        frames = _frames(svc, "serialize")
        frames[0] = (frames[0][0].replace("Service", "Serializer"), frames[0][1].replace("Service", "Serializer"), 84)
        for node in walk(root):
            if node.svc is svc and node.kind == "internal":
                node.error = b.err(msg, typ, stack=b.stack(svc.lang, typ, msg, frames))
        return root, code, data
    if kind == "kafka_consumer_lag":
        root, status, data = ep.build(ctx)
        lag = d.get("lag_seconds", 90) * 1000
        consumers = [n for n in walk(root) if n.kind == "consumer"]
        if not consumers:
            raise ValueError("The selected endpoint has no asynchronous consumer.")
        for consumer in consumers:
            consumer.async_lag = (lag, lag)
            consumer.attrs["messaging.kafka.consumer.lag"] = d.get("lag_messages", 18000)
            consumer.attrs["messaging.kafka.consumer.lag_seconds"] = lag / 1000
        return root, status, dict(data, state="pending", ready_at=ctx.session.t + lag, processing_delayed=True)
    raise ValueError("Unsupported backend fault builder")
