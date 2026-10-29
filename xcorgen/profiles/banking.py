"""Corvia Bank: retail online banking (sign-in with MFA, balances, transfers, bills, cards)."""

import random

from .. import backend as b
from ..util import uuid4
from . import Endpoint, Page, Profile, platform, services

S = services("vantra-money", "prod-use1-money", [
    ("banking-experience-api", "java", "12.4.1", "digital", 8),
    ("strong-auth-service", "go", "2.8.3", "identity", 3),
    ("account-summary-service", "java", "10.2.5", "accounts", 4),
    ("core-banking-gateway", "java", "4.33.0", "core", 4),
    ("transaction-history-service", "go", "6.1.2", "accounts", 4),
    ("payee-management-service", "dotnet", "3.6.0", "payments", 2),
    ("funds-transfer-service", "java", "8.14.2", "payments", 4),
    ("transaction-limits-service", "go", "1.22.0", "payments", 2),
    ("payment-rails-gateway", "java", "3.9.4", "payments", 3),
    ("general-ledger-service", "java", "11.0.3", "core", 3),
    ("bill-payment-service", "dotnet", "2.15.1", "payments", 2),
    ("card-management-service", "dotnet", "4.2.0", "cards", 2),
    ("statement-rendering-service", "python", "2.4.8", "documents", 2),
])
S.update(platform.P)

RDS = "{}.cluster-r4k8p2m1v6qa.us-east-1.rds.amazonaws.com"
REDIS = "{}.b9xw3e.ng.0001.use1.cache.amazonaws.com"
CORE = "https://cbs-gateway.money.vantra.internal"
PAYEES = ["Northeast Electric", "City Water & Sewer", "Brightline Mobile", "Summit Auto Loans", "Harbor Mortgage",
          "Jordan Alvarez", "Priya Natarajan", "Sam Okafor", "Lena Fischer"]


# Routes where the experience API personalizes the response with the customer's profile.
PROFILE_ROUTES = {'/api/v1/transfers', '/api/v1/accounts'}


def bff(ctx, method, route, path, children, status=200, parallel=False, error=None, ms=(3, 10)):
    """api-gateway -> banking-experience-api: token check and rate limit at the edge, then the experience API."""
    return platform.gateway(ctx, S["banking-experience-api"], method, route, path, children, status, parallel, error, ms,
                            with_profile=route in PROFILE_ROUTES)


def core_call(caller, op, ms=(140, 520), status=200, timeout_ms=None, error=None):
    return b.external(caller, "POST", f"{CORE}/cbs/v2/{op}", "core-banking-system", status=status, ms=ms,
                      timeout_ms=timeout_ms, error=error)


def login(ctx):
    locked = ctx.outcome == "bad_password"
    ident = b.http(S["banking-experience-api"], S["customer-identity-service"], "POST", "/v2/authenticate", "/v2/authenticate",
                   401 if locked else 200, (3, 9), [
                       b.sql(S["customer-identity-service"], "SELECT", "credentials",
                             "SELECT customer_id, password_hash, failed_attempts, status FROM credentials WHERE username = ?",
                             "identity", RDS.format("identity-prod"), ms=(1.2, 4), rows=1),
                       b.internal(S["customer-identity-service"], "Argon2PasswordEncoder.matches", ms=(70, 110)),
                       b.redis(S["customer-identity-service"], "INCR", "login_attempts:<customer>", REDIS.format("identity-cache")),
                       None if locked else b.grpc(S["customer-identity-service"], S["strong-auth-service"], "vantra.money.mfa.v1.MfaService",
                                                  "StartChallenge", ms=(2, 6), children=[
                                                      b.redis(S["strong-auth-service"], "SETEX", "mfa:challenge:<id>", REDIS.format("mfa-cache")),
                                                      b.external(S["strong-auth-service"], "POST", "https://push.notifyhub.io/v1/messages",
                                                                 "push-provider", status=202, ms=(60, 180))])])
    status = 401 if locked else 200
    return bff(ctx, "POST", "/api/v1/auth/login", "/api/v1/auth/login", [ident], status=status), status, {}


def mfa_verify(ctx):
    v = b.http(S["banking-experience-api"], S["strong-auth-service"], "POST", "/v1/challenges/{id}/verify",
               "/v1/challenges/current/verify", 200, (1.5, 4), [
                   b.redis(S["strong-auth-service"], "GET", "mfa:challenge:<id>", REDIS.format("mfa-cache")),
                   b.grpc(S["strong-auth-service"], S["customer-identity-service"], "vantra.money.identity.v1.TokenService", "IssueTokens",
                          ms=(3, 8), children=[
                              b.internal(S["customer-identity-service"], "jwt.sign", ms=(0.8, 2.5)),
                              b.redis(S["customer-identity-service"], "SET", "session:<id>", REDIS.format("identity-cache"))])])
    return bff(ctx, "POST", "/api/v1/auth/mfa/verify", "/api/v1/auth/mfa/verify", [v]), 200, {}


def accounts(ctx):
    cached = random.random() < 0.55
    slow_core = ctx.incident == "core-banking-latency"
    inner = [b.redis(S["account-summary-service"], "GET", f"balances:{ctx.user_id}", REDIS.format("accounts-cache"))]
    if not cached and random.random() < 0.02:
        # First call to the core banking gateway is refused while a pod restarts; the client retries.
        inner.append(b.http(S["account-summary-service"], S["core-banking-gateway"], "GET", "/v3/customers/{id}/positions",
                            "/v3/customers/<id>/positions", 503, (0.5, 2)))
        inner[-1].log("WARN", "core-banking-gateway unavailable, retrying", **{"retry.attempt": 1})
    if not cached:
        inner.append(b.http(S["account-summary-service"], S["core-banking-gateway"], "GET", "/v3/customers/{id}/positions",
                            "/v3/customers/<id>/positions", 200, (4, 12), [
                                core_call(S["core-banking-gateway"], "InquirePositions",
                                          ms=(1400, 3800) if slow_core else (160, 480))]))
        inner.append(b.redis(S["account-summary-service"], "SETEX", f"balances:{ctx.user_id}", REDIS.format("accounts-cache")))
    inner.append(b.sql(S["account-summary-service"], "SELECT", "accounts",
                       "SELECT account_id, product_code, nickname, status FROM accounts WHERE customer_id = ?",
                       "accounts", RDS.format("accounts-prod"), ms=(1.2, 4), rows=random.randint(2, 5)))
    srv = b.http(S["banking-experience-api"], S["account-summary-service"], "GET", "/v2/customers/{id}/accounts",
                 "/v2/customers/<id>/accounts", 200, (2, 6), inner)
    return bff(ctx, "GET", "/api/v1/accounts", "/api/v1/accounts", [srv]), 200, {}


def transactions(ctx):
    acct = ctx.params["account"]
    srv = b.http(S["banking-experience-api"], S["transaction-history-service"], "GET", "/v1/accounts/{id}/transactions",
                 f"/v1/accounts/{acct}/transactions?limit=50", 200, (2, 6), [
                     b.nosql(S["transaction-history-service"], "cassandra", "SELECT", "transactions_by_account", ms=(4, 16),
                             host="cassandra-txn.money.vantra.internal",
                             statement="SELECT * FROM ledger.transactions_by_account WHERE account_id = ? AND month IN (?, ?) LIMIT 50"),
                     b.internal(S["transaction-history-service"], "merchant.enrich", ms=(3, 11), attrs={"merchant.count": 50})])
    return bff(ctx, "GET", "/api/v1/accounts/:accountId/transactions", f"/api/v1/accounts/{acct}/transactions?limit=50",
               [srv]), 200, {}


def payees(ctx):
    srv = b.http(S["banking-experience-api"], S["payee-management-service"], "GET", "/api/payees", "/api/payees", 200, (1.5, 5), [
        b.sql(S["payee-management-service"], "SELECT", "payees",
              "SELECT id, display_name, routing_number, account_mask, kind FROM payees WHERE customer_id = @customerId",
              "payments", RDS.format("payments-prod"), ms=(1.4, 5), rows=random.randint(4, 14))])
    return bff(ctx, "GET", "/api/v1/payees", "/api/v1/payees", [srv]), 200, {}


def fraud_score(caller, slow):
    timed_out = slow and random.random() < 0.35
    return b.grpc(caller, S["fraud-risk-scoring-service"], "vantra.money.risk.v1.RiskScoring", "ScoreTransfer",
                  ms=(3, 9), children=[
                      b.redis(S["fraud-risk-scoring-service"], "HGETALL", "features:customer:<id>", REDIS.format("risk-features")),
                      b.redis(S["fraud-risk-scoring-service"], "ZRANGEBYSCORE", "velocity:<id>", REDIS.format("risk-features")),
                      b.internal(S["fraud-risk-scoring-service"], "model.predict",
                                 ms=(4300, 6800) if timed_out else (1800, 3400) if slow else (14, 45),
                                 attrs={"model.name": "transfer-risk-lgbm", "model.version": "2026-10-01"})],
                  code=4 if timed_out else 0, timeout_ms=4000 if timed_out else None)


def create_transfer(ctx):
    o = ctx.outcome
    slow = ctx.incident == "fraud-latency" or o == "fraud_slow"
    tid = f"TRF{random.randint(10**9, 10**10 - 1)}"
    limits = b.grpc(S["funds-transfer-service"], S["transaction-limits-service"], "vantra.money.limits.v1.LimitsService", "CheckLimit",
                    code=9 if o == "limit_exceeded" else 0, ms=(1, 3), children=[
                        b.sql(S["transaction-limits-service"], "SELECT", "daily_usage",
                              "SELECT sum(amount_cents) FROM daily_usage WHERE customer_id = $1 AND day = current_date",
                              "payments", RDS.format("payments-prod"), ms=(1.2, 4), rows=1)])
    steps = [b.sql(S["funds-transfer-service"], "SELECT", "payees",
                   "SELECT routing_number, account_number_enc FROM payees WHERE id = ? AND customer_id = ?",
                   "payments", RDS.format("payments-prod"), ms=(1.2, 4), rows=1), limits]
    if o == "limit_exceeded":
        status = 422
    else:
        score = fraud_score(S["funds-transfer-service"], slow)
        steps.append(score)
        timed_out = score.attrs.get("rpc.grpc.status_code") == 4
        if timed_out:
            status = 504
            score.error = b.err("DEADLINE_EXCEEDED", "io.grpc.StatusRuntimeException",
                                "DEADLINE_EXCEEDED: deadline exceeded after 3.999s",
                                b.stack("java", "io.grpc.StatusRuntimeException", "DEADLINE_EXCEEDED: deadline exceeded after 3.999s", [
                                    ("io.grpc.stub.ClientCalls.toStatusRuntimeException", "ClientCalls.java", 268),
                                    ("com.vantra.money.transfers.risk.RiskClient.score", "RiskClient.java", 61),
                                    ("com.vantra.money.transfers.service.TransferService.submit", "TransferService.java", 143)]))
        elif o == "rail_unavailable":
            steps.append(b.http(S["funds-transfer-service"], S["payment-rails-gateway"], "POST", "/v1/instant-payments",
                                "/v1/instant-payments", 503, (3, 8), [
                                    b.external(S["payment-rails-gateway"], "POST", "https://gw.instantrail.net/rtp/v1/credit-transfers",
                                               "instant-payments-network", status=503, ms=(40, 140))]))
            status = 503
        else:
            steps.append(b.http(S["funds-transfer-service"], S["payment-rails-gateway"], "POST", "/v1/instant-payments",
                                "/v1/instant-payments", 201, (3, 8), [
                                    b.external(S["payment-rails-gateway"], "POST", "https://gw.instantrail.net/rtp/v1/credit-transfers",
                                               "instant-payments-network", status=201, ms=(180, 620))]))
            steps.append(b.sql(S["funds-transfer-service"], "INSERT", "transfers",
                               "INSERT INTO transfers (id, customer_id, payee_id, amount_cents, status) VALUES (?, ?, ?, ?, 'SETTLED')",
                               "payments", RDS.format("payments-prod"), ms=(1.5, 5)))
            steps.append(b.kafka(S["funds-transfer-service"], "ledger-postings", S["general-ledger-service"], "ledger", tid, [
                b.sql(S["general-ledger-service"], "INSERT", "journal_entries",
                      "INSERT INTO journal_entries (txn_id, account_id, direction, amount_cents) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
                      "ledger", RDS.format("ledger-prod"), ms=(2, 7))]))
            steps.append(b.kafka(S["funds-transfer-service"], "customer-notifications", S["customer-notification-service"],
                                 "notifications", tid, [
                                     b.external(S["customer-notification-service"], "POST", "https://push.notifyhub.io/v1/messages",
                                                "push-provider", status=202, ms=(50, 160))]))
            status = 201
    srv = b.http(S["banking-experience-api"], S["funds-transfer-service"], "POST", "/v2/transfers", "/v2/transfers", status,
                 (3, 10), steps)
    if status == 504:
        srv.children[0].log("ERROR", "transfer submission failed: risk scoring deadline exceeded", transfer_id=tid,
                            **{"rpc.service": "vantra.money.risk.v1.RiskScoring", "timeout_ms": 4000})
    elif status == 503:
        srv.children[0].log("ERROR", "instant payment rail unavailable, transfer not submitted", transfer_id=tid,
                            rail="instant-payments-network")
    elif status == 422:
        srv.children[0].log("INFO", "transfer rejected: daily limit exceeded", transfer_id=tid)
    return bff(ctx, "POST", "/api/v1/transfers", "/api/v1/transfers", [srv], status=status), status, {"transfer_id": tid}


def pay_bill(ctx):
    srv = b.http(S["banking-experience-api"], S["bill-payment-service"], "POST", "/api/payments", "/api/payments", 201, (2, 6), [
        b.sql(S["bill-payment-service"], "INSERT", "scheduled_payments",
              "INSERT INTO scheduled_payments (customer_id, payee_id, amount_cents, send_on) VALUES (@c, @p, @a, @d)",
              "payments", RDS.format("payments-prod"), ms=(1.5, 5)),
        core_call(S["bill-payment-service"], "PlaceHold", ms=(120, 380))])
    return bff(ctx, "POST", "/api/v1/bill-payments", "/api/v1/bill-payments", [srv], status=201), 201, {}


def statement(ctx):
    slow = ctx.incident == "statements-slow"
    srv = b.http(S["banking-experience-api"], S["statement-rendering-service"], "GET", "/v1/statements/{id}/pdf",
                 "/v1/statements/2026-09/pdf", 200, (4, 12), [
                     b.external(S["statement-rendering-service"], "GET", "https://vantra-money-statements-prod.s3.us-east-1.amazonaws.com/2026/09/<id>.json",
                                "aws.s3", ms=(30, 90)),
                     b.internal(S["statement-rendering-service"], "pdf.render", ms=(2800, 6500) if slow else (600, 1700),
                                attrs={"pdf.pages": random.randint(2, 6), "pdf.engine": "weasyprint"})])
    return bff(ctx, "GET", "/api/v1/statements/:statementId/pdf", "/api/v1/statements/2026-09/pdf", [srv]), 200, {}


def lock_card(ctx):
    srv = b.http(S["banking-experience-api"], S["card-management-service"], "POST", "/api/cards/{cardId}/lock", "/api/cards/<id>/lock",
                 200, (2, 6), [b.external(S["card-management-service"], "POST", "https://api.cardplatform.io/v4/cards/<token>/status",
                                          "card-management-platform", ms=(180, 520))])
    return bff(ctx, "POST", "/api/v1/cards/:cardId/lock", "/api/v1/cards/4821/lock", [srv]), 200, {}


ENDPOINTS = {
    "login": Endpoint("POST", "/api/v1/auth/login", "/api/v1/auth/login", login,
                      outcomes=[("ok", 94), ("bad_password", 6)], size=(300, 700)),
    "mfa_verify": Endpoint("POST", "/api/v1/auth/mfa/verify", "/api/v1/auth/mfa/verify", mfa_verify, size=(900, 1600)),
    "accounts": Endpoint("GET", "/api/v1/accounts", "/api/v1/accounts", accounts, size=(2400, 6200)),
    "transactions": Endpoint("GET", "/api/v1/accounts/:accountId/transactions",
                             lambda p: f"/api/v1/accounts/{p['account']}/transactions?limit=50", transactions,
                             size=(12000, 34000)),
    "payees": Endpoint("GET", "/api/v1/payees", "/api/v1/payees", payees, size=(1500, 5200)),
    "create_transfer": Endpoint("POST", "/api/v1/transfers", "/api/v1/transfers", create_transfer,
                                outcomes=[("ok", 92), ("fraud_slow", 3), ("rail_unavailable", 2), ("limit_exceeded", 3)],
                                size=(500, 1200)),
    "pay_bill": Endpoint("POST", "/api/v1/bill-payments", "/api/v1/bill-payments", pay_bill, size=(400, 900)),
    "statement": Endpoint("GET", "/api/v1/statements/:statementId/pdf", "/api/v1/statements/2026-09/pdf", statement,
                          size=(180_000, 420_000)),
    "lock_card": Endpoint("POST", "/api/v1/cards/:cardId/lock", "/api/v1/cards/4821/lock", lock_card, size=(200, 500)),
}

BUNDLE = "/static/js/main.8f3c21d4.js"
PAGES = {
    "signin": Page("/login", "Sign in", bundle=BUNDLE, lcp_target="form#login h1", lcp_element="h1", doc_height=1200),
    "mfa": Page("/login/verify", "Verify it's you", bundle=BUNDLE, lcp_target="div.mfa-card h2", lcp_element="h2",
                doc_height=1000),
    "dashboard": Page("/accounts", "Accounts overview", bundle="/static/js/accounts.2b7e9a10.js",
                      lcp_target="section.balances", lcp_element="section", shifty=True,
                      shift_target="section.offers-carousel", doc_height=2600),
    "account": Page("/accounts/:accountId", lambda p: p["label"], lambda p: f"/accounts/{p['account']}",
                    bundle="/static/js/accounts.2b7e9a10.js", lcp_target="table.transactions", lcp_element="table",
                    doc_height=4800),
    "transfer": Page("/transfers/new", "Move money", bundle="/static/js/transfers.c41d07fe.js",
                     lcp_target="form.transfer h1", lcp_element="h1", doc_height=1700),
    "transfer_review": Page("/transfers/review", "Review transfer", bundle="/static/js/transfers.c41d07fe.js",
                            lcp_target="div.review-card", lcp_element="div", doc_height=1300),
    "transfer_done": Page("/transfers/:transferId/confirmation", "Transfer sent",
                          lambda p: f"/transfers/{p['transfer_id']}/confirmation", bundle="/static/js/transfers.c41d07fe.js",
                          lcp_target="div.confirmation h1", lcp_element="h1", doc_height=1100),
    "billpay": Page("/bill-pay", "Pay bills", bundle="/static/js/billpay.71aa3f2c.js", lcp_target="ul.payees",
                    lcp_element="ul", doc_height=2200),
    "statements": Page("/documents/statements", "Statements", bundle="/static/js/documents.0d9e44b1.js",
                       lcp_target="table.statements", lcp_element="table", doc_height=1800),
    "cards": Page("/cards", "Cards", bundle="/static/js/cards.5ef210a7.js", lcp_target="div.card-art img",
                  doc_height=1600),
}

STATIC = [
    {"path": BUNDLE, "size": 241_700, "initiator": "script", "ratio": 3.3},
    {"path": "/static/js/vendor.4a1f99e2.js", "size": 188_300, "initiator": "script", "ratio": 3.0},
    {"path": "/static/css/main.6c02bd18.css", "size": 52_900, "initiator": "link", "blocking": "blocking", "ratio": 5.8},
    {"path": "/static/fonts/source-sans-3-latin.woff2", "size": 41_600, "initiator": "css"},
    {"path": "/static/media/logo-corvia.9d2c1f.svg", "size": 6_800, "initiator": "img", "ratio": 2.4},
]

ACCOUNTS = [("chk-2231", "Everyday Checking ••4821"), ("sav-7710", "High Yield Savings ••0937"),
            ("crd-5512", "Cash Rewards Visa ••5512")]


def make_user(p):
    p["user_id"] = uuid4()
    p["traits"] = {"segment": random.choice(["mass", "mass", "mass", "affluent", "private"]),
                   "mfa": random.choice(["push", "push", "sms", "totp"])}


def properties(p, rng):
    return {"customer.segment": p["traits"]["segment"], "mfa.method": p["traits"]["mfa"], "locale": "en-US"}


def _sign_in(s):
    s.page_view("signin")
    s.wait(1, 4)
    s.type_text(random.randint(18, 30))
    s.click('<button class="btn btn-primary btn-lg" type="submit" id="sign-in">Sign in</button>', "#sign-in")
    with s.flow("sign_in", **{"auth.method": "password"}) as f:
        r = s.api("login")
        if not r.ok:
            f["failed"] = True
            s.log("warning", "Sign-in failed: invalid credentials", **{"http.status_code": "401"})
            s.wait(3, 8)
            s.type_text(random.randint(8, 14))
            s.click('<button class="btn btn-primary btn-lg" type="submit" id="sign-in">Sign in</button>', "#sign-in")
            r = s.api("login")
    if not r.ok:
        return False
    s.page_view("mfa")
    s.wait(4, 14)
    s.click('<button class="btn btn-primary" data-testid="mfa-approved">I approved the request</button>')
    s.api("mfa_verify")
    s.login()
    s.breadcrumb("Signed in with MFA")
    s.log("info", "Journey started: authenticated_session", **{"journey.name": "authenticated_session",
                                                              "mfa.method": s.persona["traits"]["mfa"]})
    s.page_view("dashboard")
    with s.flow("load_accounts"):
        s.api("accounts")
    s.wait(3, 12)
    return True


def _end(s):
    if random.random() < 0.55:
        s.click('<button class="nav-signout">Sign out</button>', "button.nav-signout", breadcrumb="Signed out")
        s.logout()
    else:
        s.leave()


def check_balance(s):
    if not _sign_in(s):
        return s.leave()
    acct, label = random.choice(ACCOUNTS)
    s.click(f'<a class="account-tile" href="/accounts/{acct}">{label}</a>', "a.account-tile")
    s.page_view("account", account=acct, label=label)
    s.api("transactions", account=acct)
    s.wait(8, 40)
    _end(s)


def transfer(s):
    if not _sign_in(s):
        return s.leave()
    s.click('<a class="btn btn-secondary" href="/transfers/new">Move money</a>', "a[href=/transfers/new]")
    s.page_view("transfer")
    s.api("payees")
    payee = random.choice(PAYEES)
    s.wait(2, 6)
    s.click(f'<li class="payee-option" role="option">{payee}</li>', breadcrumb=f"Selected payee: {payee}")
    s.type_text(random.randint(3, 7))
    s.click('<button class="btn btn-primary" type="submit">Review transfer</button>')
    s.page_view("transfer_review")
    s.wait(3, 9)
    s.click('<button class="btn btn-primary" data-testid="send-transfer">Send now</button>',
            "button[data-testid=send-transfer]")
    with s.flow("submit_transfer", **{"transfer.rail": "instant"}) as f:
        r = s.api("create_transfer")
        if not r.ok:
            f["failed"] = True
    if r.ok:
        s.breadcrumb(f"Transfer submitted: {r.data['transfer_id']}")
        s.log("info", "Journey completed: transfer", **{"journey.name": "transfer", "journey.outcome": "completed"})
        s.page_view("transfer_done", transfer_id=r.data["transfer_id"])
        s.wait(3, 10)
    elif r.status == 422:
        s.log("warning", "Transfer blocked: daily limit reached", **{"http.status_code": "422"})
        s.wait(5, 15)
    else:
        s.log("error", "Transfer failed", **{"http.status_code": str(r.status), "journey.name": "transfer",
                                             "journey.outcome": "failed", "transfer.id": r.data["transfer_id"]})
        if random.random() < 0.5:
            s.wait(2, 5)
            s.click('<button class="btn btn-primary" data-testid="send-transfer">Send now</button>')
            r2 = s.api("create_transfer")
            if r2.ok:
                s.page_view("transfer_done", transfer_id=r2.data["transfer_id"])
        s.wait(4, 12)
    _end(s)


def bills(s):
    if not _sign_in(s):
        return s.leave()
    s.click('<a class="nav-link" href="/bill-pay">Pay bills</a>')
    s.page_view("billpay")
    s.api("payees")
    s.wait(4, 12)
    payee = random.choice(PAYEES[:5])
    s.click(f'<button class="btn btn-sm btn-outline-primary">Pay {payee}</button>', breadcrumb=f"Paying bill: {payee}")
    s.type_text(random.randint(3, 6))
    s.click('<button class="btn btn-primary">Schedule payment</button>')
    s.api("pay_bill")
    s.wait(2, 8)
    _end(s)


def statements(s):
    if not _sign_in(s):
        return s.leave()
    s.click('<a class="nav-link" href="/documents/statements">Statements &amp; documents</a>')
    s.page_view("statements")
    s.wait(2, 6)
    s.click('<a class="statement-link" href="#">September 2026 statement (PDF)</a>')
    with s.flow("download_statement", **{"statement.period": "2026-09"}):
        s.api("statement")
    s.wait(5, 20)
    _end(s)


def card_lock(s):
    if not _sign_in(s):
        return s.leave()
    s.click('<a class="nav-link" href="/cards">Cards</a>')
    s.page_view("cards")
    s.wait(2, 7)
    s.click('<button class="toggle" role="switch" aria-checked="false">Lock card</button>', breadcrumb="Card locked")
    s.api("lock_card")
    s.wait(3, 9)
    _end(s)


PROFILE = Profile(
    key="banking", brand="Corvia Bank", namespace="vantra-money",
    web_origin="https://online.corviabank.com", api_host="api.corviabank.com", cdn="https://static.corviabank.com",
    main_bundle=BUNDLE, web_service="corvia-online-banking-web", edge_service="api-gateway", services=S,
    pages=PAGES, endpoints=ENDPOINTS, static_assets=STATIC,
    app_versions=[("26.43.1", "2.31.0", 70), ("26.42.0", "2.31.0", 24), ("26.39.2", "2.30.0", 6)],
    make_user=make_user, properties=properties,
    journeys=[(check_balance, 42), (transfer, 22), (bills, 14), (statements, 12), (card_lock, 10)],
    incidents={
        "fraud-latency": {"create_transfer": {"fraud_slow": 40}},
        "rails-outage": {"create_transfer": {"rail_unavailable": 45}},
        "core-banking-latency": {},
        "statements-slow": {},
    },
)

PROFILE.android = dict(
    ui="views",
    package="com.corviabank.mobile", app_name="Corvia", activity="com.corviabank.mobile.ui.HostActivity",
    versions=[("26.43.1", "2643100", "9.3.1", 70), ("26.42.0", "2642000", "9.3.1", 24), ("26.39.2", "2639200", "9.3.0", 6)],
    screens={"signin": "LoginFragment", "mfa": "VerifyIdentityFragment", "dashboard": "AccountsOverviewFragment",
             "account": "AccountDetailFragment", "transfer": "MoveMoneyFragment", "transfer_review": "ReviewTransferFragment",
             "transfer_done": "TransferConfirmationFragment", "billpay": "BillPayFragment",
             "statements": "StatementsFragment", "cards": "CardsFragment"},
    user_agent="Corvia/{version} (Android {os}; {model}) okhttp/4.12.0",
    business_unit="banking", shared_api_host="api.vantramoney.com",
)
