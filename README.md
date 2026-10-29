# XCOR demo data generator

Generates realistic Android app sessions (or web sessions with `--platform web`) in Cortex XCOR RUM (Embrace) and full distributed backend traces in Cortex XCOR (Chronosphere), stitched together end to end:

- Every API call in a session carries a W3C `traceparent`.
- The backend trace for that call uses the same trace ID, and its root span's parent is the app's network span.
- Embrace's network span forwarding sends the app's network span to Chronosphere, where it becomes the root of the trace. The session timeline in Embrace then shows **Forwarded** and **View trace in Cortex XCOR** for each call.

Every request enters through the shared platform: `api-gateway` (token validation, Redis rate limiting) and the line of business's experience API, with `customer-identity-service` and `customer-profile-service` behind it. Retail checkout and travel booking both authorize through `payment-authorization-service` (with `fraud-risk-scoring-service`), and order and booking events reach `customer-notification-service` over Kafka. Key traces are large: a checkout is about 60 spans across 14 services, a booking about 50.

Three lines of business are included:

| Profile | Brand | What visitors do | Backend |
| --- | --- | --- | --- |
| `ecommerce` | Shop (Northline Goods when it has its own app) | Browse, search, view products, add to bag, sign in, check out | `shop-experience-api`, `product-catalog-service`, `product-search-service` (Elasticsearch), `price-promotion-service`, `tax-calculation-service`, `inventory-availability-service`, `product-reviews-service`, `personalization-service`, `shopping-cart-service` (Redis), `checkout-orchestrator`, `shipping-rates-service`, `order-management-service`, `warehouse-fulfillment-worker` (Kafka) |
| `banking` | Money (Corvia Bank when it has its own app) | Sign in with MFA, check balances and transactions, transfers, bill pay, statements, card lock | `banking-experience-api`, `strong-auth-service`, `account-summary-service`, `core-banking-gateway`, `transaction-history-service` (Cassandra), `payee-management-service`, `funds-transfer-service`, `transaction-limits-service`, `payment-rails-gateway`, `general-ledger-service` (Kafka), `bill-payment-service`, `card-management-service`, `statement-rendering-service` |
| `travel` | Travel (Vantra Travel) | Search flights and hotels, review fares, book, manage trips | `travel-experience-api`, `geo-location-service`, `air-shopping-service` with parallel supplier fan-out (`gds-connector-service`, `ndc-connector-service`), `fare-pricing-service`, `fare-rules-service`, `lodging-search-service`, `lodging-content-service`, `reservation-service`, `pnr-management-service`, `loyalty-accrual-service`, `trip-itinerary-service`, `ticketing-fulfillment-worker` (Kafka) |

## One app, three lines of business

When several profiles use the same Embrace Android app ID (the default config does this with one app), the sessions come from a single super app, **Vantra** (`com.vantra.android`), with three lines of business that share one user base and one account system:

| Line of business | Session property | API domain | Backend in Chronosphere |
| --- | --- | --- | --- |
| Shop (Northline profile) | `business_unit = retail` | `api.vantrashop.com` | `northline` services |
| Money (Corvia profile) | `business_unit = banking` | `api.vantramoney.com` | `corvia` services |
| Travel (Vantra profile) | `business_unit = travel` | `api.vantratravel.com` | `vantra` services |

Filter or group by the `business_unit` session property in Embrace to split the three. Every session also carries `market`, `membership_tier` (standard, plus, premium) and `entry_point` (home_tab, push_notification, deep_link, widget, search), plus properties for its line of business: `experiment.checkout_v3` for retail, `customer.segment` and `mfa.method` for banking, `experiment.flex_dates` for travel. Most users stay signed in across the app, and the same person can shop, bank and book travel on different days.

With a separate app ID per profile, each profile keeps its own brand, package and API domain instead.

## Always-on mode

The generator runs as a macOS LaunchAgent (`~/Library/LaunchAgents/local.xcor-demo-generator.plist`): it starts at login, restarts if it exits, and averages 3 sessions per minute on a daily curve (quiet overnight, peaks at lunch and in the evening, US Eastern time). It pauses while the Mac sleeps and catches up on wake. Status lines go to `logs/generator.log` every 10 minutes.

```bash
tail -f ~/xcor-demo-generator/logs/generator.log
```

Stop it:

```bash
launchctl bootout gui/$(id -u)/local.xcor-demo-generator
```

Start it again:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.xcor-demo-generator.plist
```

To change the rate or add an incident, edit the arguments in the plist, then stop and start it.

## Setup (one time)

1. **Embrace apps.** Create one **Android** app per profile you want (or a web app for `--platform web`), with environment **Production**, and name it after the brand (for example `Vantra`). The forwarded client span's service name in Chronosphere is built from the app name, platform and environment: an Android app named `Vantra` becomes `vantra-android-production`. You only need the app ID from the integration screen; the Gradle plugin and its upload token are not used.
2. **Network span forwarding.** On the Embrace **Integrations** page, make sure the Chronosphere (Cortex XCOR) destination has the tenant ID saved, include each new app in network span forwarding, and add the API domains to the forwarded domain list:
   - Shared Android app: `api.vantrashop.com`, `api.vantramoney.com`, `api.vantratravel.com`
   - Separate apps or web: `api.northlinegoods.com`, `api.corviabank.com`, `api.vantratravel.com`

   Forward only these API domains. The dashboard only shows the **View trace in Cortex XCOR** link when forwarding succeeded for that request, so this step is required, and it must be in place before you send sessions: Embrace decides forwarding when it processes a session and does not revisit older sessions.
3. **Chronosphere API token** with permission to write traces (OTLP ingestion).
4. **Config.** Copy `config.example.json` to `config.json` and fill in the Embrace app IDs and the Chronosphere tenant (the `<tenant>` in `<tenant>.chronosphere.io`). Keep the token out of the file if you prefer:

   ```bash
   export CHRONOSPHERE_API_TOKEN=...
   ```

   To use one Embrace app for every profile, set `"default": "<appId>"` under `embrace.apps`.

Python 3.10+ only; there are no packages to install.

## Run

Backfill 25 sessions spread over the last 30 minutes:

```bash
python3 generate.py --profile ecommerce --sessions 25
```

Run continuously for a demo (6 new sessions per minute across all three brands; each payload is sent when the browser or service would have sent it):

```bash
python3 generate.py --profile all --rate 6 --duration 120
```

Turn up one failure scenario so it stands out in both products:

```bash
python3 generate.py --profile ecommerce --rate 4 --incident payment-timeouts
```

List the incidents for a profile:

```bash
python3 generate.py --profile all --list-incidents
```

Each session prints one line with the journey, device, number of pages, API calls and errors, its user session ID, and a Chronosphere link to one of its traces (the last failing call when there is one). Sessions appear in Embrace a few minutes after they end; the forwarded browser spans reach Chronosphere after Embrace processes the session.

### Incidents

| Profile | Incident | Effect |
| --- | --- | --- |
| ecommerce, travel | `payments-db-pool` | **Main demo.** The shared `payment-authorization-service` runs out of database connections (`HikariPool-1 - Connection is not available, request timed out after 3000ms`). Checkouts and bookings fail with 502 in both lines of business, the app logs the failure and sometimes crashes, and the traces show the 3 s `HikariPool.getConnection` error plus the slow `SELECT ... FOR UPDATE` on `payment_tokens` that drains the pool |
| ecommerce | `payment-timeouts` | Card processor stops responding; payment-service times out at 8 s, checkout returns 504, shoppers retry and rage-click |
| ecommerce | `reviews-n-plus-one` | Product pages run one review-media query per review (N+1) |
| ecommerce | `recs-latency` | Recommendation model inference takes 1–3 s |
| ecommerce | `inventory-sync` | Add to bag and checkout fail with 409 sold-out errors |
| banking | `fraud-latency` | Fraud scoring slows to 2–6 s; some transfers hit the 4 s gRPC deadline and fail with 504 |
| banking | `rails-outage` | The instant-payments network returns 503 |
| banking | `core-banking-latency` | Balance inquiries that miss the cache take 1.4–3.8 s at the core banking system |
| banking | `statements-slow` | Statement PDF rendering takes 3–6 s |
| travel | `supplier-timeouts` | One airline NDC adapter times out at 3 s; search returns partial results |
| travel | `gds-pnr-timeouts` | PNR creation times out; bookings fail with 504 |
| travel | `hotel-supplier-outage` | Hotel supplier returns 503; results fall back to cached rates |

Without `--incident`, errors are sprinkled in at realistic rates: about 3% of checkouts and 6% of bookings hit the payments connection pool, plus card declines, processor and PNR timeouts, inventory deadlocks that are retried, product-cache timeouts that fall back to the database, rate-limiter timeouts that fail open, and core-banking calls that succeed on retry. Most traces are healthy.

### Other options

| Option | Purpose |
| --- | --- |
| `--dry-run DIR` | Write every payload to `DIR` instead of sending; no credentials needed |
| `--backend-logs` | Also send trace-correlated backend logs (errors, warnings, order placed) over OTLP |
| `--emit-client-spans` | Send the browser span to Chronosphere yourself. Only for apps **without** network span forwarding; with forwarding on it duplicates the root span, and the Embrace link stays hidden either way |
| `--seed N` | Reproducible output |

To send backend spans to an OpenTelemetry Collector instead of straight to Chronosphere, set `"otlp": {"endpoint": "http://localhost:4318", "headers": {}}` in `config.json`.

## What makes it look real

### Android (default)

- **Envelopes match the Android SDK.** Sessions are posted the way SDK 9.3.x posts them: gzipped Embrace envelopes (`resource`, `metadata`, `version` 0.1.0, `type` spans or logs) to `a-<appId>.data.emb-api.com/v2/spans` and `/v2/logs`, with `User-Agent: Embrace/a/<sdk version>`, `X-EM-AID`, `X-EM-DID` and `X-EM-PAYLOAD-TYPES`. Span, event and log shapes, names and attribute keys follow the SDK source and its integration-test golden files (copies in `reference/android-sdk-golden/`); every attribute value is a string, as the SDK sends it.
- **Real devices.** Galaxy S25, S25 Ultra, S24, S23, A16, A15, A35 and Z Fold7, Pixel 10, 9, 9 Pro, 9a, 8 and 8a, moto g, and OnePlus 12, reported with their `Build.MODEL` codes (for example `SM-S931U`) so Embrace shows market names, on Android 14 to 16 with matching API levels, screens, SoCs and storage.
- **App lifecycle.** Cold and warm starts with the SDK's startup span tree (`emb-app-startup-cold` with process init, Embrace init, activity init and render), an activity view plus a view per screen (`Embrace.startView`), navigation state on logs, OkHttp network spans named `emb-<METHOD> <path>` with `Error` status for 4xx/5xx, taps (Compose labels for Northline and Vantra, View resource IDs such as `com.corviabank.mobile:id/sign_in_button` for Corvia), breadcrumbs, custom spans, logs, and session properties. Some sessions are interrupted by switching apps, which gives multi-part user sessions; sign-out in banking ends the session as `Embrace.endSession()` does.
- **Crashes.** When a checkout or booking failure leads to an unhandled error, the app crashes with a Kotlin `NullPointerException` in the screen's ViewModel: a `sys.android.crash` log with thread dumps, and a session that ends with `emb.crash_id`.

### Web (`--platform web`)

- **Embrace payloads have the Web SDK's exact shape.** Scopes, attribute keys, value types, span events and log event names come from `@embrace-io/web-sdk` 2.31.0 captured running in a browser (`reference/web-sdk-2.31.0-capture/`), and are sent the way the SDK sends them (gzipped OTLP JSON to `a-<appId>.data.emb-api.com/v2/spans` and `/v2/logs` with `X-EM-AID` and `X-EM-DID`). `tools/check_output.py` checks generated output against that capture.
- **Sessions behave like the SDK.** One session part per page with soft-navigation rollovers, page load spans for the first page, clicks with real-looking elements, breadcrumbs, web vitals (TTFB, FCP, LCP, CLS, INP) that are slower on mobile, first-interaction and scroll-depth logs, custom flow spans such as `load_product` and `place_order`, error logs and occasional JavaScript exceptions on failures. Sign-out in banking ends the user session the way `endUserSession()` does.
- **Visitors return.** About 600 visitors per profile and platform are kept in `.state/` with stable device IDs, user IDs and user-session counters, so the same people come back across runs. Web visitors use current Chrome, Safari, Edge, Firefox and Samsung Internet on Windows, macOS, iOS, iPadOS and Android, weighted per vertical, with matching screen and viewport sizes and network speeds.
- **Backend traces look instrumented, not drawn.** Each hop has both a client and a server span with OpenTelemetry semantic-convention attributes and the instrumentation scope names the real libraries use (Spring, otelhttp, gRPC, JDBC, pgx, Lettuce, ioredis, kafka-clients and so on). Services carry Kubernetes and AWS resource attributes with stable pod names. Errors carry exception events with language-appropriate stack traces. Timing is laid out from the call tree, so the browser span encloses the synchronous backend work, and Kafka consumers run after the producer.

## Known limits

- **Backend traces are sent on Embrace's schedule.** Embrace forwards the app's network spans only after a session part ends and is processed, which can be minutes after the request. Chronosphere drops spans that arrive long after the rest of their trace, so the generator holds each backend trace until its session part ends and sends it 70–130 s later, alongside Embrace's forwarding. A trace therefore appears in Chronosphere a few minutes after the request, complete with the app span as its root.

- **Location** in Embrace comes from the IP address the payloads are sent from. All sessions from one machine show that machine's location. Run the generator from hosts in other regions for geographic spread.
- iOS sessions are not generated.

## Check output without sending

```bash
python3 generate.py --profile all --sessions 20 --dry-run out/
python3 tools/check_output.py out/
```

## Files

| Path | Contents |
| --- | --- |
| `generate.py` | Command line entry point |
| `xcorgen/web_session.py` | Browser session simulator that builds Embrace Web SDK payloads |
| `xcorgen/backend.py` | Backend call-tree engine that builds the distributed traces |
| `xcorgen/profiles/` | The three brands: pages, journeys, API endpoints, services, incidents |
| `xcorgen/personas.py` | Devices, browsers and the returning-visitor pool |
| `xcorgen/otlp.py` | OTLP JSON helpers and protobuf encoder |
| `xcorgen/runner.py` | Config, senders, burst and live scheduling |
| `tools/check_output.py` | Validator for dry-run output |
| `tools/check_protobuf.py` | Checks the OTLP protobuf encoder against the official definitions |
| `tools/chronosphere_traces.py` | Looks up traces in Chronosphere and measures how many are complete |
| `deploy/local.xcor-demo-generator.plist` | The always-on LaunchAgent definition |
| `AGENTS.md` | Handoff notes for AI agents continuing the work |
| `reference/` | Payloads captured from the real Web SDK |
