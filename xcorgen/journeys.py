"""Business-state-driven journeys shared by Android and web capture adapters."""
from .client_faults import blockage, fault, followup
from .milestones import mark
from .util import lat

ROOT_OPERATIONS = {
    "search": "search_products", "get_product": "load_product", "get_recommendations": "load_recommendations",
    "add_to_cart": "update_cart", "get_cart": "load_cart", "shipping_options": "quote_shipping",
    "place_order": "authorize_payment", "get_order": "load_order_confirmation",
    "accounts": "load_accounts", "transactions": "load_transaction_history", "payees": "load_recipients",
    "create_transfer": "submit_transfer", "statement": "download_statement", "lock_card": "lock_card",
    "flight_search": "search_flights", "hotel_search": "search_hotels", "hotel_revalidate": "revalidate_hotel_rate",
    "offer": "revalidate_fare", "create_booking": "authorize_booking", "trips": "load_itineraries",
    "operation_status": "reconcile_operation", "login": "authenticate", "mfa_verify": "verify_identity_challenge",
}


def traced_attempt(s, key, params):
    name = ROOT_OPERATIONS.get(key)
    if not name:
        result = s.api(key, **params)
        crash_after(s, key, result)
        return result
    started = s.t
    with s.flow(name) as task:
        result = s.api(key, **params)
        slow = getattr(s.plan, "slow_root_span", None)
        if slow and slow["span"] == name:
            # Client-side work (parsing, mapping, rendering) holds the operation open.
            s.t = max(s.t, started + lat(slow["p50_ms"], slow["p95_ms"], s.rng))
        task["failed"] = not result.ok
        task["attributes"] = {"app.operation": key, "http.response.status_code": result.status,
                              "app.fallback.used": any(result.data.get(k) for k in ("fallback", "snapshot", "cached", "partial"))}
    crash_after(s, key, result)
    return result


def crash_after(s, key, result):
    """A crash planned for the moment a named request's response is handled."""
    plan = s.plan
    details = getattr(plan, "crash_details", None) or {}
    if plan.client_fault != "crash" or details.get("endpoint") != key or getattr(plan, "crashed", False):
        return
    if details.get("on_failure_only") and result.ok:
        return
    plan.crashed = True
    fault(s, fatal=True)


def tap(s, label):
    s.click(f'<button data-testid="action">{label}</button>')


def request(s, key, **params):
    """One failed attempt, optional bounded retry, without duplicate side effects."""
    plan = s.plan
    transport = getattr(plan, "transport", None)
    target = getattr(plan, "transport_target", None) or plan.target
    if transport and not getattr(plan, "transport_used", False) and (target == "*" or key == target):
        params["_transport"] = transport
        plan.transport_used = True
    r = traced_attempt(s, key, params)
    if r.status is None:
        mark(s, "request_retry_requested")
        s.wait(.8, 2)
        mark(s, "connectivity_restored")
        params.pop("_transport", None)
        r = traced_attempt(s, key, params)
    if r.status == 429:
        s.log("warning", "Request rate exceeded; retrying after the server delay")
        delay = max(1, min(60, r.data.get("retry_after_seconds", 3)))
        s.wait(delay, delay)
        r = traced_attempt(s, key, params)
    if r.data.get("cache_fallback"):
        s.log("warning", "Cache unavailable; database fallback served the request")
    if r.data.get("processing_delayed") and key != "operation_status":
        s.log("info", "Processing is pending; confirmation will follow")
        s.wait(1, 2)
        traced_attempt(s, "operation_status", {})
    return r


def begin(s, page, **params):
    s.page_view(page, **params)
    mark(s, "app_foregrounded")
    if s.user_id:
        mark(s, "authentication_reused")


def client_issue(s, stage="content"):
    background_share = getattr(s, "population_settings", {}).get("native_background_share", .12) if s.persona.get("population_model") else .08
    if s.persona.get("population_model") and s.persona.get("platform") not in ("android", "ios"):
        background_share = 0
    if stage == "content" and s.plan.client_fault != "crash" and hasattr(s, "background") and s.rng.random() < background_share:
        mark(s, "app_backgrounded")
        s.background(6, 45) if s.persona.get("population_model") else s.background(4, 12)
        mark(s, "session_resumed")
    plan = s.plan
    if stage == "content":
        if getattr(plan, "blockage", False) and not getattr(plan, "blockage_done", False):
            plan.blockage_done = True
            blockage(s)
        if getattr(plan, "handled", False) and not getattr(plan, "handled_done", False):
            plan.handled_done = True
            fault(s)
            mark(s, "local_cache_rebuilt")
    if s.plan.client_fault == "crash" and not (getattr(s.plan, "crash_details", None) or {}).get("endpoint"):
        stages = {"ecommerce": ("authorization", "callback", "cart", "content", "content"),
                  "banking": ("content", "callback", "content", "content", "content"),
                  "travel": ("authorization", "callback", "content", "content", "content")}
        if stage != stages[s.profile.key][s.plan.crash_family]:
            return
        if stage == "callback":
            # Destroy the original view before the delayed callback runs.
            s.page_view("dashboard" if s.profile.key == "banking" else "home")
        if s.plan.crash_family == 4:
            s.persona["cache_schema_version"] = 3
        fault(s, fatal=True)
    elif stage == "content" and s.plan.client_fault == "blockage":
        blockage(s)
    elif stage == "content" and s.plan.primary == "handled" and not getattr(plan, "handled_done", False):
        fault(s)
        mark(s, "local_cache_rebuilt")


def done(s):
    rate = getattr(s.plan, "handled_followup", .6)
    if s.plan.primary == "mobile" and s.rng.random() < rate:
        if hasattr(s.plan, "fault_ids"):
            # V2: the screen catches the failed response itself and logs it as a handled exception.
            followup(s)
        else:
            # A concrete response-mapping defect is caught by the UI's error boundary.
            fault(s)
    mark(s, "session_exit")
    s.wait(2, 8)
    s.leave()


def payment(s, key):
    mark(s, "payment_method_selected")
    s.type_text(s.rng.randint(8, 18))
    mark(s, "payment_form_validated")
    mark(s, "payment_authorization_started")
    tap(s, "Confirm payment")
    r = request(s, key)
    client_issue(s, "authorization")
    client_issue(s, "callback")
    if r.ok:
        mark(s, "payment_authorization_succeeded")
        return r
    mark(s, "payment_authorization_attempt_failed")
    # Extra taps are observable but a disabled submit button issues no request.
    if s.rng.random() < .4:
        for _ in range(s.rng.randint(2, 3)):
            tap(s, "Confirm payment")
        mark(s, "payment_submit_debounced")
    s.wait(1, 3)
    mark(s, "payment_status_checked" if key == "place_order" else "reservation_status_checked")
    status = request(s, "operation_status")
    if status.data["state"] == "not_submitted" and s.rng.random() < .65:
        mark(s, "payment_retry_requested")
        s.wait(1, 2)
        r = request(s, key)  # same operation ID; the first attempt never acquired a connection
        if r.ok:
            mark(s, "payment_authorization_succeeded")
    elif status.data["state"] == "pending":
        mark(s, "payment_status_pending" if key == "place_order" else "reservation_status_pending")
    return r


def ecommerce(s):
    if (getattr(s.plan, "shape", None) or {}).get("intent"):
        from .population_journeys import retail
        return retail(s)
    from .profiles.ecommerce import PRODUCTS, SEARCHES
    plan, rng = s.plan, s.rng
    priority = plan.primary == "mobile" and not getattr(plan, "transport", None) or rng.random() < plan.settings.get("journey_weights", {}).get("ecommerce_priority", .23)
    if plan.primary == "backend":
        priority = rng.random() < plan.settings.get("journey_weights", {}).get("ecommerce_priority", .23)
    priority |= plan.client_fault == "crash" and plan.crash_family in (0, 1, 2)
    shape = getattr(plan, "shape", None)
    if shape is not None:
        priority = shape["priority"]
    plan.target = {"payments-db-pool": "place_order", "payment-timeouts": "place_order",
                   "inventory-sync": "add_to_cart", "reviews-n-plus-one": "get_product",
                   "inventory-deadlock": "place_order",
                   "recs-latency": "get_recommendations"}.get(plan.incident)
    if getattr(plan, "transport", None):
        plan.transport_target = plan.transport_target or "search"
    product = rng.choice(PRODUCTS)
    sku, name, category, _ = product
    begin(s, "home")
    request(s, "home")
    mark(s, "home_content_rendered")
    if (shape["short"] if shape is not None else plan.primary == "clean" and not plan.client_fault and rng.random() < .35):
        request(s, "get_recommendations", sku="home")
        mark(s, "recommendations_displayed")
        s.wait(4, 14)
        return done(s)
    s.wait(2, 5)
    mark(s, "search_field_focused")
    query = rng.choice(SEARCHES)
    s.type_text(len(query))
    mark(s, "search_query_entered")
    mark(s, "product_search_submitted")
    s.page_view("search", q=query)
    request(s, "search", q=query)
    mark(s, "product_results_displayed")
    s.wait(2, 8)
    if rng.random() < .3:
        tap(s, "Sort by customer rating")
        mark(s, "product_sort_changed")
    tap(s, name)
    s.page_view("product", sku=sku)
    request(s, "get_product", sku=sku)
    mark(s, "product_details_viewed")
    mark(s, "product_image_expanded")
    mark(s, "reviews_expanded")
    mark(s, "review_media_loaded")
    r = request(s, "get_recommendations", sku=sku)
    mark(s, "default_recommendations_displayed" if r.data.get("fallback") else "recommendations_displayed")
    s.wait(4, 15)
    if r.data.get("fallback"):
        tap(s, "Back to search results")
        mark(s, "product_sort_changed")
    client_issue(s)
    if priority or (shape["add"] if shape is not None else plan.primary == "handled" or rng.random() < .3):
        tap(s, "Add to bag")
        r = request(s, "add_to_cart", sku=sku)
        if not r.ok:
            mark(s, "cart_unavailable_item_removed")
            # A replacement is checked independently; no false add success.
            if rng.random() < .6:
                sku = rng.choice([p[0] for p in PRODUCTS if p[0] != sku])
                request(s, "get_product", sku=sku)
                r = request(s, "add_to_cart", sku=sku)
        if r.ok:
            mark(s, "cart_item_added")
        else:
            return done(s)
        if priority:
            s.page_view("cart")
            mark(s, "cart_opened")
            request(s, "get_cart")
            mark(s, "cart_totals_rendered")
            client_issue(s, "cart")
            s.wait(2, 6)
            tap(s, "Check out")
            s.page_view("checkout")
            mark(s, "checkout_started")
            request(s, "shipping_options")
            mark(s, "shipping_options_displayed")
            s.type_text(rng.randint(15, 30))
            mark(s, "shipping_details_confirmed")
            r = payment(s, "place_order")
            if r.ok:
                s.page_view("confirmation", order_id=r.data["order_id"])
                request(s, "get_order", order_id=r.data["order_id"])
                mark(s, "order_details_loaded")
                mark(s, "order_confirmation_viewed")
            else:
                mark(s, "checkout_cancelled")
    done(s)


def banking(s):
    from .profiles.banking import ACCOUNTS
    plan, rng = s.plan, s.rng
    shape = getattr(plan, "shape", None)
    if (shape["short"] if shape is not None else
            plan.primary == "clean" and not plan.client_fault and plan.ring == "stable" and rng.random() < .42):
        begin(s, "cards")
        mark(s, "card_controls_opened")
        mark(s, "card_status_displayed")
        s.wait(3, 6)
        tap(s, "Lock card")
        mark(s, "card_lock_selected")
        r = request(s, "lock_card")
        if r.ok:
            mark(s, "card_lock_confirmed")
        s.wait(2, 5)
        mark(s, "card_help_viewed")
        return done(s)
    priority = plan.primary == "mobile" and not getattr(plan, "transport", None) or plan.incident == "fraud-latency" or rng.random() < plan.settings.get("journey_weights", {}).get("banking_priority", .18)
    priority |= plan.client_fault == "crash" and plan.crash_family == 1
    statement = plan.incident == "statements-slow" or (not priority and rng.random() < .25)
    if shape is not None:
        priority, statement = shape["priority"], shape["statement"] and not shape["priority"]
    plan.target = {"fraud-latency": "create_transfer", "rails-outage": "create_transfer",
                   "account-read-fallback": "accounts", "core-banking-latency": "accounts",
                   "statements-slow": "statement"}.get(plan.incident)
    if getattr(plan, "transport", None):
        plan.transport_target = plan.transport_target or "accounts"
    begin(s, "signin" if not s.user_id else "dashboard")
    if not s.user_id:
        s.type_text(rng.randint(18, 25))
        mark(s, "credentials_submitted")
        if not request(s, "login").ok:
            s.properties["visit_outcome"] = "signin_failed"
            return done(s)
        s.page_view("mfa")
        mark(s, "identity_challenge_presented")
        s.wait(3, 8)
        if not request(s, "mfa_verify").ok:
            s.properties["visit_outcome"] = "challenge_failed"
            return done(s)
        s.login()
        mark(s, "identity_verified")
    s.page_view("dashboard")
    mark(s, "account_overview_opened")
    account_result = request(s, "accounts")
    if not account_result.ok:
        s.properties["visit_outcome"] = "overview_unavailable"
        return done(s)
    if account_result.data.get("snapshot"):
        mark(s, "cached_account_snapshot_displayed")
        mark(s, "account_snapshot_as_of_displayed")
        s.log("warning", "Account snapshot is read-only; recent activity may be missing", **{
            "account.snapshot_as_of_ms": str(account_result.data["as_of_ms"])})
    mark(s, "account_overview_rendered")
    if shape and shape.get("overview_only"):
        from .population import read
        read(s)
        s.properties["visit_outcome"] = "balance_checked"
        client_issue(s)
        return done(s)
    s.wait(2, 8)
    acct, label = rng.choice(ACCOUNTS)
    tap(s, label)
    s.page_view("account", account=acct, label=label)
    mark(s, "transaction_search_submitted")
    mark(s, "transaction_filter_changed")
    request(s, "transactions", account=acct)
    mark(s, "transaction_results_displayed")
    s.wait(3, 8)
    tap(s, "View transaction details")
    mark(s, "transaction_details_viewed")
    client_issue(s)
    if shape and shape.get("power"):
        from .population import read
        other, other_label = next((a, name) for a, name in ACCOUNTS if a != acct)
        tap(s, other_label)
        s.page_view("account", account=other, label=other_label)
        request(s, "transactions", account=other)
        read(s)
    if priority or shape and shape.get("abandon"):
        tap(s, "Move money")
        s.page_view("transfer")
        mark(s, "transfer_started")
        request(s, "payees")
        mark(s, "recipient_list_displayed")
        tap(s, "Saved recipient")
        mark(s, "recipient_selected")
        s.type_text(rng.randint(3, 6))
        mark(s, "transfer_amount_entered")
        if rng.random() < .3:
            mark(s, "transfer_amount_corrected")
            s.type_text(4)
        mark(s, "transfer_amount_validated")
        s.page_view("transfer_review")
        mark(s, "transfer_review_displayed")
        s.wait(2, 5)
        if shape and shape.get("abandon"):
            s.properties["visit_outcome"] = "transfer_abandoned"
            return done(s)
        tap(s, "Send transfer")
        mark(s, "fraud_check_started")
        r = request(s, "create_transfer")
        client_issue(s, "callback")
        if not r.ok:
            mark(s, "transfer_status_pending")
            status = request(s, "operation_status")
            if status.data["state"] == "not_submitted" and rng.random() < .65:
                mark(s, "request_retry_requested")
                s.wait(1, 3)
                r = request(s, "create_transfer")
        if r.ok:
            s.page_view("transfer_done", transfer_id=r.data["transfer_id"])
            s.properties["visit_outcome"] = "transfer_completed"
            mark(s, "transfer_confirmation_viewed")
        else:
            s.properties["visit_outcome"] = "transfer_unconfirmed"
            mark(s, "transfer_help_opened")
    elif statement:
        s.page_view("statements")
        mark(s, "statement_period_selected")
        mark(s, "statement_download_requested")
        r = request(s, "statement")
        if r.ok and not (plan.incident == "statements-slow" and rng.random() < .2):
            mark(s, "statement_opened")
        else:
            mark(s, "statement_open_cancelled" if r.ok else "statement_download_failed")
    elif account_result.data.get("snapshot"):
        mark(s, "account_refresh_requested")
        request(s, "accounts")
        mark(s, "account_overview_rendered")
    done(s)


def travel(s):
    from .profiles.travel import ROUTES
    plan, rng = s.plan, s.rng
    shape = getattr(plan, "shape", None)
    if (shape["short"] if shape is not None else
            plan.primary == "clean" and not plan.client_fault and plan.ring == "stable" and rng.random() < .42):
        begin(s, "trips")
        mark(s, "trip_list_opened")
        request(s, "trips")
        mark(s, "trip_list_rendered")
        s.wait(4, 8)
        tap(s, "View itinerary")
        mark(s, "trip_details_viewed")
        mark(s, "itinerary_saved")
        s.wait(2, 5)
        mark(s, "offline_itinerary_opened")
        return done(s)
    priority = plan.primary == "mobile" and not getattr(plan, "transport", None) or rng.random() < plan.settings.get("journey_weights", {}).get("travel_priority", .23)
    priority |= plan.client_fault == "crash" and plan.crash_family in (0, 1)
    hotel = plan.incident == "hotel-supplier-outage" or (not priority and plan.incident not in ("supplier-timeouts", "search-complexity") and rng.random() < .3)
    if shape is not None:
        priority, hotel = shape["priority"], shape["hotel"] and not shape["priority"]
    plan.target = {"payments-db-pool": "create_booking", "gds-pnr-timeouts": "create_booking",
                   "supplier-timeouts": "flight_search", "search-complexity": "flight_search",
                   "hotel-supplier-outage": "hotel_search"}.get(plan.incident)
    if getattr(plan, "transport", None):
        plan.transport_target = plan.transport_target or ("hotel_search" if hotel else "flight_search")
    origin, dest, from_city, city = rng.choice(ROUTES)
    begin(s, "home")
    s.wait(2, 5)
    if hotel:
        tap(s, "Stays")
        mark(s, "hotel_search_opened")
        s.type_text(len(city))
        request(s, "locations", q=city[:3])
        mark(s, "hotel_destination_selected")
        tap(s, "Check-in and check-out dates")
        mark(s, "check_in_date_selected")
        mark(s, "check_out_date_selected")
        tap(s, "2 adults, 1 room")
        mark(s, "guest_count_selected")
        mark(s, "hotel_search_criteria_reviewed")
        mark(s, "hotel_search_submitted")
        s.page_view("hotels", city=city)
        r = request(s, "hotel_search", city=city)
        mark(s, "hotel_results_displayed")
        if r.data.get("cached"):
            mark(s, "cached_hotel_rates_displayed")
            mark(s, "hotel_rate_freshness_displayed")
        s.wait(3, 8)
        tap(s, "View hotel")
        mark(s, "hotel_details_viewed")
        mark(s, "hotel_selected")
        r = request(s, "hotel_revalidate")
        mark(s, "hotel_rate_revalidated" if r.ok else "hotel_rate_unavailable")
        client_issue(s)
        return done(s)
    mark(s, "flight_search_opened")
    tap(s, "Round trip")
    mark(s, "trip_type_selected")
    s.type_text(len(from_city))
    mark(s, "flight_origin_entered")
    request(s, "locations", q=from_city[:3])
    s.type_text(len(city))
    mark(s, "flight_destination_entered")
    request(s, "locations", q=city[:3])
    tap(s, "Departure and return dates")
    mark(s, "departure_date_selected")
    mark(s, "return_date_selected")
    tap(s, "2 passengers, economy")
    mark(s, "passenger_count_selected")
    mark(s, "cabin_selected")
    mark(s, "flight_search_criteria_reviewed")
    mark(s, "flight_search_submitted")
    s.page_view("flights", origin=origin, dest=dest, **{"from": from_city, "to": city})
    r = request(s, "flight_search", origin=origin, dest=dest)
    mark(s, "partial_flight_results_displayed" if r.data.get("partial") else "flight_results_displayed")
    mark(s, "flight_result_count_displayed")
    s.wait(4, 14)
    if rng.random() < .4:
        tap(s, "Earlier departures")
        mark(s, "flight_departure_filter_changed")
    client_issue(s)
    if shape and shape.get("power"):
        tap(s, "Compare flight options")
        request(s, "flight_search", origin=origin, dest=dest)
        from .population import read
        read(s)
    if priority or (shape["offer"] if shape is not None else rng.random() < .5):
        tap(s, "Select itinerary")
        mark(s, "itinerary_selected")
        s.page_view("offer")
        request(s, "offer")
        mark(s, "fare_rules_displayed")
        mark(s, "fare_revalidated")
        s.wait(3, 8)
    if priority or shape and shape.get("abandon"):
        s.page_view("checkout")
        mark(s, "booking_started")
        s.type_text(rng.randint(25, 50))
        mark(s, "traveler_details_entered")
        if rng.random() < .3:
            s.type_text(5)
            mark(s, "traveler_details_corrected")
        mark(s, "traveler_details_validated")
        mark(s, "booking_review_displayed")
        if shape and shape.get("abandon"):
            s.properties["visit_outcome"] = "booking_abandoned"
            return done(s)
        r = payment(s, "create_booking")
        if r.ok:
            s.properties["visit_outcome"] = "booking_completed"
            s.page_view("confirmation", ref=r.data["ref"])
            mark(s, "booking_confirmation_viewed")
            request(s, "trips")
        else:
            s.properties["visit_outcome"] = "booking_unconfirmed"
            mark(s, "booking_cancelled")
    done(s)


JOURNEYS = {"ecommerce": ecommerce, "banking": banking, "travel": travel}
