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
statement_period_selected statement_download_retried statement_download_cancelled
flight_origin_entered flight_destination_entered flight_results_displayed partial_flight_results_displayed
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
CATALOG = set(EXTRA) | {anchor for flows in FLOWS.values() for _, start, end, _ in flows for anchor in (start, end)}


def mark(s, name):
    if name not in CATALOG:
        raise ValueError(f"Unknown milestone: {name}")
    if not hasattr(s, "milestones"):
        s.milestones = []
    s.breadcrumb(name)
    s.milestones.append({"name": name, "time_ms": s.t, "part_id": s.part.id})


def definitions(app_id):
    return [{"name": name, "profile": profile, "business_unit": "retail" if profile == "ecommerce" else profile,
             "app_id": app_id, "start_type": "Breadcrumb", "start": start, "end_type": "Breadcrumb", "end": end,
             "timeout_seconds": timeout, "ignore_restarts_while_in_progress": True}
            for profile, flows in FLOWS.items() for name, start, end, timeout in flows]
