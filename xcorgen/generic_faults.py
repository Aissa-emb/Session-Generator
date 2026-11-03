"""Backend traces for spec-defined HTTP errors on any endpoint.

A spec's http_error fault names an endpoint and a status. The trace keeps the
real shape of the request (gateway, experience API, owning domain service)
and fails at the owning service with the exception that service's language
and libraries would raise for that status. 4xx responses carry no exception
event, matching OpenTelemetry server-span status rules.
"""
import random

from . import backend as b
from .profiles import platform

EXPERIENCE = {"ecommerce": "shop-experience-api", "banking": "banking-experience-api",
              "travel": "travel-experience-api"}
ENDPOINT_SERVICE = {
    "ecommerce": {"home": "personalization-service", "list_products": "product-catalog-service",
                  "search": "product-search-service", "get_product": "product-catalog-service",
                  "get_recommendations": "personalization-service", "add_to_cart": "shopping-cart-service",
                  "get_cart": "shopping-cart-service", "login": "customer-identity-service",
                  "shipping_options": "shipping-rates-service", "place_order": "checkout-orchestrator",
                  "get_order": "order-management-service", "operation_status": "checkout-orchestrator"},
    "banking": {"login": "strong-auth-service", "mfa_verify": "strong-auth-service",
                "accounts": "account-summary-service", "transactions": "transaction-history-service",
                "payees": "payee-management-service", "create_transfer": "funds-transfer-service",
                "pay_bill": "bill-payment-service", "statement": "statement-rendering-service",
                "lock_card": "card-management-service", "operation_status": "funds-transfer-service"},
    "travel": {"locations": "geo-location-service", "flight_search": "air-shopping-service",
               "offer": "fare-pricing-service", "hotel_search": "lodging-search-service",
               "hotel_revalidate": "lodging-search-service", "create_booking": "reservation-service",
               "trips": "trip-itinerary-service", "operation_status": "reservation-service"},
}

# (exception type, message) per language for the 5xx families.
FAILURES = {
    "internal": {
        "java": ("java.lang.IllegalStateException", "Required field 'currency' was missing from the upstream response"),
        "go": ("runtime.Error", "runtime error: invalid memory address or nil pointer dereference"),
        "python": ("KeyError", "'currency'"),
        "nodejs": ("TypeError", "Cannot read properties of undefined (reading 'currency')"),
        "dotnet": ("System.NullReferenceException", "Object reference not set to an instance of an object."),
    },
    "unavailable": {
        "java": ("io.github.resilience4j.circuitbreaker.CallNotPermittedException",
                 "CircuitBreaker 'downstream' is OPEN and does not permit further calls"),
        "go": ("gobreaker.ErrOpenState", "circuit breaker is open"),
        "python": ("pybreaker.CircuitBreakerError", "Failures threshold reached, circuit breaker opened"),
        "nodejs": ("Error", "Breaker is open"),
        "dotnet": ("Polly.CircuitBreaker.BrokenCircuitException", "The circuit is now open and is not allowing calls."),
    },
    "timeout": {
        "java": ("java.net.http.HttpTimeoutException", "request timed out"),
        "go": ("context.deadlineExceededError", "context deadline exceeded"),
        "python": ("httpx.ReadTimeout", "timed out"),
        "nodejs": ("TimeoutError", "The operation was aborted due to timeout"),
        "dotnet": ("System.Threading.Tasks.TaskCanceledException",
                   "The request was canceled due to the configured HttpClient.Timeout of 5 seconds elapsing."),
    },
}


def _family(status):
    return "timeout" if status == 504 else "unavailable" if status in (502, 503) else "internal"


def _frames(svc, method):
    words = [w.title() for w in svc.name.replace("-service", "").replace("-orchestrator", "").replace("-gateway", "").split("-")]
    cls = "".join(words)
    if svc.lang == "java":
        pkg = svc.app_scope
        return [(f"{pkg}.service.{cls}Service.{method}", f"{cls}Service.java", random.randint(60, 240)),
                (f"{pkg}.api.{cls}Controller.{method}", f"{cls}Controller.java", random.randint(30, 120)),
                ("org.springframework.web.servlet.FrameworkServlet.service", "FrameworkServlet.java", 885)]
    if svc.lang == "go":
        mod = svc.name
        return [(f"github.com/vantra/{mod}/internal/service.(*Service).{method.title()}", f"/app/internal/service/{method}.go",
                 random.randint(40, 180)),
                (f"github.com/vantra/{mod}/internal/api.(*Handler).{method.title()}", "/app/internal/api/handler.go",
                 random.randint(30, 140))]
    if svc.lang == "python":
        mod = svc.name.replace("-", "_")
        return [(method, f"/app/{mod}/service.py", random.randint(40, 200)),
                (f"{method}_endpoint", f"/app/{mod}/api/routes.py", random.randint(20, 90))]
    if svc.lang == "dotnet":
        ns = "Vantra." + "".join(words)
        return [(f"{ns}.Services.{cls}Service.{method.title()}Async()", f"/src/{ns}/Services/{cls}Service.cs", random.randint(40, 160)),
                (f"{ns}.Api.Controllers.{cls}Controller.{method.title()}()", f"/src/{ns}/Api/Controllers/{cls}Controller.cs",
                 random.randint(20, 80))]
    return [(f"{cls}Service.{method}", f"/app/dist/services/{svc.name}.js", random.randint(40, 300)),
            (f"{cls}Controller.{method}", f"/app/dist/controllers/{svc.name}.js", random.randint(20, 120))]


def http_error(ctx, ep, key, spec):
    """(root Node, status, data) for an endpoint that fails with spec["status"]."""
    profile = ctx.session.profile.key
    status = int(spec["status"])
    exp = ctx.svc[EXPERIENCE[profile]]
    svc = ctx.svc[spec.get("service") or ENDPOINT_SERVICE[profile].get(key, EXPERIENCE[profile])]
    path = ep.path(ctx.params)
    method = key.split("_")[0] if key.split("_")[0] in ("get", "list", "create", "lock", "place") else "handle"
    error = None
    if status >= 500:
        exc_type, message = FAILURES[_family(status)][svc.lang]
        error = b.err(message, exc_type, stack=b.stack(svc.lang, exc_type, message, _frames(svc, method)),
                      error_type=exc_type)
    internal_route = "/v1" + ep.route.split("/api/v1", 1)[-1].split("/api/v2", 1)[-1].split("/api/v3", 1)[-1]
    if svc is exp:
        work = b.internal(exp, f"{ep.method} handler", ms=(2, 12), error=error)
        if error:
            work.log("ERROR", f"request failed: {error['exc_message']}", **{"http.route": ep.route})
        children = [work]
    else:
        inner = b.http(exp, svc, ep.method, internal_route, internal_route, status, (3, 18),
                       [b.internal(svc, f"{svc.name.replace('-', '_')}.{method}", ms=(1, 6), error=error)])
        if error:
            inner.children[0].log("ERROR", f"{ep.method} {internal_route} failed: {error['exc_message']}")
        children = [inner]
    root = platform.gateway(ctx, exp, ep.method, ep.route, path, children, status=status)
    return root, status, {}
