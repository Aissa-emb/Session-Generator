"""Retail visitor intents executed through the existing native/web adapters."""
from .population import read


def retail(s):
    from .journeys import begin, request, tap, client_issue, payment, done
    from .milestones import mark
    from .profiles.ecommerce import PRODUCTS, SEARCHES
    shape, rng = s.plan.shape, s.rng
    s.plan.target = {"payments-db-pool": "place_order", "payment-timeouts": "place_order",
                     "inventory-sync": "add_to_cart", "reviews-n-plus-one": "get_product",
                     "inventory-deadlock": "place_order", "recs-latency": "get_recommendations"}.get(s.plan.incident)
    if getattr(s.plan, "transport", None):
        s.plan.transport_target = s.plan.transport_target or "search"
    begin(s, "home")
    request(s, "home")
    mark(s, "home_content_rendered")
    if shape.get("bounce"):
        read(s, 2, 5)
        s.properties["visit_outcome"] = "bounced"
        mark(s, "session_exit")
        return s.leave()
    read(s)
    if shape.get("search"):
        query = rng.choice(SEARCHES)
        tap(s, "Search")
        s.type_text(len(query))
        mark(s, "product_search_submitted")
        s.page_view("search", q=query)
        request(s, "search", q=query)
        mark(s, "product_results_displayed")
        read(s)
    elif shape.get("category"):
        category = rng.choice(PRODUCTS)[2]
        tap(s, "Browse category")
        s.page_view("category", category=category)
        request(s, "list_products", category=category)
        read(s)
    count = shape["visits"]
    items = rng.sample(PRODUCTS, min(count, len(PRODUCTS)))
    to_add = min(len(items), rng.randint(2, 3)) if shape["power"] else 1
    added = 0
    for i, (sku, name, _, _) in enumerate(items):
        tap(s, name)
        s.page_view("product", sku=sku)
        result = request(s, "get_product", sku=sku)
        if not result.ok:
            s.properties["visit_outcome"] = "product_unavailable"
            return done(s)
        mark(s, "product_details_viewed")
        request(s, "get_recommendations", sku=sku)
        read(s)
        if i == 0:
            client_issue(s)
        if (shape["priority"] or shape["add"]) and i < to_add:
            tap(s, "Add to bag")
            result = request(s, "add_to_cart", sku=sku)
            if result.ok:
                added += 1
                mark(s, "cart_item_added")
            else:
                s.properties["visit_outcome"] = "cart_unavailable"
                return done(s)
    if added:
        tap(s, "View bag")
        s.page_view("cart")
        mark(s, "cart_opened")
        request(s, "get_cart")
        client_issue(s, "cart")
        read(s)
        if not shape["priority"]:
            s.properties["visit_outcome"] = "cart_abandoned"
            return done(s)
        tap(s, "Check out")
        s.page_view("checkout")
        mark(s, "checkout_started")
        request(s, "shipping_options")
        s.type_text(rng.randint(15, 30))
        mark(s, "shipping_details_confirmed")
        result = payment(s, "place_order")
        if result.ok:
            s.page_view("confirmation", order_id=result.data["order_id"])
            request(s, "get_order", order_id=result.data["order_id"])
            s.properties["visit_outcome"] = "order_completed"
            mark(s, "order_confirmation_viewed")
        else:
            s.properties["visit_outcome"] = "checkout_failed"
    else:
        s.properties["visit_outcome"] = "browsed"
    done(s)
