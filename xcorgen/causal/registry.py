"""Canonical simulated processes, owned routes, and scenario vocabulary (v1)."""
from dataclasses import dataclass

VERSION = "1.0.0"
SCHEMA_URL = "https://opentelemetry.io/schemas/1.37.0"

@dataclass(frozen=True)
class Service:
    domain: str
    responsibility: str
    version: str = "2026.10.1"

SERVICES = {
    "vantra-edge": Service("shared", "Public ingress and local token verification"),
    "identity-sessions": Service("shared", "Credentials, sessions and authentication persistence"),
    "account-overview-api": Service("banking", "Parallel profile and account composition"),
    "customer-directory": Service("banking", "Customer profile read path"),
    "account-portfolio": Service("banking", "Account inventory and balance projections"),
    "transaction-history": Service("banking", "History, pagination and detail contracts"),
    "transfer-orchestrator": Service("banking", "Intent, idempotency, outbox and reconciliation"),
    "transfer-outbox-relay": Service("banking", "Committed outbox publication"),
    "ledger-posting-worker": Service("banking", "Atomic balanced internal ledger postings"),
    "travel-search-api": Service("travel", "Availability, quote, merge and rank"),
    "destination-catalog": Service("travel", "Destination autocomplete"),
    "inventory-availability": Service("travel", "Inventory and supplier adapters"),
    "rate-quote": Service("travel", "Quote validity, taxes and fees"),
    "reservation-orchestrator": Service("travel", "Reservation intents and confirmation"),
}
# Public paths deliberately retain the existing estate's versioning.
ROUTES = {
    "signin": ("POST", "/api/v1/auth/login", "identity-sessions", "/internal/v1/sessions"),
    "overview": ("GET", "/api/v1/accounts", "account-overview-api", "/internal/v1/account-overviews"),
    "history": ("GET", "/api/v1/accounts/{accountId}/transactions", "transaction-history", "/internal/v1/accounts/{accountId}/transactions"),
    "detail": ("GET", "/api/v1/transactions/{transactionId}", "transaction-history", "/internal/v1/transactions/{transactionId}"),
    "transfer": ("POST", "/api/v1/transfers", "transfer-orchestrator", "/internal/v1/transfers"),
    "status": ("GET", "/api/v1/operations/{operationId}", "transfer-orchestrator", "/internal/v1/operations/{operationId}"),
    "autocomplete": ("GET", "/api/v3/locations", "destination-catalog", "vantra.travel.catalog.v1.DestinationService/Autocomplete"),
    "search": ("POST", "/api/v3/hotels/search", "travel-search-api", "/internal/v1/stay-searches"),
    "reservation": ("POST", "/api/v3/bookings", "reservation-orchestrator", "/internal/v1/reservations"),
}
SCENARIOS = {
    "healthy": ("Healthy account review", "banking", "account_review", "overview"),
    "signin-outage": ("Sign-in pool timeout", "banking", "sign_in", "signin"),
    "slow-history": ("Slow transaction history", "banking", "account_review", "history"),
    "contract-regression": ("Transaction detail contract", "banking", "transaction_detail", "detail"),
    "transfer-response-loss": ("Transfer response lost", "banking", "transfer", "transfer"),
    "worker-backlog": ("Transfer worker backlog", "banking", "transfer", "transfer"),
    "frontend-regression": ("Client view-state regression", "banking", "transfer", None),
    "connectivity": ("Connectivity transition", "banking", "account_review", "history"),
    "travel-provider": ("Travel provider degradation", "travel", "travel_search", "search"),
}
# Server boundaries are owned here, independently of the builders that emit them.
HTTP_OWNERS = {path: owner for _, _, owner, path in ROUTES.values() if path.startswith('/')}
HTTP_OWNERS.update({path: 'vantra-edge' for _, path, _, _ in ROUTES.values()})
HTTP_OWNERS['/internal/v1/customers/{customerId}/accounts'] = 'account-portfolio'
RPC_OWNERS = {
    ('vantra.customer.v1.DirectoryService', 'GetProfile'): 'customer-directory',
    ('vantra.travel.catalog.v1.DestinationService', 'Autocomplete'): 'destination-catalog',
    ('vantra.travel.inventory.v1.AvailabilityService', 'Search'): 'inventory-availability',
    ('vantra.travel.pricing.v1.QuoteService', 'GetQuote'): 'rate-quote',
}
PROVENANCE = frozenset({"demo.synthetic", "demo.run_id", "demo.scenario_id", "demo.scenario_version",
    "demo.journey", "demo.variant", "demo.client_platform", "demo.client_build", "demo.operation_id",
    "demo.feature.detail_optional", "demo.feature.transfer_view", "demo.cache.name", "demo.cache.outcome",
    "demo.redis.script", "demo.network.profile", "demo.cohort.region", "demo.cohort.release"})

def listing():
    return [{"id": key, "name": value[0], "domain": value[1], "journey": value[2], "version": VERSION}
            for key, value in SCENARIOS.items()]
