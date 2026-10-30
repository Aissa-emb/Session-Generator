"""The Vantra web estate: sites, routes, page labels, page groups, releases and page resources.

URL schemes are designed for the backend's URL rollup (go/lib/grouper/classify.go):
segments with two or more dashes collapse as slugs, hex strings longer than
12 characters collapse, numbers and dates collapse, and other words collapse
only after 25 distinct values. Surfaces carry route templates
(setCurrentRoute), because surface names are not rolled up and Pages health
counts views per template.

Journeys drive pages by key (the same keys as the Android screens); a page's
"render" says whether the site serves it as a full page load (server) or a
client-side route change (client). Retail is multi-page, banking a
single-page app and travel a hybrid on one origin: landing and search pages
are server-rendered, the booking flow is client-routed.
"""
import hashlib
import re

from .profiles.ecommerce import PRODUCTS, CATEGORIES

BRAND = {"retail": "Vantra", "banking": "Vantra Money", "travel": "Vantra Travel"}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def hex_id(*parts, n=16):
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:n]


def content_hash(*parts):
    """Vite-style 8-character content hash, stable within a release."""
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:8]


def product(sku):
    return next(p for p in PRODUCTS if p[0] == sku)


class Page:
    def __init__(self, key, route, group, render, label, path=None, lcp=None, cls=None, doc_height=2400,
                 elements=(), measures=(), chunk=None, controls=()):
        self.key, self.route, self.group, self.render = key, route, group, render
        self._label, self._path = label, path
        # lcp: (CSS selector, element type, image path function or None)
        self.lcp = lcp or ("main h1", "h1", None)
        self.cls = cls or "main"
        self.doc_height, self.elements, self.measures = doc_height, elements, measures
        self.chunk = chunk or key
        self.controls = controls

    def label(self, params, lob):
        text = self._label(params) if callable(self._label) else self._label
        return f"{text} | {BRAND[lob]}"

    def path(self, params):
        return self._path(params) if self._path else self.route


def _product_path(p):
    sku = p["sku"]
    return f"/p/{slug(product(sku)[1])}/{sku}"


RETAIL = {
    "home": Page("home", "/", "Home", "server", "Outdoor gear and apparel",
                 lcp=("main>section.hero>picture>img", "img", lambda p: "/images/campaigns/fall-2026/hero-1600w.avif"),
                 cls="div.promo-banner", doc_height=4200, elements=(("hero-image", "img"),),
                 controls=(("a", "nav-link", "Outerwear"), ("button", "search-trigger", "Search"))),
    "category": Page("category", "/c/:category", "Category", "server", lambda p: CATEGORIES[p["category"]],
                     path=lambda p: f"/c/{p['category']}",
                     lcp=("ul.product-grid>li:nth-child(1)>a>img", "img", lambda p: "/images/products/1051027/card-480w.webp"),
                     cls="ul.product-grid", doc_height=5200),
    "search": Page("search", "/search", "Search", "server", lambda p: f"Search results for {p['q']}",
                   path=lambda p: "/search?q=" + p["q"].replace(" ", "+"),
                   lcp=("ul.product-grid>li:nth-child(1)>a>img", "img", lambda p: "/images/products/1055601/card-480w.webp"),
                   cls="ul.product-grid", doc_height=3800, measures=("search:results-rendered",)),
    "product": Page("product", "/p/:slug/:productId", "Product", "server", lambda p: product(p["sku"])[1],
                    path=_product_path,
                    lcp=("div.product-gallery>picture>img.hero", "img",
                         lambda p: f"/images/products/{p['sku']}/hero-1600w.avif"),
                    cls="section.product-reviews", doc_height=3600,
                    elements=(("hero-image", "img"), ("price-block", "div")), measures=("product:gallery-ready",)),
    "cart": Page("cart", "/cart", "Cart", "server", "Your bag", lcp=("main>h1.cart-title", "h1", None), doc_height=1500),
    "signin": Page("signin", "/account/sign-in", "Account", "server", "Sign in", lcp=("form.sign-in>h1", "h1", None),
                   doc_height=1100),
    "checkout": Page("checkout", "/checkout", "Checkout", "server", "Checkout", lcp=("form.checkout>h2", "h2", None),
                     cls="div.payment-element", doc_height=2100, measures=("checkout:payment-sheet-ready",)),
    "confirmation": Page("confirmation", "/checkout/confirmation/:orderId", "Checkout", "server", "Order confirmed",
                         path=lambda p: "/checkout/confirmation/" + p["order_id"].replace("-", ""),
                         lcp=("div.confirmation>h1", "h1", None), doc_height=1600),
}

BANKING = {
    "signin": Page("signin", "/login", "Sign in", "client", "Sign in", lcp=("form#login>h1", "h1", None), doc_height=1200),
    "mfa": Page("mfa", "/login/verify", "Sign in", "client", "Verify it's you", lcp=("div.mfa-card>h2", "h2", None),
                doc_height=1000),
    "dashboard": Page("dashboard", "/accounts", "Account", "client", "Accounts", lcp=("section.balances>h2", "h2", None),
                      cls="section.offers-carousel", doc_height=2600, elements=(("balance-summary", "section"),),
                      measures=("accounts:balances-rendered",)),
    "account": Page("account", "/accounts/:accountId", "Account", "client", lambda p: p["label"].split(" ••")[0],
                    path=lambda p: f"/accounts/{hex_id('account', p['account'], p.get('_user', ''))}",
                    lcp=("table.transactions", "table", None), doc_height=4800,
                    measures=("transactions:list-rendered",)),
    "transfer": Page("transfer", "/move-money", "Transfers", "client", "Move money", lcp=("form.transfer>h1", "h1", None),
                     doc_height=1700),
    "transfer_review": Page("transfer_review", "/move-money/review", "Transfers", "client", "Review transfer",
                            lcp=("div.review-card", "div", None), doc_height=1300),
    "transfer_done": Page("transfer_done", "/move-money/confirmation/:transferId", "Transfers", "client", "Transfer sent",
                          path=lambda p: "/move-money/confirmation/" + hex_id("transfer", p["transfer_id"]),
                          lcp=("div.confirmation>h1", "h1", None), doc_height=1100),
    "billpay": Page("billpay", "/bills", "Transfers", "client", "Pay bills", lcp=("ul.payees", "ul", None), doc_height=2200),
    "statements": Page("statements", "/statements", "Statements", "client", "Statements and documents",
                       lcp=("table.statements", "table", None), doc_height=1800),
    "cards": Page("cards", "/cards", "Account", "client", "Cards",
                  lcp=("div.card-art>img", "img", lambda p: "/static/media/card-art-cash-rewards.png"), doc_height=1600),
}

TRAVEL = {
    "home": Page("home", "/travel", "Home", "server", "Flights, hotels and trips",
                 lcp=("main>section.hero>picture>img", "img", lambda p: "/images/travel/destinations/lisbon-coast-1600w.avif"),
                 cls="div.promo-banner", doc_height=3900, elements=(("hero-image", "img"),)),
    "flights": Page("flights", "/travel/flights/search", "Search", "server", lambda p: f"{p['from']} to {p['to']} flights",
                    path=lambda p: f"/travel/flights/search?from={p['origin']}&to={p['dest']}&depart=2026-11-21&return=2026-11-28&adults=2",
                    lcp=("ol.results>li:nth-child(1)>div.fare-card", "div", None), cls="ol.results", doc_height=6400,
                    elements=(("fare-summary", "div"),), measures=("flights:results-rendered",)),
    "hotels": Page("hotels", "/travel/hotels/search", "Search", "server", lambda p: f"Hotels in {p['city']}",
                   path=lambda p: "/travel/hotels/search?city=" + p["city"].replace(" ", "+") + "&checkin=2026-11-21&nights=3",
                   lcp=("ol.hotel-results>li:nth-child(1)>picture>img", "img",
                        lambda p: f"/images/travel/hotels/{slug(p['city'])}-featured-960w.webp"),
                   cls="div.map-panel", doc_height=5800, measures=("hotels:results-rendered",)),
    "offer": Page("offer", "/travel/book/:offerId", "Checkout", "client", "Review your trip",
                  path=lambda p: "/travel/book/" + hex_id("offer", p.get("_user", ""), p.get("_session", "")),
                  lcp=("div.itinerary-summary", "div", None), doc_height=2600),
    "checkout": Page("checkout", "/travel/book/:offerId/travelers", "Checkout", "client", "Traveler details and payment",
                     path=lambda p: "/travel/book/" + hex_id("offer", p.get("_user", ""), p.get("_session", "")) + "/travelers",
                     lcp=("form.travelers>h2", "h2", None), cls="div.payment-iframe", doc_height=3100,
                     measures=("checkout:payment-sheet-ready",)),
    "confirmation": Page("confirmation", "/travel/trips/:bookingRef", "Account", "client", "Booking confirmed",
                         path=lambda p: f"/travel/trips/{p['ref']}", lcp=("div.confirmation>h1", "h1", None), doc_height=1900),
    "trips": Page("trips", "/travel/trips", "Account", "client", "My trips", lcp=("ul.trip-cards", "ul", None), doc_height=2000),
}

SITES = {"retail": RETAIL, "banking": BANKING, "travel": TRAVEL}
# Where a line of business lives on the estate (spec targets override the origins).
APP_SHELL = {"retail": "storefront", "banking": "money", "travel": "travel"}

# Page Group rules for the dashboard (regex over browser.url.full), generated into the setup kit.
PAGE_GROUP_RULES = [
    ("Home", r"^https://www\.vantra\.com/(travel/?)?(\?.*)?$"),
    ("Category", r"^https://www\.vantra\.com/c/[^/?]+"),
    ("Product", r"^https://www\.vantra\.com/p/[^/]+/\d+"),
    ("Search", r"^https://www\.vantra\.com/(search|travel/(flights|hotels)/search)\b"),
    ("Cart", r"^https://www\.vantra\.com/cart\b"),
    ("Checkout", r"^https://www\.vantra\.com/(checkout|travel/book)\b"),
    ("Account", r"^https://(www\.vantra\.com/(account|travel/trips)|app\.vantra\.com/(accounts|cards))\b"),
    ("Sign in", r"^https://app\.vantra\.com/login\b"),
    ("Transfers", r"^https://app\.vantra\.com/(move-money|bills)\b"),
    ("Statements", r"^https://app\.vantra\.com/statements\b"),
]

# Third-party tags present on each site. timing: whether the vendor sends Timing-Allow-Origin.
THIRD_PARTY = {
    "tag_manager": {"url": "https://www.googletagmanager.com/gtm.js?id=GTM-K7Q2M9X", "initiator": "script",
                    "size": 98_000, "timing": False, "sites": ("retail", "travel", "banking")},
    "analytics": {"url": "https://www.googletagmanager.com/gtag/js?id=G-4XJ8R2QW1N", "initiator": "script",
                  "size": 141_000, "timing": False, "sites": ("retail", "travel")},
    "chat_widget": {"url": "https://widget.intercom.io/widget/q8r3f2z1", "initiator": "script", "size": 24_000,
                    "timing": False, "sites": ("retail", "travel")},
    "ab_testing": {"url": "https://cdn.optimizely.com/js/24813690571.js", "initiator": "script", "size": 86_000,
                   "timing": True, "blocking": True, "sites": ("retail",)},
}
# Script URLs that long animation frames are attributed to, per third-party tag.
THIRD_PARTY_SCRIPTS = {"tag_manager": THIRD_PARTY["tag_manager"]["url"],
                       "analytics": THIRD_PARTY["analytics"]["url"],
                       "chat_widget": "https://js.intercomcdn.com/frame-modern.d6a8bd4c.js",
                       "ab_testing": THIRD_PARTY["ab_testing"]["url"]}


def first_party(cdn, lob, release, page):
    """First-party assets of one page load: (url, initiator, encoded size, decoded/encoded ratio, blocking)."""
    shell = APP_SHELL[lob]
    h = lambda name: content_hash(shell, release, name)
    base = f"{cdn}/assets/{shell}"
    out = [(f"{base}/entry-{h('entry.css')}.css", "link", 31_000, 5.1, True),
           (f"{base}/entry-{h('entry.js')}.js", "script", 64_000, 3.3, False),
           (f"{base}/vendor-{h('vendor.js')}.js", "script", 158_000, 3.1, False),
           (f"{base}/{page.chunk}-{h(page.chunk)}.js", "script", 22_000, 3.4, False),
           (f"{cdn}/fonts/vantra-sans-var.woff2", "css", 46_000, 1.0, False)]
    if lob == "banking":
        out = [(f"{cdn}/money/static/css/main.{h('entry.css')}.css", "link", 31000, 5.1, True),
               (bundle_url(cdn, lob, release, "main"), "script", 64000, 3.3, False),
               (bundle_url(cdn, lob, release, "vendor"), "script", 158000, 3.1, False),
               (bundle_url(cdn, lob, release, page.chunk), "script", 22000, 3.4, False), out[-1]]
    return out


def bundle_url(cdn, lob, release, chunk):
    if lob == "banking":
        name = "main" if chunk == "main" else str(int(hex_id(chunk, n=4), 16) % 900 + 100)
        return f"{cdn}/money/static/js/{name}.{content_hash('money', release, chunk)}{'' if name == 'main' else '.chunk'}.js"
    return f"{cdn}/assets/{APP_SHELL[lob]}/{chunk}-{content_hash(APP_SHELL[lob], release, chunk if chunk.endswith('.js') else chunk)}.js"


# Server-Timing on server-rendered documents: CDN cache status, edge, origin render and its database time.
def server_timing(rng, origin_ms):
    db = round(origin_ms * rng.uniform(.25, .55), 1)
    app = round(origin_ms - db, 1)
    return [("cdn-cache", 0.0, "MISS"), ("edge", round(rng.uniform(2, 9), 1), ""),
            ("origin", round(origin_ms, 1), ""), ("db", db, ""), ("app", app, "")]
