"""Northline Goods: an outdoor and apparel retailer's storefront."""

import random

from .. import backend as b
from ..util import lat, uuid4
from ..scenarios import current, cache_read
from . import Endpoint, Page, Profile, platform, services

S = services("vantra-shop", "prod-use1-shop", [
    ("shop-experience-api", "nodejs", "8.41.0", "storefront", 8),
    ("product-catalog-service", "java", "5.22.1", "catalog", 4),
    ("product-search-service", "go", "4.3.0", "search", 4),
    ("price-promotion-service", "go", "2.31.4", "pricing", 3),
    ("inventory-availability-service", "java", "3.17.0", "inventory", 3),
    ("product-reviews-service", "python", "1.12.6", "reviews", 2),
    ("personalization-service", "python", "6.2.0", "personalization", 3),
    ("shopping-cart-service", "dotnet", "4.5.2", "cart", 4),
    ("checkout-orchestrator", "go", "7.8.1", "checkout", 4),
    ("shipping-rates-service", "go", "2.6.5", "fulfillment", 2),
    ("order-management-service", "java", "6.11.0", "orders", 3),
    ("warehouse-fulfillment-worker", "java", "3.4.2", "fulfillment", 2),
    ("tax-calculation-service", "java", "2.12.0", "pricing", 2),
])
S.update(platform.P)

PRODUCTS = [
    ("1048213", "Merino Crew Sweater", "knitwear", 98.00), ("1051027", "Alpine Down Jacket", "outerwear", 279.00),
    ("1033519", "Trailhead Hiking Boot", "footwear", 164.00), ("1020984", "Ridge Fleece Half-Zip", "outerwear", 89.00),
    ("1067340", "Everyday Canvas Tote", "bags", 48.00), ("1071155", "Waxed Field Jacket", "outerwear", 245.00),
    ("1012876", "Organic Cotton Tee", "tops", 32.00), ("1090412", "Wool Blend Beanie", "accessories", 29.00),
    ("1055601", "Stormline Rain Shell", "outerwear", 189.00), ("1038842", "Summit 28L Daypack", "bags", 129.00),
    ("1044178", "Chino Straight Fit", "bottoms", 78.00), ("1062239", "Cabin Flannel Shirt", "tops", 69.00),
    ("1017763", "Insulated Water Bottle", "accessories", 36.00), ("1080325", "Trail Running Shoe", "footwear", 139.00),
]
CATEGORIES = {"outerwear": "Outerwear", "footwear": "Footwear", "knitwear": "Knitwear", "tops": "Tops", "bags": "Bags",
              "accessories": "Accessories", "bottoms": "Bottoms"}
SEARCHES = ["rain jacket", "merino", "hiking boots", "fleece", "daypack", "beanie", "down jacket", "flannel"]

RDS = "{}.cluster-c8h2kq1x9z0p.us-east-1.rds.amazonaws.com"
REDIS = "{}.x7mq2a.ng.0001.use1.cache.amazonaws.com"


def _product(sku):
    return next(p for p in PRODUCTS if p[0] == sku)


# Routes where the experience API personalizes the response with the customer's profile.
PROFILE_ROUTES = {'/api/v2/cart', '/api/v2/products/:sku', '/api/v2/checkout/orders', '/api/v2/home'}


def bff(ctx, method, route, path, children, status=200, parallel=False, error=None, ms=(3, 12)):
    """api-gateway -> shop-experience-api: token check and rate limit at the edge, then the experience API."""
    return platform.gateway(ctx, S["shop-experience-api"], method, route, path, children, status, parallel, error, ms,
                            with_profile=route in PROFILE_ROUTES)


def catalog_lookup(caller, sku):
    hit = cache_read(S["product-catalog-service"], "product", sku, initial_hit=.82)
    cache = b.redis(S["product-catalog-service"], "GET", f"product:{sku}", REDIS.format("catalog-cache"))
    cache.attrs["cache.hit"] = hit
    if current() is None and random.random() < 0.012:
        # Cache read times out; the service falls back to the database and the request still succeeds.
        hit = False
        cache.ms, cache.min_ms = (1000, 1010), 1000
        cache.error = b.err("Command timed out after 1 second(s)", "io.lettuce.core.RedisCommandTimeoutException",
                            stack=b.stack("java", "io.lettuce.core.RedisCommandTimeoutException", "Command timed out after 1 second(s)", [
                                ("io.lettuce.core.internal.ExceptionFactory.createTimeoutException", "ExceptionFactory.java", 59),
                                ("io.lettuce.core.LettuceFutures.awaitOrCancel", "LettuceFutures.java", 129),
                                ("com.vantra.shop.catalog.cache.ProductCache.get", "ProductCache.java", 44)]))
        cache.log("WARN", "product cache read timed out, falling back to database", sku=sku)
    db = None if hit else b.sql(S["product-catalog-service"], "SELECT", "products",
                                "SELECT p.sku, p.name, p.description, p.attributes, m.url FROM products p "
                                "LEFT JOIN product_media m ON m.sku = p.sku WHERE p.sku = $1",
                                "catalog", RDS.format("catalog-prod"), ms=(2.5, 9), rows=1)
    fill = None if hit else b.redis(S["product-catalog-service"], "SETEX", f"product:{sku}", REDIS.format("catalog-cache"))
    return b.grpc(caller, S["product-catalog-service"], "vantra.shop.catalog.v1.CatalogService", "GetProduct",
                  ms=(1.2, 4), children=[cache, db, fill])


def price_lookup(caller, skus):
    return b.grpc(caller, S["price-promotion-service"], "vantra.shop.pricing.v1.PricingService", "GetPrices", ms=(0.8, 3),
                  children=[b.redis(S["price-promotion-service"], "MGET", " ".join(f"price:{s}" for s in skus[:3]),
                                    REDIS.format("pricing-cache"))])


def get_product(ctx):
    sku = ctx.params["sku"]
    n_plus_one = ctx.incident == "reviews-n-plus-one" or (current() is None and random.random() < 0.15)
    review_count = random.randint(14, 20)
    review_queries = [b.sql(S["product-reviews-service"], "SELECT", "review_media",
                            "SELECT url, kind FROM review_media WHERE review_id = %s", "reviews",
                            RDS.format("reviews-prod"), ms=(110, 180) if n_plus_one else (0.8, 2.5), rows=random.randint(0, 3))
                      for _ in range(review_count if n_plus_one else 0)]
    reviews = b.http(S["shop-experience-api"], S["product-reviews-service"], "GET", "/v1/products/{sku}/reviews/summary",
                     f"/v1/products/{sku}/reviews/summary", 200, (4, 14), [
                         b.sql(S["product-reviews-service"], "SELECT", "reviews",
                               "SELECT id, rating, title, body, created_at FROM reviews WHERE sku = %s "
                               "ORDER BY helpful_votes DESC LIMIT 20", "reviews", RDS.format("reviews-prod"),
                               ms=(3, 11), rows=review_count)] + review_queries)
    reviews.children[0].attrs["reviews.loaded"] = review_count
    reviews.children[0].attrs["reviews.media_query_count"] = len(review_queries)
    inventory = b.grpc(S["shop-experience-api"], S["inventory-availability-service"], "vantra.shop.inventory.v1.InventoryService",
                       "GetAvailability", ms=(1, 4), children=[
                           b.sql(S["inventory-availability-service"], "SELECT", "stock_levels",
                                 "SELECT location_id, on_hand, reserved FROM stock_levels WHERE sku = ? AND on_hand > 0",
                                 "inventory", RDS.format("inventory-prod"), ms=(1.5, 5), rows=random.randint(2, 9))])
    root = bff(ctx, "GET", "/api/v2/products/:sku", f"/api/v2/products/{sku}", [
        catalog_lookup(S["shop-experience-api"], sku), price_lookup(S["shop-experience-api"], [sku]), inventory, reviews],
        parallel=True)
    return root, 200, {}


def get_recommendations(ctx):
    sku = ctx.params.get("sku", "home")
    fallback = ctx.outcome == "fallback"
    slow = ctx.outcome == "slow_model" or fallback or ctx.incident == "recs-latency"
    duration = random.uniform(1000, 3000) if slow else random.uniform(18, 60)
    model = b.internal(S["personalization-service"], "ranker.predict", ms=(duration, duration),
                       attrs={"model.name": "two-tower-ranker", "model.version": "2026.10.14", "candidates": 400})
    features = b.redis(S["personalization-service"], "HMGET", f"user_features:{ctx.user_id or 'anon'}",
                       REDIS.format("feature-store"), ms=(0.4, 1.8))
    recs = b.http(S["shop-experience-api"], S["personalization-service"], "GET", "/v1/recommendations",
                  f"/v1/recommendations?context={sku}&limit=12", 504 if fallback else 200, (3, 9), [features, model],
                  timeout_ms=800 if fallback else None,
                  client_error=b.err("recommendation deadline exceeded", "TimeoutError", error_type="timeout") if fallback else None)
    if slow:
        recs.children[0].log("WARN", "ranker inference exceeded latency budget", **{
            "model.name": "two-tower-ranker", "latency_budget_ms": 250, "gpu.pool": "inference-g5"})
    batch = b.grpc(S["shop-experience-api"], S["product-catalog-service"], "vantra.shop.catalog.v1.CatalogService",
                   "BatchGetProducts", ms=(2, 7), children=[
                       b.redis(S["product-catalog-service"], "MGET", "product:1048213 product:1051027 ...", REDIS.format("catalog-cache"))])
    steps = [recs]
    if fallback:
        cached = b.redis(S["shop-experience-api"], "GET", "recommendations:popular:US", REDIS.format("catalog-cache"))
        cached.attrs.update({"cache.hit": True, "cache.age_ms": 120000, "fallback.source": "popular_products"})
        steps.append(cached)
    steps.append(batch)
    root = bff(ctx, "GET", "/api/v2/recommendations", f"/api/v2/recommendations?sku={sku}", steps)
    root.attrs.update({"recommendations.personalized": not fallback, "fallback.used": fallback})
    return root, 200, {"fallback": fallback, "source": "popular_products" if fallback else "personalized"}


def list_products(ctx):
    cat = ctx.params["category"]
    es = b.nosql(S["product-search-service"], "elasticsearch", "search", "products-v7", ms=(6, 22),
                 host="vpc-catalog-search-7kq2m.us-east-1.es.amazonaws.com",
                 statement='{"query":{"bool":{"filter":[{"term":{"category":"?"}},{"term":{"in_stock":true}}]}},"size":48}')
    search = b.grpc(S["shop-experience-api"], S["product-search-service"], "vantra.shop.search.v1.SearchService", "Browse",
                    ms=(1, 4), children=[es])
    root = bff(ctx, "GET", "/api/v2/products", f"/api/v2/products?category={cat}&page=1&sort=featured",
               [search, price_lookup(S["shop-experience-api"], [p[0] for p in PRODUCTS])])
    return root, 200, {}


def search(ctx):
    q = ctx.params["q"]
    spell = b.internal(S["product-search-service"], "query.spellcheck", ms=(0.6, 2))
    es = b.nosql(S["product-search-service"], "elasticsearch", "search", "products-v7", ms=(9, 35),
                 host="vpc-catalog-search-7kq2m.us-east-1.es.amazonaws.com",
                 statement='{"query":{"multi_match":{"query":"?","fields":["name^3","description","tags"]}},"size":24}')
    s = b.grpc(S["shop-experience-api"], S["product-search-service"], "vantra.shop.search.v1.SearchService", "Search",
               ms=(1, 3), children=[spell, es])
    root = bff(ctx, "GET", "/api/v2/search", f"/api/v2/search?q={q.replace(' ', '+')}", [s])
    return root, 200, {}


def add_to_cart(ctx):
    sku = ctx.params["sku"]
    oos = ctx.outcome == "out_of_stock"
    check = b.grpc(S["shopping-cart-service"], S["inventory-availability-service"], "vantra.shop.inventory.v1.InventoryService",
                   "CheckAvailability", code=9 if oos else 0, ms=(1, 3.5), children=[
                       b.sql(S["inventory-availability-service"], "SELECT", "stock_levels",
                             "SELECT sum(on_hand - reserved) FROM stock_levels WHERE sku = ?", "inventory",
                             RDS.format("inventory-prod"), ms=(1.2, 4), rows=1)])
    writes = [] if oos else [
        b.redis(S["shopping-cart-service"], "HSET", f"cart:{ctx.persona['device_id'][:12].lower()}", REDIS.format("cart-store")),
        b.redis(S["shopping-cart-service"], "EXPIRE", f"cart:{ctx.persona['device_id'][:12].lower()}", REDIS.format("cart-store"))]
    status = 409 if oos else 201
    cart = b.http(S["shop-experience-api"], S["shopping-cart-service"], "POST", "/api/carts/{cartId}/items",
                  f"/api/carts/{ctx.persona['device_id'][:12].lower()}/items", status, (1.5, 5), [
                      b.redis(S["shopping-cart-service"], "HGETALL", f"cart:{ctx.persona['device_id'][:12].lower()}",
                              REDIS.format("cart-store")), check] + writes)
    root = bff(ctx, "POST", "/api/v2/cart/items", "/api/v2/cart/items", [cart], status=status)
    if oos:
        cart.children[0].log("WARN", f"add to cart rejected: sku {sku} out of stock", sku=sku, reason="insufficient_stock")
    return root, status, {}


def get_cart(ctx):
    key = f"cart:{ctx.persona['device_id'][:12].lower()}"
    cart = b.http(S["shop-experience-api"], S["shopping-cart-service"], "GET", "/api/carts/{cartId}", f"/api/carts/{key[5:]}",
                  200, (1, 4), [b.redis(S["shopping-cart-service"], "HGETALL", key, REDIS.format("cart-store"))])
    quote = b.grpc(S["shop-experience-api"], S["price-promotion-service"], "vantra.shop.pricing.v1.PricingService", "QuoteCart",
                   ms=(1.5, 6), children=[b.internal(S["price-promotion-service"], "promotions.evaluate", ms=(0.8, 4))])
    return bff(ctx, "GET", "/api/v2/cart", "/api/v2/cart", [cart, quote]), 200, {}


def login(ctx):
    ident = b.http(S["shop-experience-api"], S["customer-identity-service"], "POST", "/v1/sessions", "/v1/sessions", 201, (2, 6), [
        b.sql(S["customer-identity-service"], "SELECT", "customers",
              "SELECT id, email, password_hash, status FROM customers WHERE email = $1", "identity",
              RDS.format("identity-prod"), ms=(1.2, 4), rows=1),
        b.internal(S["customer-identity-service"], "bcrypt.CompareHashAndPassword", ms=(55, 80)),
        b.redis(S["customer-identity-service"], "SET", "session:<redacted>", REDIS.format("sessions"))])
    return bff(ctx, "POST", "/api/v2/auth/session", "/api/v2/auth/session", [ident], status=201), 201, {}


def shipping_options(ctx):
    carriers = [b.external(S["shipping-rates-service"], "POST", "https://api.parcelrate.net/v2/rates", "carrier-rates",
                           ms=(90, 260)),
                b.external(S["shipping-rates-service"], "POST", "https://rates.freightlane.io/v1/quote", "carrier-rates",
                           ms=(120, 420))]
    ship = b.grpc(S["checkout-orchestrator"], S["shipping-rates-service"], "vantra.shop.shipping.v1.ShippingService", "GetOptions",
                  ms=(1, 4), children=carriers, parallel=True)
    co = b.http(S["shop-experience-api"], S["checkout-orchestrator"], "POST", "/v1/checkouts/{id}/shipping-options",
                "/v1/checkouts/current/shipping-options", 200, (1.5, 5), [ship])
    return bff(ctx, "POST", "/api/v2/checkout/shipping-options", "/api/v2/checkout/shipping-options", [co]), 200, {}


def place_order(ctx):
    """Checkout: cart, promotions and tax, inventory reservation, payment, order creation, async fulfillment."""
    o = ctx.outcome
    order_id = f"{random.randint(100, 999)}-{random.randint(1000000, 9999999)}-{random.randint(1000000, 9999999)}"
    co, inv, pps = S["checkout-orchestrator"], S["inventory-availability-service"], S["price-promotion-service"]
    pool_incident = ctx.incident == "payments-db-pool"
    cart = b.grpc(co, S["shopping-cart-service"], "vantra.shop.cart.v1.CartService", "GetCart", ms=(1, 3),
                  children=[b.redis(S["shopping-cart-service"], "HGETALL", "cart:<id>", REDIS.format("cart-store")),
                            b.internal(S["shopping-cart-service"], "CartValidator.validateLineItems", ms=(0.4, 2))])
    totals = b.grpc(co, pps, "vantra.shop.pricing.v1.PricingService", "CalculateTotals", ms=(2, 8), children=[
        b.redis(pps, "HMGET", "promo:active:US", REDIS.format("pricing-cache")),
        b.internal(pps, "promotions.evaluate", ms=(1.5, 6), attrs={"promotions.evaluated": random.randint(4, 18)}),
        b.grpc(pps, S["tax-calculation-service"], "vantra.shop.tax.v1.TaxService", "Quote", ms=(1, 4), children=[
            b.external(S["tax-calculation-service"], "POST", "https://api.taxcompute.io/v3/transactions/quote",
                       "tax-provider", ms=(60, 180))])])
    shipping = b.grpc(co, S["shipping-rates-service"], "vantra.shop.shipping.v1.ShippingService", "ConfirmRate", ms=(1, 4),
                      children=[b.redis(S["shipping-rates-service"], "GET", "rate_quote:<id>", REDIS.format("pricing-cache"))])
    conflict = o == "inventory_conflict"
    deadlock = conflict or (current() is None and random.random() < 0.02)
    reserve_sql = ("UPDATE stock_levels SET reserved = reserved + ? WHERE sku = ? AND location_id = ? "
                   "AND on_hand - reserved >= ? RETURNING location_id")
    attempts = []
    if deadlock:
        # Two checkouts reserving the same SKUs in different orders; Postgres aborts one and it is retried.
        attempts.append(b.sql(inv, "UPDATE", "stock_levels", reserve_sql, "inventory", RDS.format("inventory-prod"),
                              ms=(900, 1400), rows=0, error=b.err(
                                  "ERROR: deadlock detected", "org.postgresql.util.PSQLException",
                                  "ERROR: deadlock detected\n  Detail: Process 48213 waits for ShareLock on transaction "
                                  "91827364; blocked by process 48177.\n  Hint: See server log for query details.",
                                  b.stack("java", "org.postgresql.util.PSQLException", "ERROR: deadlock detected", [
                                      ("org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse", "QueryExecutorImpl.java", 2725),
                                      ("org.postgresql.jdbc.PgPreparedStatement.executeUpdate", "PgPreparedStatement.java", 152),
                                      ("com.vantra.shop.inventory.repository.StockRepository.reserve", "StockRepository.java", 91),
                                      ("com.vantra.shop.inventory.service.ReservationService.reserve", "ReservationService.java", 63)]))))
    if not conflict:
        attempts.append(b.sql(inv, "UPDATE", "stock_levels", reserve_sql, "inventory", RDS.format("inventory-prod"),
                              ms=(2, 9), rows=1))
        attempts.append(b.sql(inv, "INSERT", "reservations",
                              "INSERT INTO reservations (id, checkout_id, sku, qty, expires_at) VALUES (?, ?, ?, ?, now() + interval '15 minutes')",
                              "inventory", RDS.format("inventory-prod"), ms=(1.2, 4)))
    reserve = b.grpc(co, inv, "vantra.shop.inventory.v1.InventoryService", "ReserveItems", code=10 if conflict else 0,
                     ms=(2, 6), children=attempts)
    if deadlock:
        reserve.children[0].log("WARN" if not conflict else "ERROR",
                                "stock reservation aborted by deadlock" + ("; retried" if not conflict else "; retries exhausted"),
                                **{"db.collection.name": "stock_levels", "retry.attempt": 1})
    steps = [cart, totals, shipping, reserve]
    if conflict:
        status = 409
    else:
        mode = {"processor_timeout": "processor_timeout", "card_declined": "declined",
                "payment_db_pool": "db_pool"}.get(o, "ok")
        payment, pay_status = platform.authorize_payment(co, mode, db_pressure=pool_incident)
        steps.append(payment)
        status = {201: 201, 402: 402, 504: 504, 503: 502}[pay_status]
        if status != 201:
            steps.append(b.grpc(co, inv, "vantra.shop.inventory.v1.InventoryService", "ReleaseReservation", ms=(1, 4),
                                children=[b.sql(inv, "DELETE", "reservations", "DELETE FROM reservations WHERE checkout_id = ?",
                                                "inventory", RDS.format("inventory-prod"), ms=(1, 3))]))
    if status == 201:
        oms, wfw = S["order-management-service"], S["warehouse-fulfillment-worker"]
        order_db = b.http(co, oms, "POST", "/v1/orders", "/v1/orders", 201, (2, 6), [
            b.sql(oms, "INSERT", "orders",
                  "INSERT INTO orders (id, customer_id, total_minor, currency, status) VALUES (?, ?, ?, ?, 'PLACED')",
                  "orders", RDS.format("orders-prod"), ms=(1.5, 5)),
            b.sql(oms, "INSERT", "order_items",
                  "INSERT INTO order_items (order_id, sku, qty, unit_price_minor) VALUES (?, ?, ?, ?)", "orders",
                  RDS.format("orders-prod"), ms=(1.2, 4)),
            b.sql(oms, "INSERT", "outbox_events",
                  "INSERT INTO outbox_events (aggregate_id, type, payload) VALUES (?, 'OrderPlaced', ?)", "orders",
                  RDS.format("orders-prod"), ms=(0.8, 3))])
        order_db.children[0].children.extend([
            b.kafka(oms, "order-events", wfw, "fulfillment", order_id, [
                b.sql(wfw, "SELECT", "warehouses",
                      "SELECT id FROM warehouses WHERE region = ? AND accepts_orders ORDER BY distance_km LIMIT 1",
                      "fulfillment", RDS.format("fulfillment-prod"), ms=(1, 4), rows=1),
                b.sql(wfw, "INSERT", "shipments",
                      "INSERT INTO shipments (order_id, warehouse_id, status) VALUES (?, ?, 'PENDING')", "fulfillment",
                      RDS.format("fulfillment-prod"), ms=(1.5, 5)),
                b.external(wfw, "POST", "https://api.parcelrate.net/v2/labels", "carrier-labels", status=201, ms=(180, 520))]),
            platform.notify(oms, "order-events", order_id, "email"),
            platform.notify(oms, "order-events", order_id, "push")])
        order_db.children[0].log("INFO", f"order placed {order_id}", order_id=order_id)
        steps.append(order_db)
    co_err = None
    if status in (502, 504):
        co_err = b.err(f"payment-authorization-service returned {503 if status == 502 else 504}", "PaymentAuthorizationError",
                       "payment authorization failed", b.stack("go", "PaymentAuthorizationError", "payment authorization failed", [
                           ("github.com/vantra/shop-checkout/internal/payments.(*Client).Authorize", "/src/internal/payments/client.go", 142),
                           ("github.com/vantra/shop-checkout/internal/orders.(*PlaceOrderHandler).ServeHTTP", "/src/internal/orders/place.go", 88)]))
    node = b.http(S["shop-experience-api"], co, "POST", "/v1/checkouts/{id}/orders", "/v1/checkouts/current/orders",
                  status, (2, 8), steps, error=co_err)
    if status in (502, 504):
        node.children[0].log("ERROR", "place order failed: payment authorization unavailable", order_id=order_id,
                             upstream="payment-authorization-service")
    return bff(ctx, "POST", "/api/v2/checkout/orders", "/api/v2/checkout/orders", [node], status=status), status, \
        {"order_id": order_id}


def get_order(ctx):
    oid = ctx.params["order_id"]
    srv = b.http(S["shop-experience-api"], S["order-management-service"], "GET", "/v1/orders/{orderId}", f"/v1/orders/{oid}", 200,
                 (1.5, 4), [b.sql(S["order-management-service"], "SELECT", "orders",
                                  "SELECT o.*, i.sku, i.qty FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.id = ?",
                                  "orders", RDS.format("orders-prod"), ms=(1.5, 5), rows=3)])
    return bff(ctx, "GET", "/api/v2/orders/:orderId", f"/api/v2/orders/{oid}", [srv]), 200, {}


def home(ctx):
    collections = b.grpc(S["shop-experience-api"], S["product-catalog-service"], "vantra.shop.catalog.v1.CatalogService",
                         "GetCollections", ms=(1, 3), children=[
                             b.redis(S["product-catalog-service"], "GET", "collections:home:fall-2026", REDIS.format("catalog-cache"))])
    return bff(ctx, "GET", "/api/v2/home", "/api/v2/home", [collections]), 200, {}


ENDPOINTS = {
    "home": Endpoint("GET", "/api/v2/home", "/api/v2/home", home, size=(6000, 18000)),
    "list_products": Endpoint("GET", "/api/v2/products", lambda p: f"/api/v2/products?category={p['category']}&page=1&sort=featured",
                              list_products, size=(14000, 42000)),
    "search": Endpoint("GET", "/api/v2/search", lambda p: f"/api/v2/search?q={p['q'].replace(' ', '+')}", search,
                       size=(8000, 30000)),
    "get_product": Endpoint("GET", "/api/v2/products/:sku", lambda p: f"/api/v2/products/{p['sku']}", get_product,
                            size=(4000, 12000)),
    "get_recommendations": Endpoint("GET", "/api/v2/recommendations", lambda p: f"/api/v2/recommendations?sku={p.get('sku', 'home')}",
                                    get_recommendations, outcomes=[("ok", 96), ("slow_model", 4)], size=(5000, 16000)),
    "add_to_cart": Endpoint("POST", "/api/v2/cart/items", "/api/v2/cart/items", add_to_cart,
                            outcomes=[("ok", 96), ("out_of_stock", 4)], size=(600, 2200)),
    "get_cart": Endpoint("GET", "/api/v2/cart", "/api/v2/cart", get_cart, size=(1500, 6000)),
    "login": Endpoint("POST", "/api/v2/auth/session", "/api/v2/auth/session", login, size=(400, 900)),
    "shipping_options": Endpoint("POST", "/api/v2/checkout/shipping-options", "/api/v2/checkout/shipping-options",
                                 shipping_options, size=(900, 2400)),
    "place_order": Endpoint("POST", "/api/v2/checkout/orders", "/api/v2/checkout/orders", place_order,
                            outcomes=[("ok", 88), ("card_declined", 5), ("processor_timeout", 2), ("payment_db_pool", 3),
                                      ("inventory_conflict", 2)],
                            size=(800, 2000)),
    "get_order": Endpoint("GET", "/api/v2/orders/:orderId", lambda p: f"/api/v2/orders/{p['order_id']}", get_order,
                          size=(1800, 5000)),
}

BUNDLE = "/assets/index-D7kQ2mXa.js"
PAGES = {
    "home": Page("/", "Home", bundle=BUNDLE, lcp_target="section.hero > picture > img", heavy=True, doc_height=4200),
    "category": Page("/c/:category", lambda p: CATEGORIES[p["category"]], lambda p: f"/c/{p['category']}",
                     bundle="/assets/category-B3nL8sQe.js", lcp_target="ul.product-grid > li:nth-child(1) img",
                     shifty=True, shift_target="ul.product-grid", doc_height=5200),
    "search": Page("/search", "Search results", lambda p: f"/search?q={p['q'].replace(' ', '+')}",
                   bundle="/assets/search-Ch4vR1pT.js", lcp_target="ul.product-grid > li:nth-child(1) img",
                   shifty=True, shift_target="ul.product-grid", doc_height=3800),
    "product": Page("/products/:sku", lambda p: _product(p["sku"])[1], lambda p: f"/products/{p['sku']}",
                    bundle="/assets/product-Dq9wE3hZ.js", lcp_target="div.pdp-gallery img.primary", heavy=True,
                    shift_target="section.pdp-reviews", doc_height=3600),
    "cart": Page("/cart", "Shopping bag", bundle="/assets/cart-Ay6pK2nV.js", lcp_target="h1.cart-title",
                 lcp_element="h1", doc_height=1500),
    "signin": Page("/account/sign-in", "Sign in", bundle="/assets/account-Fm2xZ8rL.js", lcp_target="form.sign-in h1",
                   lcp_element="h1", doc_height=1100),
    "checkout": Page("/checkout", "Checkout", bundle="/assets/checkout-Cq3tY8vZ.js", lcp_target="form.checkout h2",
                     lcp_element="h2", shift_target="div.payment-element", doc_height=2100),
    "confirmation": Page("/checkout/confirmation/:orderId", "Order confirmed",
                         lambda p: f"/checkout/confirmation/{p['order_id']}", bundle="/assets/checkout-Cq3tY8vZ.js",
                         lcp_target="div.confirmation h1", lcp_element="h1", doc_height=1600),
}

STATIC = [
    {"path": BUNDLE, "size": 186_400, "initiator": "script", "ratio": 3.4},
    {"path": "/assets/vendor-react-Bw1nX7cJ.js", "size": 142_900, "initiator": "script", "ratio": 3.1},
    {"path": "/assets/index-Kp3sV9aQ.css", "size": 38_200, "initiator": "link", "blocking": "blocking", "ratio": 5.2},
    {"path": "/fonts/inter-var-latin.woff2", "size": 48_300, "initiator": "css"},
    {"path": "/img/hero/fall-2026-collection-1600w.avif", "size": 162_000, "initiator": "img"},
]


def make_user(p):
    p["user_id"] = uuid4() if random.random() < 0.55 else None
    p["traits"] = {"loyalty": random.choice(["member", "member", "insider", "insider_plus"])}


def properties(p, rng):
    props = {"experiment.checkout_v3": rng.choice(["control", "treatment"]), "currency": "USD"}
    if p["user_id"]:
        props["loyalty_tier"] = p["traits"]["loyalty"]
    return props


# --- journeys -------------------------------------------------------------------

def _product_page(s, sku):
    name = _product(sku)[1]
    s.page_view("product", sku=sku)
    with s.flow("load_product", **{"product.sku": sku}) as f:
        r, _ = s.parallel(("get_product", {"sku": sku}), ("get_recommendations", {"sku": sku}))
        if not r.ok:
            f["failed"] = True
    s.breadcrumb(f"Viewed product {name} ({sku})")
    s.wait(4, 30)


def _add_to_cart(s, sku):
    s.click('<button class="btn btn-primary btn-block" data-testid="add-to-bag">Add to bag</button>',
            "button[data-testid=add-to-bag]")
    with s.flow("add_to_cart", **{"product.sku": sku}) as f:
        r = s.api("add_to_cart", sku=sku)
        if not r.ok:
            f["failed"] = True
    if r.ok:
        s.breadcrumb(f"Added to bag: {sku} x1")
        s.log("info", "Journey started: add_to_cart", **{"journey.name": "add_to_cart", "product.sku": sku})
    else:
        s.log("warning", "Add to bag failed: item unavailable", **{"http.status_code": str(r.status), "product.sku": sku})
    return r.ok


def _checkout(s):
    s.click('<a class="btn btn-primary" href="/cart">Bag (1)</a>', "a[href=/cart]")
    s.page_view("cart")
    s.api("get_cart")
    s.breadcrumb("Opened bag")
    s.wait(3, 15)
    if s.user_id is None and random.random() < 0.5:
        s.click('<button class="btn btn-outline">Sign in to check out</button>')
        s.page_view("signin")
        s.wait(2, 5)
        s.type_text(random.randint(22, 34))
        s.click('<button class="btn btn-primary" type="submit">Sign in</button>', "form.sign-in button[type=submit]")
        r = s.api("login")
        if r.ok:
            s.login()
            s.breadcrumb("Signed in")
    s.click('<button class="btn btn-primary btn-block" data-testid="checkout">Check out</button>',
            "button[data-testid=checkout]")
    s.page_view("checkout")
    s.log("info", "Journey started: checkout", **{"journey.name": "checkout"})
    s.wait(1, 3)
    s.api("shipping_options")
    s.type_text(random.randint(30, 70))
    s.click('<label class="shipping-option"><input type="radio" name="shipping" value="standard">Standard (3-5 days)</label>')
    s.breadcrumb("Selected shipping: Standard (3-5 days)")
    s.type_text(random.randint(18, 26))
    for attempt in range(2):
        s.click('<button class="btn btn-primary btn-lg" data-testid="place-order">Place order</button>',
                "button[data-testid=place-order]")
        with s.flow("place_order", **{"checkout.attempt": str(attempt + 1)}) as f:
            r = s.api("place_order")
            if not r.ok:
                f["failed"] = True
        if r.ok:
            oid = r.data["order_id"]
            s.log("info", "Journey completed: checkout", **{"journey.name": "checkout", "journey.outcome": "completed",
                                                            "order.id": oid})
            s.page_view("confirmation", order_id=oid)
            s.api("get_order", order_id=oid)
            s.breadcrumb(f"Order placed: {oid}")
            s.wait(5, 20)
            return
        if r.status == 402:
            s.log("warning", "Payment declined", **{"http.status_code": "402", "checkout.step": "payment"})
            s.breadcrumb("Payment declined, updating card")
            s.wait(8, 25)
            s.type_text(random.randint(16, 24))
            continue
        if r.status == 409:
            s.log("warning", "Checkout blocked: item sold out during checkout", **{"http.status_code": "409"})
            s.wait(4, 12)
            return
        s.log("error", "Checkout failed: payment could not be authorized", **{
            "http.status_code": str(r.status), "checkout.step": "payment", "journey.name": "checkout",
            "journey.outcome": "failed"})
        if random.random() < 0.6:
            # Visitors hammer the button when nothing seems to happen.
            for _ in range(random.randint(2, 4)):
                s.t += random.uniform(250, 600)
                s.click('<button class="btn btn-primary btn-lg" data-testid="place-order">Place order</button>')
        if random.random() < 0.25:
            s.exception("TypeError", "Cannot read properties of undefined (reading 'orderId')",
                        function="handleOrderResponse")
        s.wait(6, 20)
        return


def browse_and_buy(s):
    p = random.choice(PRODUCTS)
    s.page_view("home")
    s.parallel(("home", {}), ("get_recommendations", {"sku": "home"}))
    s.wait(3, 10)
    s.click(f'<a class="nav-link" href="/c/{p[2]}">{CATEGORIES[p[2]]}</a>', "nav a.nav-link")
    s.page_view("category", category=p[2])
    s.api("list_products", category=p[2])
    s.wait(5, 25)
    s.click(f'<a class="product-card__link" href="/products/{p[0]}">{p[1]}</a>', "a.product-card__link")
    _product_page(s, p[0])
    if _add_to_cart(s, p[0]):
        s.wait(2, 8)
        _checkout(s)
    s.leave()


def search_and_browse(s):
    q = random.choice(SEARCHES)
    s.page_view("home")
    s.parallel(("home", {}), ("get_recommendations", {"sku": "home"}))
    s.wait(2, 8)
    s.click('<input class="search-input" type="search" placeholder="Search products">', "input.search-input")
    s.type_text(len(q))
    s.page_view("search", q=q)
    s.api("search", q=q)
    s.breadcrumb(f"Searched: {q}")
    s.wait(4, 20)
    for p in random.sample(PRODUCTS, random.randint(1, 3)):
        s.click(f'<a class="product-card__link" href="/products/{p[0]}">{p[1]}</a>', "a.product-card__link")
        _product_page(s, p[0])
        if random.random() < 0.25:
            _add_to_cart(s, p[0])
            break
    s.leave()


def landing_from_ad(s):
    p = random.choice(PRODUCTS)
    _product_page(s, p[0])
    if random.random() < 0.45 and _add_to_cart(s, p[0]):
        s.wait(2, 6)
        if random.random() < 0.6:
            _checkout(s)
    s.leave()


def returning_checkout(s):
    if s.persona["user_id"]:
        s.login()
    p = random.choice(PRODUCTS)
    _product_page(s, p[0])
    if _add_to_cart(s, p[0]):
        _checkout(s)
    s.leave()


def bounce(s):
    s.page_view("home")
    s.parallel(("home", {}), ("get_recommendations", {"sku": "home"}))
    s.wait(4, 25)
    s.leave()


PROFILE = Profile(
    key="ecommerce", brand="Northline Goods", namespace="vantra-shop",
    web_origin="https://www.northlinegoods.com", api_host="api.northlinegoods.com",
    cdn="https://cdn.northlinegoods.com", main_bundle=BUNDLE, web_service="northline-web",
    edge_service="api-gateway", services=S, pages=PAGES, endpoints=ENDPOINTS, static_assets=STATIC,
    app_versions=[("2026.10.3", "2.31.0", 64), ("2026.10.2", "2.31.0", 27), ("2026.9.4", "2.30.0", 9)],
    make_user=make_user, properties=properties,
    journeys=[(browse_and_buy, 22), (search_and_browse, 30), (landing_from_ad, 20), (returning_checkout, 10),
              (bounce, 18)],
    incidents={
        "payment-timeouts": {"place_order": {"processor_timeout": 30}},
        "payments-db-pool": {"place_order": {"payment_db_pool": 35}},
        "reviews-n-plus-one": {},
        "recs-latency": {},
        "inventory-sync": {"add_to_cart": {"out_of_stock": 25}, "place_order": {"inventory_conflict": 12}},
    },
)

PROFILE.android = dict(
    ui="compose",
    package="com.northlinegoods.android", app_name="Northline", activity="com.northlinegoods.android.MainActivity",
    versions=[("6.14.2", "61402", "9.3.1", 64), ("6.14.1", "61401", "9.3.1", 27), ("6.12.4", "61204", "9.3.0", 9)],
    screens={"home": "HomeFragment", "category": "CategoryFragment", "search": "SearchResultsFragment",
             "product": "ProductDetailFragment", "cart": "BagFragment", "signin": "SignInFragment",
             "checkout": "CheckoutFragment", "confirmation": "OrderConfirmationFragment"},
    user_agent="Northline/{version} (Android {os}; {model}) okhttp/4.12.0",
    business_unit="retail", shared_api_host="api.vantrashop.com",
)
