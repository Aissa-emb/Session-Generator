"""Stable application breadcrumbs and User Flow definitions (not custom spans)."""
FLOWS = {
    "ecommerce": [
        ("Product discovery", "product_search_submitted", "product_details_viewed", 90),
        ("Product to cart", "product_details_viewed", "cart_item_added", 120),
        ("Checkout completion", "checkout_started", "order_confirmation_viewed", 180),
        ("Payment authorization", "payment_authorization_started", "payment_authorization_succeeded", 45)],
    "banking": [
        ("Account overview", "account_overview_opened", "account_overview_rendered", 30),
        ("Transaction lookup", "transaction_search_submitted", "transaction_details_viewed", 90),
        ("Transfer completion", "transfer_started", "transfer_confirmation_viewed", 120),
        ("Statement retrieval", "statement_download_requested", "statement_opened", 45)],
    "travel": [
        ("Flight selection", "flight_search_submitted", "itinerary_selected", 120),
        ("Hotel selection", "hotel_search_submitted", "hotel_selected", 120),
        ("Booking completion", "booking_started", "booking_confirmation_viewed", 240),
        ("Payment authorization", "payment_authorization_started", "payment_authorization_succeeded", 45)]}

EXTRA = """app_foregrounded session_resumed home_content_rendered search_field_focused search_query_entered
product_results_displayed product_sort_changed product_image_expanded reviews_expanded review_media_loaded
recommendations_displayed default_recommendations_displayed cart_opened cart_totals_rendered cart_quantity_updated
cart_unavailable_item_removed checkout_cancelled shipping_options_displayed shipping_details_confirmed
payment_method_selected payment_form_validated payment_authorization_attempt_failed payment_retry_requested
payment_status_checked payment_status_pending payment_submit_debounced order_details_loaded
credentials_submitted identity_challenge_presented identity_verified authentication_reused
account_refresh_requested cached_account_snapshot_displayed account_snapshot_as_of_displayed
transaction_filter_changed transaction_results_displayed transaction_selection_changed
recipient_list_displayed recipient_selected transfer_amount_entered transfer_amount_corrected
transfer_amount_validated transfer_review_displayed fraud_check_started transfer_status_pending transfer_help_opened
statement_period_selected statement_download_retried statement_open_cancelled statement_download_failed
flight_origin_entered flight_destination_entered flight_results_displayed partial_flight_results_displayed
flight_search_opened trip_type_selected departure_date_selected return_date_selected passenger_count_selected
cabin_selected flight_search_criteria_reviewed flight_result_count_displayed
hotel_search_opened hotel_destination_selected check_in_date_selected check_out_date_selected
guest_count_selected hotel_search_criteria_reviewed
flight_departure_filter_changed fare_rules_displayed fare_revalidated booking_cancelled
traveler_details_entered traveler_details_corrected traveler_details_validated booking_review_displayed
reservation_status_checked reservation_status_pending hotel_results_displayed cached_hotel_rates_displayed
hotel_rate_freshness_displayed hotel_details_viewed hotel_rate_revalidated hotel_rate_unavailable
request_retry_requested connectivity_restored response_validation_failed local_cache_rebuilt
app_backgrounded callback_arrived_after_navigation selection_changed_during_refresh localized_value_received
cache_upgrade_started main_thread_work_started main_thread_responsive support_opened session_exit
card_controls_opened card_status_displayed card_lock_selected card_lock_confirmed card_help_viewed
trip_list_opened trip_list_rendered trip_details_viewed itinerary_saved offline_itinerary_opened
""".split()
REQUEST_MILESTONES = {
    "home": "home_catalog", "search": "product_results", "get_product": "product_data",
    "get_recommendations": "recommendation_data", "add_to_cart": "cart_item_update",
    "get_cart": "cart_summary", "shipping_options": "shipping_quote", "place_order": "order_submission",
    "get_order": "order_receipt", "login": "identity_verification", "mfa_verify": "challenge_verification",
    "accounts": "account_balances", "transactions": "transaction_history", "payees": "saved_recipients",
    "create_transfer": "transfer_submission", "statement": "statement_document", "lock_card": "card_lock_update",
    "locations": "location_suggestions", "flight_search": "flight_offers", "hotel_search": "hotel_rates",
    "hotel_revalidate": "hotel_rate_validation", "offer": "fare_quote", "create_booking": "reservation_submission",
    "trips": "saved_itineraries", "operation_status": "operation_reconciliation",
}
CATALOG = (set(EXTRA) | {anchor for flows in FLOWS.values() for _, start, end, _ in flows for anchor in (start, end)} |
           {f"{prefix}_{suffix}" for prefix in REQUEST_MILESTONES.values()
            for suffix in ("requested", "received", "failed")})

# Keep native User Flow anchors and only the context that changes the story.
# Loading/field/retry-attempt detail belongs in task spans and network records.
ANCHORS = {anchor for flows in FLOWS.values() for _, start, end, _ in flows for anchor in (start, end)}
SPARSE_CONTEXT = set("""default_recommendations_displayed
partial_flight_results_displayed cached_hotel_rates_displayed payment_authorization_attempt_failed
payment_retry_requested transfer_status_pending reservation_status_pending checkout_cancelled
booking_cancelled statement_open_cancelled card_lock_confirmed trip_details_viewed""".split())


def mark(s, name):
    if name not in CATALOG:
        raise ValueError(f"Unknown milestone: {name}")
    if name not in ANCHORS and name not in SPARSE_CONTEXT:
        return
    if not hasattr(s, "milestones"):
        s.milestones = []
    s.breadcrumb(name)
    s.milestones.append({"name": name, "time_ms": s.t, "part_id": s.part.id})


def definitions(app_id):
    return [{"name": name, "profile": profile, "business_unit": "retail" if profile == "ecommerce" else profile,
             "app_id": app_id, "start_type": "Breadcrumb", "start": start, "end_type": "Breadcrumb", "end": end,
             "timeout_seconds": timeout, "ignore_restarts_while_in_progress": True}
            for profile, flows in FLOWS.items() for name, start, end, timeout in flows]
