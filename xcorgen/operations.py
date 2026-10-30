"""Explicit reconciliation and rate revalidation endpoints."""
from . import backend as b
from .profiles import Endpoint
from .scenarios import current

ROUTES = {"ecommerce": ("shop-experience-api", "checkout-orchestrator", "/api/v2/checkout/status"),
          "banking": ("banking-experience-api", "funds-transfer-service", "/api/v1/transfers/status"),
          "travel": ("travel-experience-api", "reservation-service", "/api/v3/bookings/status")}


def replay(ctx, record, endpoint):
    """A repeated business operation reads the stored result, never resubmits it."""
    experience, domain, _ = ROUTES[ctx.session.profile.key]
    code = 201 if record["state"] == "confirmed" else 409
    cache = b.redis(ctx.svc[domain], "GET", "idempotency:operation", "operation-state.platform.vantra.internal")
    cache.attrs.update({"cache.hit": True, "operation.state": record["state"], "idempotency.replayed": True})
    node = b.http(ctx.svc[experience], ctx.svc[domain], endpoint.method, endpoint.route, endpoint.path(ctx.params),
                  code, (1, 3), [cache])
    from .profiles.platform import gateway
    return gateway(ctx, ctx.svc[experience], endpoint.method, endpoint.route, endpoint.path(ctx.params), [node],
                   status=code), code, dict(record)


def status(ctx):
    req = current()
    plan = req["plan"]
    experience, domain, route = ROUTES[ctx.session.profile.key]
    data = dict(plan.runtime.operations.get(plan.operation_id, {"state": "not_submitted"}))
    if data.get("ready_at") and ctx.session.t >= data["ready_at"]:
        data["state"] = "confirmed"
        plan.runtime.operations[plan.operation_id] = dict(data)
    svc = ctx.svc[domain]
    cache = b.redis(svc, "GET", "idempotency:operation", "operation-state.platform.vantra.internal")
    cache.attrs.update({"cache.hit": True, "operation.state": data["state"], "idempotency.operation_id": plan.operation_id})
    children = [cache]
    if data["state"] == "pending":
        supplier = "https://ws.globaldistribution.net/pnr/v3/status" if ctx.session.profile.key == "travel" else "https://api.paycore-acquiring.com/v2/payments/status"
        children.append(b.external(svc, "GET", supplier, "reservation-reconciliation" if ctx.session.profile.key == "travel" else "card-acquirer",
                                   status=202, ms=(80, 240)))
    operation = b.http(ctx.svc[experience], svc, "GET", "/v1/operations/{id}/status", "/v1/operations/current/status",
                       200, (1, 4), children, server_extra={"operation.state": data["state"]})
    from .profiles.platform import gateway
    return gateway(ctx, ctx.svc[experience], "GET", route, route, [operation]), 200, data


def hotel_revalidate(ctx):
    svc = ctx.svc["lodging-search-service"]
    node = b.http(ctx.svc["travel-experience-api"], svc, "POST", "/v1/rates/revalidate", "/v1/rates/revalidate", 200,
                  (2, 5), [b.external(svc, "POST", "https://api.staysupply.com/v2/rates/revalidate", "hotel-supplier",
                                      ms=(180, 450))], server_extra={"rates.indicative": False, "rates.revalidated": True})
    from .profiles.platform import gateway
    return gateway(ctx, ctx.svc["travel-experience-api"], "POST", "/api/v3/hotels/rates/revalidate",
                   "/api/v3/hotels/rates/revalidate", [node]), 200, {"revalidated": True}


def install(profile):
    route = ROUTES[profile.key][2]
    profile.endpoints["operation_status"] = Endpoint("GET", route, route, status, size=(250, 650))
    if profile.key == "travel":
        route = "/api/v3/hotels/rates/revalidate"
        profile.endpoints["hotel_revalidate"] = Endpoint("POST", route, route, hotel_revalidate, size=(500, 1400))
