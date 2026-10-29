"""Platform services shared by every line of business: edge, identity, customer profile, payments, risk, notifications.

Retail checkouts and travel bookings both authorize through payment-authorization-service, so a fault there
shows up in both lines of business at once.
"""

import random

from .. import backend as b
from . import services

P = services("vantra-platform", "prod-use1-platform", [
    ("api-gateway", "go", "4.18.2", "edge", 8),
    ("customer-identity-service", "java", "7.21.0", "identity", 4),
    ("customer-profile-service", "java", "5.9.3", "customer", 3),
    ("payment-authorization-service", "java", "9.6.1", "payments", 6),
    ("fraud-risk-scoring-service", "python", "5.8.0", "risk", 4),
    ("customer-notification-service", "nodejs", "3.14.2", "notifications", 3),
])

RDS = "{}.cluster-k3v9q2w7m1xe.us-east-1.rds.amazonaws.com"
REDIS = "{}.r8t4yb.ng.0001.use1.cache.amazonaws.com"
PSP = "https://api.paycore-acquiring.com/v2/payments"

HIKARI_STACK = b.stack("java", "java.sql.SQLTransientConnectionException",
                       "HikariPool-1 - Connection is not available, request timed out after 3000ms.", [
                           ("com.zaxxer.hikari.pool.HikariPool.createTimeoutException", "HikariPool.java", 710),
                           ("com.zaxxer.hikari.pool.HikariPool.getConnection", "HikariPool.java", 189),
                           ("com.zaxxer.hikari.HikariDataSource.getConnection", "HikariDataSource.java", 100),
                           ("org.springframework.jdbc.datasource.DataSourceUtils.fetchConnection", "DataSourceUtils.java", 160),
                           ("com.vantra.platform.payments.repository.PaymentAttemptRepository.insert", "PaymentAttemptRepository.java", 58),
                           ("com.vantra.platform.payments.service.AuthorizationService.authorize", "AuthorizationService.java", 117),
                           ("com.vantra.platform.payments.api.AuthorizationController.authorize", "AuthorizationController.java", 64)])


def gateway(ctx, experience, method, route, path, children, status=200, parallel=False, error=None, ms=(3, 10),
            with_profile=False):
    """api-gateway (token check, rate limit) -> experience API, the first two hops of every client request."""
    gw = P["api-gateway"]
    auth = b.internal(gw, "authn.ValidateAccessToken", ms=(0.2, 0.9), attrs={"auth.scheme": "bearer"})
    if ctx.user_id and random.random() < 0.12:  # token cache miss: introspect with the identity service
        ids = P["customer-identity-service"]
        auth.children.append(b.grpc(gw, ids, "vantra.identity.v2.TokenService", "Introspect", ms=(1.2, 4), children=[
            b.redis(ids, "GET", "token:<sha256>", REDIS.format("identity-tokens"))]))
    limit = b.redis(gw, "EVALSHA", "ratelimit:<client>:<route>", REDIS.format("edge-ratelimit"), ms=(0.3, 1.2))
    if random.random() < 0.006:
        # Rate limiter times out; the gateway fails open and the request still succeeds.
        limit.ms, limit.min_ms = (250, 260), 250
        limit.error = b.err("redis: command timeout", "redis.TimeoutError", "i/o timeout after 250ms", error_type="timeout")
        auth.log("WARN", "rate limiter unavailable, failing open", route=route)
    steps = list(children)
    if with_profile and ctx.user_id:
        steps.insert(0, profile_lookup(experience, ctx))
    srv = b.http(gw, experience, method, route, path, status, ms, steps, parallel, error=error)
    return ctx.edge(method, route, path, status, [auth, limit, srv])


def profile_lookup(caller, ctx):
    cps = P["customer-profile-service"]
    kids = [b.redis(cps, "GET", f"profile:{ctx.user_id[:8]}", REDIS.format("customer-profile"))]
    if random.random() < 0.15:
        kids += [b.sql(cps, "SELECT", "customers",
                       "SELECT id, display_name, membership_tier, locale, marketing_opt_in FROM customers WHERE id = ?",
                       "customer", RDS.format("customer-prod"), ms=(1.2, 4), rows=1),
                 b.redis(cps, "SETEX", f"profile:{ctx.user_id[:8]}", REDIS.format("customer-profile"))]
    return b.grpc(caller, cps, "vantra.customer.v1.ProfileService", "GetProfile", ms=(0.8, 3), children=kids)


def risk_score(caller, kind, slow=False, timed_out=False):
    frs = P["fraud-risk-scoring-service"]
    model = b.internal(frs, "model.predict", ms=(4300, 6800) if timed_out else (1800, 3400) if slow else (8, 30),
                       attrs={"model.name": f"{kind}-risk-gbm", "model.version": "2026.10.2"})
    return b.grpc(caller, frs, "vantra.risk.v3.RiskScoring", f"Score{kind.title()}", ms=(2, 6),
                  code=4 if timed_out else 0, timeout_ms=4000 if timed_out else None, children=[
                      b.redis(frs, "HGETALL", "features:customer:<id>", REDIS.format("risk-features")),
                      b.redis(frs, "ZCOUNT", "velocity:<id>:24h", REDIS.format("risk-features")), model])


def authorize_payment(caller, mode, db_pressure=False):
    """Client hop into payment-authorization-service. mode: ok, declined, processor_timeout, db_pool, 3ds_failed.

    Returns (node, HTTP status). db_pool is connection pool exhaustion: the insert waits the full 3 s Hikari
    timeout and the service answers 503. db_pressure (pool incident active) makes the payment-token lock
    query slow on requests that still get a connection, which is what drains the pool.
    """
    pas = P["payment-authorization-service"]
    steps = [b.internal(pas, "PaymentRequestValidator.validate", ms=(0.3, 1.2)),
             risk_score(pas, "payment")]
    log = None
    if mode == "db_pool":
        steps.append(b.Node(pas, "HikariPool.getConnection", "internal", "internal",
                            {"db.client.connection.pool.name": "HikariPool-1", "db.client.connection.pool.max": 50,
                             "db.client.connection.pool.pending_requests": random.randint(28, 46)},
                            ms=(3000, 3000), min_ms=3000,
                            error=b.err("HikariPool-1 - Connection is not available, request timed out after 3000ms.",
                                        "java.sql.SQLTransientConnectionException", stack=HIKARI_STACK)))
        status = 503
        error = b.err("Failed to obtain JDBC Connection", "org.springframework.jdbc.CannotGetJdbcConnectionException",
                      "Failed to obtain JDBC Connection; nested exception is java.sql.SQLTransientConnectionException: "
                      "HikariPool-1 - Connection is not available, request timed out after 3000ms.", HIKARI_STACK)
        log = ("ERROR", "payment authorization failed: database connection pool exhausted",
               {"db.client.connection.pool.name": "HikariPool-1", "pool.active": 50, "pool.idle": 0,
                "pool.waiting": random.randint(28, 46)})
    else:
        lock_ms = (1500, 2800) if db_pressure else (1.5, 6)
        steps.append(b.sql(pas, "SELECT", "payment_tokens",
                           "SELECT token_ref, network, exp_month, exp_year FROM payment_tokens "
                           "WHERE customer_id = ? AND is_default = true FOR UPDATE", "payments",
                           RDS.format("payments-prod"), ms=lock_ms, rows=1))
        steps.append(b.sql(pas, "INSERT", "payment_attempts",
                           "INSERT INTO payment_attempts (id, customer_id, amount_minor, currency, status) "
                           "VALUES (?, ?, ?, ?, 'PENDING')", "payments", RDS.format("payments-prod"), ms=(1.2, 4)))
        error = None
        if mode == "processor_timeout":
            steps.append(b.external(pas, "POST", PSP, "card-acquirer", status=0, ms=(9000, 14000), timeout_ms=8000,
                                    error=b.err("Read timed out", "java.net.http.HttpTimeoutException", "request timed out",
                                                error_type="java.net.http.HttpTimeoutException")))
            status = 504
            error = b.err("card acquirer did not respond within 8000 ms", "com.vantra.platform.payments.AcquirerTimeoutException")
            log = ("ERROR", "card authorization timed out after 8000ms", {"provider": "card-acquirer"})
        elif mode in ("declined", "3ds_failed"):
            steps.append(b.external(pas, "POST", PSP, "card-acquirer", status=402, ms=(180, 520)))
            status = 402
            log = ("INFO", "card authorization declined", {"provider": "card-acquirer", "decline_code": random.choice(
                ["insufficient_funds", "do_not_honor", "authentication_required"] if mode == "declined"
                else ["authentication_failed"])})
        else:
            steps.append(b.external(pas, "POST", PSP, "card-acquirer", status=201, ms=(220, 640)))
            status = 201
        steps.append(b.sql(pas, "UPDATE", "payment_attempts",
                           "UPDATE payment_attempts SET status = ?, acquirer_ref = ? WHERE id = ?", "payments",
                           RDS.format("payments-prod"), ms=(1, 3.5)))
    node = b.http(caller, pas, "POST", "/v1/authorizations", "/v1/authorizations", status, (3, 9), steps, error=error)
    if log:
        node.children[0].log(log[0], log[1], **log[2])
    return node, status


def notify(producer, topic, key, channel="email"):
    """Event to customer-notification-service: render a template and hand it to the delivery provider."""
    cns = P["customer-notification-service"]
    provider = ("https://api.mailrelay.io/v3/mail/send", "email-provider") if channel == "email" else \
        ("https://fcm.googleapis.com/v1/projects/vantra-prod/messages:send", "fcm")
    return b.kafka(producer, topic, cns, "customer-notifications", key, [
        b.internal(cns, "template.render", ms=(2, 8), attrs={"template.channel": channel}),
        b.external(cns, "POST", provider[0], provider[1], status=202 if channel == "email" else 200, ms=(60, 200))])
