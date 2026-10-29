"""Vantra Travel: flights and hotels booking site with supplier fan-out search."""

import random

from .. import backend as b
from ..util import uuid4
from . import Endpoint, Page, Profile, platform, services

S = services("vantra-travel", "prod-use1-travel", [
    ("travel-experience-api", "nodejs", "14.6.0", "web", 8),
    ("geo-location-service", "go", "2.3.1", "search", 3),
    ("air-shopping-service", "go", "9.12.0", "search", 6),
    ("gds-connector-service", "java", "5.8.3", "suppliers", 4),
    ("ndc-connector-service", "java", "3.21.0", "suppliers", 4),
    ("fare-rules-service", "python", "2.2.4", "pricing", 2),
    ("fare-pricing-service", "python", "7.0.2", "pricing", 4),
    ("lodging-search-service", "go", "4.9.5", "search", 4),
    ("lodging-content-service", "nodejs", "2.17.3", "content", 2),
    ("reservation-service", "java", "11.3.0", "booking", 4),
    ("pnr-management-service", "java", "4.0.8", "booking", 2),
    ("loyalty-accrual-service", "dotnet", "3.3.2", "loyalty", 2),
    ("trip-itinerary-service", "nodejs", "5.1.0", "trips", 3),
    ("ticketing-fulfillment-worker", "java", "2.14.0", "booking", 2),
])
S.update(platform.P)

REDIS = "{}.p2vn8d.ng.0001.use1.cache.amazonaws.com"
RDS = "{}.cluster-w6t1j9k3d8yb.us-east-1.rds.amazonaws.com"
ROUTES = [("BOS", "LHR", "Boston", "London"), ("JFK", "CDG", "New York", "Paris"), ("SFO", "NRT", "San Francisco", "Tokyo"),
          ("ORD", "MIA", "Chicago", "Miami"), ("LAX", "HNL", "Los Angeles", "Honolulu"), ("SEA", "DEN", "Seattle", "Denver"),
          ("ATL", "CUN", "Atlanta", "Cancun"), ("DFW", "LAS", "Dallas", "Las Vegas"), ("IAD", "FCO", "Washington", "Rome")]
AIRLINES = [("aerolink", "api.aerolink-ndc.com"), ("skyward", "ndc.skywardair.net"), ("pacifica", "gateway.pacifica-air.com")]
HOTELS = ["The Harborline", "Maison Rivoli", "Shinjuku Garden Hotel", "Coral Reef Resort", "The Alder", "Plaza Verde"]


# Routes where the experience API personalizes the response with the customer's profile.
PROFILE_ROUTES = {'/api/v3/flights/search', '/api/v3/trips', '/api/v3/bookings'}


def bff(ctx, method, route, path, children, status=200, parallel=False, error=None, ms=(3, 10)):
    """api-gateway -> travel-experience-api: token check and rate limit at the edge, then the experience API."""
    return platform.gateway(ctx, S["travel-experience-api"], method, route, path, children, status, parallel, error, ms,
                            with_profile=route in PROFILE_ROUTES)


def locations(ctx):
    srv = b.grpc(S["travel-experience-api"], S["geo-location-service"], "vantra.travel.locations.v1.LocationService", "Autocomplete",
                 ms=(0.8, 3), children=[b.redis(S["geo-location-service"], "ZRANGEBYLEX", "loc:prefix:<q>", REDIS.format("search-cache"))])
    return bff(ctx, "GET", "/api/v3/locations", f"/api/v3/locations?q={ctx.params['q']}", [srv]), 200, {}


def flight_search(ctx):
    o, slow_supplier = ctx.outcome, ctx.incident == "supplier-timeouts" or ctx.outcome == "supplier_timeout"
    cache_hit = random.random() < 0.3
    fs = S["air-shopping-service"]
    children = [b.redis(fs, "GET", f"fares:{ctx.params['origin']}-{ctx.params['dest']}:2026-11-21", REDIS.format("fare-cache"))]
    if not cache_hit:
        suppliers = [b.http(fs, S["gds-connector-service"], "POST", "/v2/availability", "/v2/availability", 200, (6, 18), [
            b.external(S["gds-connector-service"], "POST", "https://ws.globaldistribution.net/flight/v4/offers/search", "gds",
                       ms=(420, 1300))])]
        slow_one = random.randrange(len(AIRLINES)) if slow_supplier else -1
        for i, (airline, host) in enumerate(AIRLINES):
            if i == slow_one:
                ext = b.external(S["ndc-connector-service"], "POST", f"https://{host}/ndc/21.3/AirShopping", f"ndc-{airline}",
                                 status=0, ms=(4200, 9000), timeout_ms=3000,
                                 error=b.err("Read timed out", "java.net.SocketTimeoutException", "Read timed out",
                                             b.stack("java", "java.net.SocketTimeoutException", "Read timed out", [
                                                 ("java.base/sun.nio.ch.NioSocketImpl.timedRead", "NioSocketImpl.java", 278),
                                                 ("okhttp3.internal.http1.Http1ExchangeCodec.readResponseHeaders", "Http1ExchangeCodec.kt", 178),
                                                 ("com.vantra.travel.suppliers.ndc.NdcClient.airShopping", "NdcClient.java", 96)]),
                                             error_type="java.net.SocketTimeoutException"))
                call = b.http(fs, S["ndc-connector-service"], "POST", "/v1/{airline}/air-shopping", f"/v1/{airline}/air-shopping",
                              504, (2, 6), [ext])
                call.children[0].log("WARN", f"supplier {airline} timed out after 3000ms; returning partial results",
                                     supplier=airline, timeout_ms=3000)
            else:
                call = b.http(fs, S["ndc-connector-service"], "POST", "/v1/{airline}/air-shopping", f"/v1/{airline}/air-shopping",
                              200, (2, 6), [b.external(S["ndc-connector-service"], "POST", f"https://{host}/ndc/21.3/AirShopping",
                                                       f"ndc-{airline}", ms=(350, 1100))])
            suppliers.append(call)
        children.append(b.internal(fs, "supplier.fanout", ms=(0.5, 2), children=suppliers, parallel=True,
                                   attrs={"suppliers.count": len(suppliers)}))
        children.append(b.grpc(fs, S["fare-pricing-service"], "vantra.travel.pricing.v1.PricingEngine", "PriceOffers", ms=(8, 30), children=[
            b.internal(S["fare-pricing-service"], "markup.apply", ms=(4, 16), attrs={"offers.count": random.randint(80, 320)}),
            b.redis(S["fare-pricing-service"], "HMGET", "fx:rates", REDIS.format("pricing-cache"))]))
        children.append(b.redis(fs, "SETEX", f"fares:{ctx.params['origin']}-{ctx.params['dest']}:2026-11-21", REDIS.format("fare-cache")))
    srv = b.grpc(S["travel-experience-api"], fs, "vantra.travel.flights.v1.FlightSearch", "Search", ms=(2, 6), children=children)
    root = bff(ctx, "POST", "/api/v3/flights/search", "/api/v3/flights/search", [srv])
    return root, 200, {"partial": slow_supplier and not cache_hit}


def offer(ctx):
    changed = ctx.outcome == "price_changed"
    srv = b.http(S["travel-experience-api"], S["fare-pricing-service"], "POST", "/v1/offers/{id}/reprice", "/v1/offers/<id>/reprice",
                 409 if changed else 200, (4, 12), [
                     b.http(S["fare-pricing-service"], S["gds-connector-service"], "POST", "/v2/offers/price", "/v2/offers/price", 200, (5, 14), [
                         b.external(S["gds-connector-service"], "POST", "https://ws.globaldistribution.net/flight/v4/offers/price", "gds",
                                    ms=(280, 900))]),
                     b.grpc(S["fare-pricing-service"], S["fare-rules-service"], "vantra.travel.fares.v1.FareRules", "GetRules", ms=(2, 9))])
    status = 409 if changed else 200
    return bff(ctx, "POST", "/api/v3/offers/:offerId/price", "/api/v3/offers/OF-8F2K9/price", [srv], status=status), status, {}


def hotel_search(ctx):
    hs = S["lodging-search-service"]
    supplier_down = ctx.outcome == "supplier_503"
    srv = b.grpc(S["travel-experience-api"], hs, "vantra.travel.hotels.v1.HotelSearch", "Search", ms=(2, 6), parallel=True, children=[
        b.nosql(hs, "elasticsearch", "search", "hotels-v12", ms=(12, 40), host="vpc-hotels-search-r2kp.us-east-1.es.amazonaws.com",
                statement='{"query":{"bool":{"filter":[{"geo_distance":{"distance":"8km","location":"?"}}]}},"size":60}'),
        b.external(hs, "POST", "https://api.staysupply.com/v2/availability", "hotel-supplier",
                   status=503 if supplier_down else 200, ms=(40, 120) if supplier_down else (380, 1100)),
        b.http(hs, S["lodging-content-service"], "GET", "/v1/properties", "/v1/properties?ids=...", 200, (3, 9), [
            b.nosql(S["lodging-content-service"], "mongodb", "find", "properties", ms=(4, 14), host="hotels-content.travel.vantra.internal")])])
    if supplier_down:
        srv.children[0].log("WARN", "hotel supplier returned 503; serving cached rates only", supplier="staysupply")
    return bff(ctx, "POST", "/api/v3/hotels/search", "/api/v3/hotels/search", [srv]), 200, {}


def create_booking(ctx):
    """Reservation: reprice the held offer, take payment, create the PNR, accrue loyalty, then ticket and notify."""
    o = ctx.outcome
    ref = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    rs, pnr, fps, gds = S["reservation-service"], S["pnr-management-service"], S["fare-pricing-service"], S["gds-connector-service"]
    pool_incident = ctx.incident == "payments-db-pool"
    steps = [
        b.redis(rs, "GET", "offer:OF-8F2K9", REDIS.format("fare-cache")),
        b.grpc(rs, fps, "vantra.travel.pricing.v1.PricingEngine", "RepriceOffer", ms=(3, 9), children=[
            b.http(fps, gds, "POST", "/v2/offers/price", "/v2/offers/price", 200, (4, 12), [
                b.external(gds, "POST", "https://ws.globaldistribution.net/flight/v4/offers/price", "gds", ms=(260, 820))]),
            b.grpc(fps, S["fare-rules-service"], "vantra.travel.fares.v1.FareRules", "GetRules", ms=(2, 8))]),
        b.sql(rs, "INSERT", "bookings",
              "INSERT INTO bookings (ref, customer_id, offer_id, status, total_minor) VALUES (?, ?, ?, 'PENDING', ?)",
              "reservations", RDS.format("reservations-prod"), ms=(1.5, 5)),
        b.sql(rs, "INSERT", "booking_passengers",
              "INSERT INTO booking_passengers (booking_ref, pax_index, given_name_enc, surname_enc, dob_enc) VALUES (?, ?, ?, ?, ?)",
              "reservations", RDS.format("reservations-prod"), ms=(1.2, 4)),
    ]
    mode = {"3ds_failed": "3ds_failed", "payment_db_pool": "db_pool"}.get(o, "ok")
    payment, pay_status = platform.authorize_payment(rs, mode, db_pressure=pool_incident)
    steps.append(payment)
    status = {201: 201, 402: 402, 503: 502}[pay_status]
    error = None
    if status == 201:
        pnr_timeout = o == "pnr_timeout"
        pnr_call = b.http(rs, pnr, "POST", "/v1/pnr", "/v1/pnr", 504 if pnr_timeout else 201, (3, 8), [
            b.sql(pnr, "SELECT", "seat_assignments", "SELECT seat, cabin FROM seat_assignments WHERE offer_id = ?",
                  "reservations", RDS.format("reservations-prod"), ms=(1, 3.5), rows=2),
            b.external(pnr, "POST", "https://ws.globaldistribution.net/pnr/v3/create", "gds",
                       status=0 if pnr_timeout else 201, ms=(6000, 9000) if pnr_timeout else (500, 1400),
                       timeout_ms=5000 if pnr_timeout else None,
                       error=b.err("PNR creation timed out", "java.util.concurrent.TimeoutException",
                                   "PNR creation did not complete within 5000 ms",
                                   b.stack("java", "java.util.concurrent.TimeoutException", "PNR creation did not complete within 5000 ms", [
                                       ("java.base/java.util.concurrent.CompletableFuture.timedGet", "CompletableFuture.java", 1960),
                                       ("com.vantra.travel.pnr.GdsPnrClient.create", "GdsPnrClient.java", 88)]),
                                   error_type="java.util.concurrent.TimeoutException") if pnr_timeout else None)])
        steps.append(pnr_call)
        if pnr_timeout:
            status = 504
            pnr_call.children[0].log("ERROR", f"PNR creation timed out for booking {ref}; voiding payment", booking_ref=ref)
            steps.append(b.http(rs, S["payment-authorization-service"], "POST", "/v1/authorizations/{id}/void",
                                "/v1/authorizations/<id>/void", 200, (3, 8), [
                                    b.external(S["payment-authorization-service"], "POST", platform.PSP + "/<id>/void",
                                               "card-acquirer", status=200, ms=(150, 420))]))
        else:
            steps += [
                b.grpc(rs, S["loyalty-accrual-service"], "vantra.travel.loyalty.v1.Loyalty", "AccruePending", ms=(2, 6),
                       children=[b.sql(S["loyalty-accrual-service"], "INSERT", "accruals",
                                       "INSERT INTO accruals (member_id, booking_ref, points) VALUES (@m, @b, @p)",
                                       "loyalty", RDS.format("loyalty-prod"), ms=(1.2, 4))]),
                b.sql(rs, "UPDATE", "bookings", "UPDATE bookings SET status = 'CONFIRMED', pnr = ? WHERE ref = ?",
                      "reservations", RDS.format("reservations-prod"), ms=(1.2, 4)),
                b.kafka(rs, "booking-events", S["ticketing-fulfillment-worker"], "ticketing", ref, [
                    b.external(S["ticketing-fulfillment-worker"], "POST", "https://ws.globaldistribution.net/ticketing/v2/issue",
                               "gds", ms=(700, 1800)),
                    b.sql(S["ticketing-fulfillment-worker"], "INSERT", "tickets",
                          "INSERT INTO tickets (booking_ref, ticket_number, pax_index) VALUES (?, ?, ?)", "reservations",
                          RDS.format("reservations-prod"), ms=(1.2, 4))]),
                b.kafka(rs, "booking-events", S["trip-itinerary-service"], "itinerary", ref, [
                    b.nosql(S["trip-itinerary-service"], "mongodb", "insert", "trips", ms=(3, 10), host="trips.travel.vantra.internal")]),
                platform.notify(rs, "booking-events", ref, "email"),
                platform.notify(rs, "booking-events", ref, "push")]
    if status != 201:
        steps.append(b.sql(rs, "UPDATE", "bookings", "UPDATE bookings SET status = 'FAILED', failure_reason = ? WHERE ref = ?",
                           "reservations", RDS.format("reservations-prod"), ms=(1.2, 4)))
    if status == 502:
        error = b.err("payment-authorization-service returned 503", "com.vantra.travel.reservation.PaymentUnavailableException",
                      "payment authorization unavailable",
                      b.stack("java", "com.vantra.travel.reservation.PaymentUnavailableException", "payment authorization unavailable", [
                          ("com.vantra.travel.reservation.payments.PaymentClient.authorize", "PaymentClient.java", 74),
                          ("com.vantra.travel.reservation.service.BookingService.create", "BookingService.java", 133),
                          ("com.vantra.travel.reservation.api.BookingController.create", "BookingController.java", 47)]))
    srv = b.http(S["travel-experience-api"], rs, "POST", "/v1/bookings", "/v1/bookings", status, (4, 12), steps, error=error)
    if status == 502:
        srv.children[0].log("ERROR", f"booking {ref} failed: payment authorization unavailable", booking_ref=ref,
                            upstream="payment-authorization-service")
    return bff(ctx, "POST", "/api/v3/bookings", "/api/v3/bookings", [srv], status=status), status, {"ref": ref}


def trips(ctx):
    srv = b.http(S["travel-experience-api"], S["trip-itinerary-service"], "GET", "/v1/trips", "/v1/trips?status=upcoming", 200, (2, 6), [
        b.nosql(S["trip-itinerary-service"], "mongodb", "find", "trips", ms=(3, 12), host="trips.travel.vantra.internal"),
        b.grpc(S["trip-itinerary-service"], S["loyalty-accrual-service"], "vantra.travel.loyalty.v1.Loyalty", "GetBalance", ms=(1.5, 5))])
    return bff(ctx, "GET", "/api/v3/trips", "/api/v3/trips?status=upcoming", [srv]), 200, {}


ENDPOINTS = {
    "locations": Endpoint("GET", "/api/v3/locations", lambda p: f"/api/v3/locations?q={p['q']}", locations, size=(700, 2600)),
    "flight_search": Endpoint("POST", "/api/v3/flights/search", "/api/v3/flights/search", flight_search,
                              outcomes=[("ok", 93), ("supplier_timeout", 7)], size=(60_000, 180_000)),
    "offer": Endpoint("POST", "/api/v3/offers/:offerId/price", "/api/v3/offers/OF-8F2K9/price", offer,
                      outcomes=[("ok", 94), ("price_changed", 6)], size=(4000, 11000)),
    "hotel_search": Endpoint("POST", "/api/v3/hotels/search", "/api/v3/hotels/search", hotel_search,
                             outcomes=[("ok", 95), ("supplier_503", 5)], size=(40_000, 120_000)),
    "create_booking": Endpoint("POST", "/api/v3/bookings", "/api/v3/bookings", create_booking,
                               outcomes=[("ok", 87), ("3ds_failed", 4), ("pnr_timeout", 3), ("payment_db_pool", 6)], size=(1500, 4000)),
    "trips": Endpoint("GET", "/api/v3/trips", "/api/v3/trips?status=upcoming", trips, size=(5000, 16000)),
}

BUNDLE = "/_app/immutable/entry/app.Cx81mQ.js"
PAGES = {
    "home": Page("/", "Flights, hotels and vacation packages", bundle=BUNDLE, lcp_target="section.hero img", heavy=True,
                 doc_height=3900),
    "flights": Page("/flights/search", lambda p: f"{p['from']} to {p['to']} flights",
                    lambda p: f"/flights/search?from={p['origin']}&to={p['dest']}&depart=2026-11-21&return=2026-11-28&adults=2",
                    bundle="/_app/immutable/nodes/flights.D2a9kL.js", lcp_target="ol.results li:first-child",
                    lcp_element="li", shifty=True, shift_target="ol.results", doc_height=6400),
    "offer": Page("/flights/offer/:offerId", "Review your flight", lambda p: "/flights/offer/OF-8F2K9",
                  bundle="/_app/immutable/nodes/offer.Bf3xQ0.js", lcp_target="div.itinerary-summary", lcp_element="div",
                  doc_height=2600),
    "checkout": Page("/checkout/:offerId", "Traveler details and payment", lambda p: "/checkout/OF-8F2K9",
                     bundle="/_app/immutable/nodes/checkout.Ak72pE.js", lcp_target="form.travelers h2", lcp_element="h2",
                     shift_target="div.payment-iframe", doc_height=3100),
    "confirmation": Page("/trips/:bookingRef/confirmation", "Booking confirmed",
                         lambda p: f"/trips/{p['ref']}/confirmation", bundle="/_app/immutable/nodes/trips.Qe1t5Z.js",
                         lcp_target="div.confirmation h1", lcp_element="h1", doc_height=1900),
    "hotels": Page("/hotels/search", lambda p: f"Hotels in {p['city']}", lambda p: f"/hotels/search?city={p['city'].replace(' ', '+')}&checkin=2026-11-21&nights=3",
                   bundle="/_app/immutable/nodes/hotels.Pk8w3R.js", lcp_target="ol.hotel-results img", heavy=True,
                   shifty=True, shift_target="div.map-panel", doc_height=5800),
    "trips": Page("/trips", "My trips", bundle="/_app/immutable/nodes/trips.Qe1t5Z.js", lcp_target="ul.trip-cards",
                  lcp_element="ul", doc_height=2000),
}

STATIC = [
    {"path": BUNDLE, "size": 98_200, "initiator": "script", "ratio": 3.2},
    {"path": "/_app/immutable/chunks/vendor.Hn2q8T.js", "size": 131_500, "initiator": "script", "ratio": 3.0},
    {"path": "/_app/immutable/assets/app.Wd7c1X.css", "size": 44_100, "initiator": "link", "blocking": "blocking", "ratio": 5.4},
    {"path": "/fonts/manrope-latin.woff2", "size": 36_900, "initiator": "css"},
    {"path": "/img/hero/autumn-getaways-1920w.webp", "size": 214_000, "initiator": "img"},
]


def make_user(p):
    p["user_id"] = uuid4() if random.random() < 0.6 else None
    p["traits"] = {"tier": random.choice(["blue", "blue", "silver", "gold", "platinum"])}


def properties(p, rng):
    props = {"market": "US", "currency": "USD", "experiment.flex_dates": rng.choice(["control", "variant_b"])}
    if p["user_id"]:
        props["loyalty.tier"] = p["traits"]["tier"]
    return props


def _search_flights(s):
    o, d, ocity, dcity = random.choice(ROUTES)
    s.page_view("home")
    s.wait(2, 8)
    s.click('<input id="flight-origin" class="field-input" placeholder="Leaving from">', "#flight-origin")
    s.type_text(3)
    s.api("locations", q=ocity[:3].lower())
    s.click('<input id="flight-destination" class="field-input" placeholder="Going to">', "#flight-destination")
    s.type_text(4)
    s.api("locations", q=dcity[:4].lower())
    s.click('<button class="btn btn-primary btn-search" type="submit">Search flights</button>', "button.btn-search")
    s.page_view("flights", origin=o, dest=d, **{"from": ocity, "to": dcity})
    with s.flow("flight_search", **{"search.origin": o, "search.destination": d}) as f:
        r = s.api("flight_search", origin=o, dest=d)
        if not r.ok:
            f["failed"] = True
    s.breadcrumb(f"Searched flights {o}-{d}, 2 adults")
    if r.data.get("partial"):
        s.log("warning", "Flight results incomplete: a supplier timed out", **{"search.route": f"{o}-{d}"})
    s.wait(10, 60)
    return o, d, dcity


def book_flight(s):
    if s.persona["user_id"] and random.random() < 0.7:
        s.login()
    o, d, _ = _search_flights(s)
    s.click('<button class="btn btn-outline-primary select-fare">Select</button>', "button.select-fare")
    s.page_view("offer")
    r = s.api("offer")
    if r.status == 409:
        s.log("warning", "Fare changed during review", **{"http.status_code": "409"})
        s.breadcrumb("Price updated")
        s.wait(5, 15)
        if random.random() < 0.5:
            return s.leave()
    s.wait(8, 30)
    s.click('<button class="btn btn-primary">Continue to checkout</button>')
    s.page_view("checkout")
    s.type_text(random.randint(60, 120))
    s.click('<button class="btn btn-primary btn-lg" data-testid="book-now">Complete booking</button>',
            "button[data-testid=book-now]")
    with s.flow("create_booking", **{"booking.route": f"{o}-{d}"}) as f:
        r = s.api("create_booking")
        if not r.ok:
            f["failed"] = True
    if r.ok:
        s.breadcrumb(f"Booking confirmed: {r.data['ref']}")
        s.log("info", "Journey completed: booking", **{"journey.name": "booking", "journey.outcome": "completed",
                                                       "booking.ref": r.data["ref"]})
        s.page_view("confirmation", ref=r.data["ref"])
        s.wait(6, 20)
    elif r.status == 402:
        s.log("warning", "Card authentication (3-D Secure) failed", **{"http.status_code": "402"})
        s.wait(5, 15)
    else:
        s.log("error", "Booking failed: reservation could not be confirmed", **{
            "http.status_code": str(r.status), "journey.name": "booking", "journey.outcome": "failed"})
        if random.random() < 0.3:
            s.exception("TypeError", "Cannot read properties of null (reading 'bookingRef')", function="onBookingResult")
        s.wait(5, 18)
    s.leave()


def search_only(s):
    _search_flights(s)
    if random.random() < 0.4:
        s.click('<button class="chip" aria-pressed="false">Nonstop only</button>', breadcrumb="Filter: nonstop")
        s.wait(5, 25)
    s.leave()


def hotels(s):
    city = random.choice(ROUTES)[3]
    s.page_view("home")
    s.wait(2, 6)
    s.click('<button role="tab" class="tab" aria-selected="false">Stays</button>')
    s.type_text(len(city))
    s.click('<button class="btn btn-primary btn-search" type="submit">Search stays</button>')
    s.page_view("hotels", city=city)
    with s.flow("hotel_search", **{"search.city": city}):
        r = s.api("hotel_search", city=city)
    s.breadcrumb(f"Searched hotels in {city}")
    s.wait(10, 45)
    s.click(f'<a class="hotel-card" href="#">{random.choice(HOTELS)}</a>')
    s.wait(8, 30)
    s.leave()


def manage_trip(s):
    if s.persona["user_id"]:
        s.login()
    s.page_view("trips")
    s.api("trips")
    s.wait(10, 40)
    s.leave()


PROFILE = Profile(
    key="travel", brand="Vantra Travel", namespace="vantra-travel",
    web_origin="https://www.vantratravel.com", api_host="api.vantratravel.com", cdn="https://assets.vantratravel.com",
    main_bundle=BUNDLE, web_service="vantra-web", edge_service="api-gateway", services=S, pages=PAGES,
    endpoints=ENDPOINTS, static_assets=STATIC,
    app_versions=[("10.28.0", "2.31.0", 61), ("10.27.3", "2.31.0", 30), ("10.25.1", "2.30.0", 9)],
    make_user=make_user, properties=properties,
    journeys=[(book_flight, 14), (search_only, 38), (hotels, 22), (manage_trip, 12)],
    incidents={
        "supplier-timeouts": {"flight_search": {"supplier_timeout": 60}},
        "gds-pnr-timeouts": {"create_booking": {"pnr_timeout": 35}},
        "payments-db-pool": {"create_booking": {"payment_db_pool": 40}},
        "hotel-supplier-outage": {"hotel_search": {"supplier_503": 70}},
    },
)

# Android app: one activity hosting fragments, as most travel apps ship today.
PROFILE.android = dict(
    ui="compose",
    package="com.vantratravel.android", app_name="Vantra", activity="com.vantratravel.android.ui.MainActivity",
    versions=[("10.28.0", "1028004", "9.3.1", 61), ("10.27.3", "1027032", "9.3.1", 30), ("10.25.1", "1025011", "9.3.0", 9)],
    screens={"home": "HomeFragment", "flights": "FlightResultsFragment", "offer": "FareReviewFragment",
             "checkout": "CheckoutFragment", "confirmation": "BookingConfirmationFragment",
             "hotels": "HotelResultsFragment", "trips": "MyTripsFragment"},
    user_agent="Vantra/{version} (Android {os}; {model}) okhttp/4.12.0",
    business_unit="travel", shared_api_host="api.vantratravel.com",
)
