"""Child spans an app team records under its business root spans.

The SDKs only time what the app instruments: a root span from ``Embrace.startSpan`` (Android), ``buildSpan``
(Apple) or the OTel tracer (Web) and children started with that root as parent. Network request spans stay
separate root spans in the SDK (Android parents them only when the app sets a traceparent header itself), so the
child that wraps the request is the app's own call around it. Each plan lists local work before the request,
the request child, and local work after a successful response, with millisecond ranges for a flagship device.
"""
from .util import lat

# operation key -> (local work before, request child, local work after); work = (name, lo_ms, hi_ms, kind)
PLANS = {
    "search": ([("read_recent_searches", 2, 9, "db")], "fetch_search_results",
               [("decode_search_results", 4, 28, "decode"), ("render_search_results", 9, 46, "render")]),
    "get_product": ([("read_product_cache", 2, 14, "db")], "fetch_product",
                    [("decode_product", 3, 20, "decode"), ("render_product", 10, 52, "render")]),
    "get_recommendations": ([], "fetch_recommendations",
                            [("decode_recommendations", 4, 30, "decode"), ("render_recommendations", 8, 40, "render")]),
    "add_to_cart": ([("validate_cart_change", 1, 6, "cpu")], "submit_cart_change",
                    [("persist_cart", 3, 18, "db"), ("render_cart_badge", 2, 12, "render")]),
    "get_cart": ([("read_cart_cache", 2, 16, "db")], "fetch_cart",
                 [("decode_cart", 3, 22, "decode"), ("render_cart", 8, 40, "render")]),
    "shipping_options": ([("read_saved_addresses", 2, 12, "db")], "fetch_shipping_quotes",
                         [("render_shipping_options", 6, 30, "render")]),
    "place_order": ([("tokenize_payment_method", 6, 40, "cpu")], "submit_payment_authorization",
                    [("persist_order_receipt", 3, 20, "db"), ("render_order_status", 6, 30, "render")]),
    "get_order": ([], "fetch_order", [("decode_order", 2, 14, "decode"), ("render_order_confirmation", 8, 38, "render")]),
    "accounts": ([("read_account_snapshot", 3, 20, "db")], "fetch_accounts",
                 [("decode_accounts", 3, 24, "decode"), ("persist_account_snapshot", 4, 26, "db"),
                  ("render_accounts", 8, 42, "render")]),
    "transactions": ([("read_transaction_cache", 3, 24, "db")], "fetch_transactions",
                     [("decode_transactions", 5, 40, "decode"), ("render_transactions", 10, 54, "render")]),
    "payees": ([("read_payee_cache", 2, 12, "db")], "fetch_payees", [("render_payees", 5, 26, "render")]),
    "create_transfer": ([("validate_transfer", 1, 6, "cpu"), ("sign_transfer_request", 8, 34, "cpu")],
                        "submit_transfer_request",
                        [("persist_transfer_receipt", 3, 18, "db"), ("render_transfer_status", 5, 26, "render")]),
    "statement": ([], "fetch_statement_document",
                  [("write_statement_file", 12, 90, "io"), ("render_statement_preview", 20, 110, "render")]),
    "lock_card": ([("confirm_device_credential", 30, 140, "cpu")], "submit_card_lock",
                  [("update_card_state", 2, 12, "db")]),
    "flight_search": ([("read_recent_routes", 2, 10, "db")], "fetch_flight_offers",
                      [("decode_flight_offers", 8, 60, "decode"), ("sort_flight_offers", 3, 22, "cpu"),
                       ("render_flight_results", 10, 56, "render")]),
    "hotel_search": ([], "fetch_hotel_offers",
                     [("decode_hotel_offers", 6, 44, "decode"), ("render_hotel_results", 10, 52, "render")]),
    "hotel_revalidate": ([], "fetch_hotel_rate", [("apply_rate_change", 2, 12, "cpu")]),
    "offer": ([("read_fare_cache", 2, 12, "db")], "fetch_fare_offer",
              [("decode_fare_rules", 3, 22, "decode"), ("render_fare_review", 6, 34, "render")]),
    "create_booking": ([("tokenize_payment_method", 6, 40, "cpu")], "submit_booking",
                       [("persist_itinerary", 4, 24, "db"), ("render_booking_status", 6, 30, "render")]),
    "trips": ([("read_trip_cache", 3, 20, "db")], "fetch_itineraries",
              [("decode_itineraries", 4, 30, "decode"), ("render_itineraries", 8, 40, "render")]),
    "operation_status": ([], "fetch_operation_status", [("update_operation_state", 2, 12, "db")]),
    "login": ([("read_device_key", 4, 22, "io")], "submit_credentials", [("store_session_tokens", 3, 16, "db")]),
    "mfa_verify": ([], "submit_challenge_response", [("store_session_tokens", 3, 16, "db")]),
}
# Platform speed of local work relative to the Android numbers above.
PLATFORM_FACTOR = {"android": 1.0, "ios": .75, "web": .85}


def _speed(s):
    if s.persona.get("population_model") and s.persona.get("cpu_factor"):
        return s.persona["cpu_factor"]
    if hasattr(s, "_cpu_slowdown"):
        return s._cpu_slowdown()
    return 1.0


class Phases:
    """Records a plan's children around the request; the journey calls before(), the request, then after()."""

    def __init__(self, s, key, holder):
        self.s, self.holder = s, holder
        self.plan = PLANS.get(key) if hasattr(s, "child_span") else None
        platform = getattr(s, "platform", None) or ("web" if hasattr(s, "page") else "android")
        self.scale = _speed(s) * PLATFORM_FACTOR.get(platform, 1.0)
        self.request_start = None

    def _work(self, name, lo, hi, extra=0.0, **attrs):
        start = self.s.t
        self.s.t = start + lat(lo, hi, self.s.rng) * self.scale + extra
        self.s.child_span(self.holder, name, start, self.s.t, attrs=attrs)
        self.s.t += self.s.rng.uniform(.2, 2)

    def before(self):
        if not self.plan:
            return
        for name, lo, hi, _ in self.plan[0]:
            self._work(name, lo, hi)
        self.request_start = self.s.t
        self.s.t += self.s.rng.uniform(.3, 2.5)

    def after(self, result, extra=0.0):
        """result: the request Result. ``extra`` lengthens the heaviest local step (a slow root span)."""
        if not self.plan:
            return
        s = self.s
        start = self.request_start if self.request_start is not None else result.start
        end = result.end + s.rng.uniform(.4, 3)
        s.child_span(self.holder, self.plan[1], start, end, failed=not result.ok)
        if not result.ok:
            s.t = max(s.t, end + s.rng.uniform(.2, 1.5))
            return
        resume = s.t
        s.t = end + s.rng.uniform(.5, 4)
        work = self.plan[2]
        heavy = max(range(len(work)), key=lambda i: work[i][2]) if work else None
        size = getattr(result, "data", {}) or {}
        for i, (name, lo, hi, kind) in enumerate(work):
            grow = 1.0
            if kind == "decode" and isinstance(size, dict) and size.get("items"):
                grow = min(2.5, 1 + len(size["items"]) / 40)
            self._work(name, lo * grow, hi * grow, extra if i == heavy else 0.0)
        s.t = max(s.t, resume)
