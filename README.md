# XCOR demo data generator

This project creates realistic demo sessions for shopping, banking and travel, including normal activity, slow requests, errors, crashes and freezes. All of that activity is simulated by the generator; it does not run a real app or backend.

The generator sends session data directly to Embrace using your app ID and the same data format as the Embrace SDK. It also creates backend spans describing the work behind each request and sends them to Chronosphere's OpenTelemetry ingestion endpoint. The generator gives both sides the same trace ID, and Embrace's Network Spans Forwarding integration sends the network span to Chronosphere so they join into one trace. You can then open that trace from the request in the Embrace session timeline.

Three lines of business are included:

| Profile | Brand | What visitors do | Backend |
| --- | --- | --- | --- |
| `ecommerce` | Shop (Northline Goods when it has its own app) | Browse, search, view products, add to bag, sign in, check out | `shop-experience-api`, `product-catalog-service`, `product-search-service` (Elasticsearch), `price-promotion-service`, `tax-calculation-service`, `inventory-availability-service`, `product-reviews-service`, `personalization-service`, `shopping-cart-service` (Redis), `checkout-orchestrator`, `shipping-rates-service`, `order-management-service`, `warehouse-fulfillment-worker` (Kafka) |
| `banking` | Money (Corvia Bank when it has its own app) | Sign in with MFA, check balances and transactions, transfers, bill pay, statements, card lock | `banking-experience-api`, `strong-auth-service`, `account-summary-service`, `core-banking-gateway`, `transaction-history-service` (Cassandra), `payee-management-service`, `funds-transfer-service`, `transaction-limits-service`, `payment-rails-gateway`, `general-ledger-service` (Kafka), `bill-payment-service`, `card-management-service`, `statement-rendering-service` |
| `travel` | Travel (Vantra Travel) | Search flights and hotels, review fares, book, manage trips | `travel-experience-api`, `geo-location-service`, `air-shopping-service` with parallel supplier fan-out (`gds-connector-service`, `ndc-connector-service`), `fare-pricing-service`, `fare-rules-service`, `lodging-search-service`, `lodging-content-service`, `reservation-service`, `pnr-management-service`, `loyalty-accrual-service`, `trip-itinerary-service`, `ticketing-fulfillment-worker` (Kafka) |

## V2: runs described by a ScenarioSpec

On the `v2` branch, a run is described by one JSON document, the ScenarioSpec (`spec/scenario.schema.json`): targets, population, traffic (live, burst or backfill), client faults, backend faults, incidents, rollouts and the destination mode. The same document will be edited by the web UI, the REST API, MCP clients and the copilot.

```bash
python3 generate.py preset list                                   # Android, backend and story presets
python3 generate.py preset apply release-regression > my-spec.json
python3 generate.py validate my-spec.json                         # schema, platform limits, realism checks
python3 generate.py explain my-spec.json                          # what the run will generate
python3 generate.py run my-spec.json --dry-run out/run            # writes payloads, prints requested versus emitted
python3 generate.py run my-spec.json                              # sends
```

- **Destination modes.** `sessions_only` sends Cortex XCOR RUM data only: no traceparents, so nothing is forwarded and no "View trace" links appear. `end_to_end` also sends backend traces to Cortex XCOR, joined through network span forwarding.
- **Exact distributions.** "One in three sessions crashes" means exactly that in bursts and backfills (`quota: auto` or `exact`); live streams draw per session unless `quota` is `exact`. After every run the generator reports requested versus emitted rates, computed from the generated payloads.
- **Backfill.** Up to 24 hours of sessions appear at their original times in rolling ranges. Present history with "Last 24 hours", not a calendar "Yesterday"; alerts, metric exports and version-over-version views do not include history.

> Cortex XCOR accepts trace data only for the last 10 minutes, so distributed traces cannot be backfilled. Traces are generated from the moment this run starts; sessions dated earlier are sent to Cortex XCOR RUM without trace links. For a fuller trace history, start the run at least two hours before the demo.

`python3 generate.py capabilities` prints every platform limit, fault type and backfill rule. Examples are in `spec/examples/`: `live-end-to-end.json` is the always-on Baseline stream, `baseline-burst.json` an 8-minute burst and `verification-plan.json` the V1 verification cases. The web generator, server, UI, MCP server and copilot follow in later phases (see `docs/v2/V2-BUILD-PROMPT.md`).

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

The generator runs as a macOS LaunchAgent (`~/Library/LaunchAgents/local.xcor-demo-generator.plist`): it starts at login, restarts if it exits, and targets 10 sessions per minute total across all three profiles. It pauses while the Mac sleeps and catches up on wake. Status lines go to `logs/generator.log` every 10 minutes. Add `--diurnal` only when an hourly traffic curve is wanted.

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
4. **Config.** Copy `config.example.json` to `config.json` and fill in the Embrace app IDs and the Chronosphere tenant (the `<tenant>` in `<tenant>.chronosphere.io`). Keep the token in an owner-only file outside the checkout and set `chronosphere.api_token_file` to its path. On macOS/Linux the token file must be owned by the user running the generator and have mode `600` or `400`. Alternatively, inject it through your secret manager as an environment variable:

   ```bash
   # Set through your secret manager; avoid putting real values in shell history.
   export CHRONOSPHERE_API_TOKEN_FILE=/absolute/path/outside/the/repo/chronosphere.token
   ```

   To use one Embrace app for every profile, set `"default": "<appId>"` under `embrace.apps`.

   `CHRONOSPHERE_API_TOKEN` overrides token-file settings. Inline `chronosphere.api_token` is supported for existing installations, but a config containing credentials or custom OTLP headers must have owner-only permissions. The trace inspection tool uses the same credential loading rules. The ingestion token should have only trace-write permission; use a separate identity for administrative changes.

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

Without `--incident`, a seeded session policy chooses one primary scenario and generates related consequences. This is an intentionally enriched **synthetic demonstration distribution**, not a claim about production reliability. The current presentation mix targets 10% fatal crashes and 30% captured main-thread freezes lasting 6–12 seconds. These client faults are assigned across stable and canary users, independently of the primary backend/handled-error scenario, so client and backend failures can overlap. Backend-issue coverage still targets 80% of exposed canary sessions. Explicit `--scenario` or `--incident` cases remain reproducible and do not receive an unrelated random client fault. `tools/calibrate.py` measures actual emitted evidence and overlapping categories per profile; labels are not counted as proof.

The `generation` section of `config.example.json` controls `crash_session_rate`, `blockage_session_rate`, primary weights, cohort seed/share, priority journey probabilities and transport-failure share. Crash and blockage probabilities must sum to at most one. Stable identity assignment is persisted in the persona pool. Session exposure starts false on each visit and changes only when a feature request runs. Feature context is explicitly propagated to backend spans and logs, including Redis clients; it is never stored in service resources.

Additional controls: `--scenario clean|mobile|backend|slow|handled|blockage|crash`, `--crash-family 0..4`, `--incident-phase scheduled|onset|incident|recovery`, and `--rollout-phase active|rollback`. Scheduled shared-payment contention defaults to hourly UTC cycles: two minutes of onset, 52 active minutes and six recovery minutes. Phase is evaluated at request time; Shop and Travel share the occurrence, affected PAS instance and deterministic occupant traces. Healthy routes use other instances. Explicit phases select bounded verification cases; do not use such a selected corpus to claim population rates. `--manifest /absolute/path.jsonl` records live session/request identities and separate export receipts. `--verification-plan cases.json` runs a finite JSON case list at the existing rate, then resumes the normal distribution. Remove the plan from persistent configuration after the run so restarting does not repeat the selected corpus.

The smallest explicit new incident is `banking --incident account-read-fallback`: an actual core-read deadline, a separate retained snapshot, HTTP 200 with read-only/as-of semantics, and no claim that the snapshot confirms a transfer. Pending payment/PNR outcomes use a status endpoint and the same idempotency identifier. A timeout never becomes an invented confirmation.

### Other options

| Option | Purpose |
| --- | --- |
| `--dry-run DIR` | Write every payload to `DIR` instead of sending; no credentials needed |
| `--backend-logs` | Also send trace-correlated backend logs (errors, warnings, order placed) over OTLP |
| `--emit-client-spans` | Send the browser span to Chronosphere yourself. Only for apps **without** network span forwarding; with forwarding on it duplicates the root span, and the Embrace link stays hidden either way |
| `--seed N` | Reproducible output |

To send backend spans to an OpenTelemetry Collector instead of straight to Chronosphere, set `"otlp": {"endpoint": "http://localhost:4318", "headers": {}}` in `config.json`.

Remote endpoints require HTTPS with normal certificate validation. Credential-free HTTP is supported only for a loopback collector. Redirects are refused, and credentials, query strings, and fragments cannot be embedded in endpoint URLs. Configure the final endpoint directly. Server error bodies are not logged; response sizes and pending network work are bounded. See [SECURITY.md](SECURITY.md) for the security boundary and checks.

## What makes it look real

### Android (default)

- **Envelopes match the Android SDK.** Sessions are posted the way SDK 9.3.x posts them: gzipped Embrace envelopes (`resource`, `metadata`, `version` 0.1.0, `type` spans or logs) to `a-<appId>.data.emb-api.com/v2/spans` and `/v2/logs`, with `User-Agent: Embrace/a/<sdk version>`, `X-EM-AID`, `X-EM-DID` and `X-EM-PAYLOAD-TYPES`. Span, event and log shapes, names and attribute keys follow the SDK source and its integration-test golden files (copies in `reference/android-sdk-golden/`); every attribute value is a string, as the SDK sends it.
- **Real devices.** Galaxy S25, S25 Ultra, S24, S23, A16, A15, A35 and Z Fold7, Pixel 10, 9, 9 Pro, 9a, 8 and 8a, moto g, and OnePlus 12, reported with their `Build.MODEL` codes (for example `SM-S931U`) so Embrace shows market names, on Android 14 to 16 with matching API levels, screens, SoCs and storage.
- **App lifecycle.** Cold and warm starts with the SDK's startup span tree (`emb-app-startup-cold` with process init, Embrace init, activity init and render), an activity view plus a view per screen (`Embrace.startView`), navigation state on logs, OkHttp network spans named `emb-<METHOD> <path>` with `Error` status for 4xx/5xx, taps (Compose labels for Northline and Vantra, View resource IDs such as `com.corviabank.mobile:id/sign_in_button` for Corvia), breadcrumbs, custom spans, logs, and session properties. Some sessions are interrupted by switching apps, which gives multi-part user sessions; sign-out in banking ends the session as `Embrace.endSession()` does.
- **Crashes and freezes.** Five stable crash families per profile use the verified `sys.android.crash` schema, terminate the process, and upload at a modeled relaunch with a new process ID. Main-thread migration, worker-lock and layout freezes use the SDK's `perf.thread_blockage` interval and sample schema. These are generated ingestion records; OS-confirmed ANRs require a real instrumented device and are not claimed here.

### Web (`--platform web`)

- **Embrace payloads have the Web SDK's exact shape.** Scopes, attribute keys, value types, span events and log event names come from `@embrace-io/web-sdk` 2.31.0 captured running in a browser (`reference/web-sdk-2.31.0-capture/`), and are sent the way the SDK sends them (gzipped OTLP JSON to `a-<appId>.data.emb-api.com/v2/spans` and `/v2/logs` with `X-EM-AID` and `X-EM-DID`). `tools/check_output.py` checks generated output against that capture.
- **Sessions behave like the SDK.** One session part per page with soft-navigation rollovers, page load spans for the first page, clicks with real-looking elements, breadcrumbs, web vitals (TTFB, FCP, LCP, CLS, INP) that are slower on mobile, first-interaction and scroll-depth logs, custom flow spans such as `load_product` and `place_order`, error logs and occasional JavaScript exceptions on failures. Sign-out in banking ends the user session the way `endUserSession()` does.
- **Visitors return.** About 600 visitors per profile and platform are kept in `.state/` with stable device IDs, user IDs and user-session counters, so the same people come back across runs. Web visitors use current Chrome, Safari, Edge, Firefox and Samsung Internet on Windows, macOS, iOS, iPadOS and Android, weighted per vertical, with matching screen and viewport sizes and network speeds.
- **Backend traces look instrumented, not drawn.** Each hop has both a client and a server span with OpenTelemetry semantic-convention attributes and the instrumentation scope names the real libraries use (Spring, otelhttp, gRPC, JDBC, pgx, Lettuce, ioredis, kafka-clients and so on). Services carry Kubernetes and AWS resource attributes with stable pod names. Errors carry exception events with language-appropriate stack traces. Timing is laid out from the call tree, so the browser span encloses the synchronous backend work, and Kafka consumers run after the producer.

## Known limits

- **Forwarding success does not guarantee retention.** Chronosphere applies sampling after accepting spans. On `demo-sandbox`, Vantra traces previously inherited the `unassigned` dataset's baseline behavior: 50% ordinary traces, 5% fast traces, and 1% small traces. A controlled run on 2026-10-29 exported all payloads successfully, but only 3 of 8 shop traces and 1 of 3 travel traces were retained. Existing retained traces carried `behavior_dataset_slug=unassigned` and `tail_sampling_rate=0.5`.

  The tenant now has a `vantra-end-to-end` dataset matching any span whose service is one of `vantra-android-production`, `shop-experience-api`, `banking-experience-api`, or `travel-experience-api`. Its first-priority `Allow` behavior retains matching traces at 100%. This is a tenant configuration change, not an exporter flag; recreating the generator or moving it to another tenant does not recreate this policy. Other datasets' sampling settings are unchanged. Validate new traces' `behavior_dataset_slug` and `tail_sampling_rate`, and check **every known generated trace ID**, not only a sample of traces already retained. Old traces already sampled out are not recovered by a policy change.

- **Backend traces are sent on Embrace's schedule.** Embrace forwards the app's network spans only after a session part ends and is processed. The generator holds backend traces until that part ends and sends them 3–15 seconds later. Earlier timing experiments were performed with sampling enabled, so missing spans alone did not establish a universal Chronosphere arrival-order rule. Keep the current schedule while measuring timing separately from sampling. Allow processing time before checking for a trace or its app span.

- **All three domains must be enabled in Embrace.** For the shared app, include `api.vantrashop.com`, `api.vantramoney.com`, and `api.vantratravel.com`. A backend trace can exist without its app span when its domain is not forwarded. XCOR retention settings do not enable Embrace forwarding. The NSF configuration manager defaults to refreshing domain settings every five minutes (`NETWORK_SPAN_DESTINATIONS_REFRESH_PERIOD`); wait for propagation and verify newly completed sessions after a domain change. The deployed refresh period can differ from the source default.

- **Location** in Embrace comes from the IP address the payloads are sent from. All sessions from one machine show that machine's location. Run the generator from hosts in other regions for geographic spread.
- iOS sessions are not generated.

## Check output without sending

For population calibration (no export, no state saved):

```bash
python3 tools/calibrate.py --sessions 5000 --seed 7331 --output out/calibration.json
python3 -m unittest discover -s tests -v
```

`docs/upgrade-audit.json` records the original gaps and capture capabilities. `xcorgen/milestones.py` is the central breadcrumb and twelve profile-flow catalog. Timelines keep only flow anchors and a few meaningful fallback/cancellation milestones; repetitive form changes and request-start/request-end breadcrumbs are suppressed. Business operations instead emit named root spans with their measured duration, HTTP outcome and fallback attribute. Failed HTTP or transport attempts mark the task span as Error; a successful fallback remains successful with failed backend children. The calibration checks that no timeline contains three consecutive breadcrumb rows. User Flows are configured in the dashboard; custom task spans alone do not create those definitions. The current dashboard form expects **milliseconds** (maximum 300,000), and exposes “Ignore restarts while in progress” when the feature is enabled. Fresh sessions must be generated after definitions are saved. The shared app has one payment anchor pair for Shop and Travel, so scope its views by `business_unit` instead of claiming the name alone scopes a definition.

`docs/user-flows.json` contains the setup recipes and `docs/session-filters.json` the source-confirmed Session Property Key/Value conditions. These are recipes, not an import format. In app **Vantra / 5nrc2**, create separate AND conditions for business unit, release ring, exposure and the relevant feature variant. Select the actual incident window. Add the supported HTTP-status filter only for visible mobile failures; leave it off for successful-HTTP fallback demos. In User Flows, create each unique breadcrumb start/end pair, enter its `timeout_ms`, and enable **Ignore restarts while in progress** when available. Generate fresh sessions after saving. Native Error/Abandoned/completion counts require dashboard reconciliation and must not be inferred from a custom failure breadcrumb.

`docs/calibration.json` measures 5,000 sessions per profile with seed 7331 in the representative active incident phase. It includes the union of issue-bearing sessions, overlapping categories, five crash families per profile, actual emitted breadcrumb counts, start/end evidence and expected starts/hour at 10 total sessions/minute with equal profile shares. Simulated return visits accumulate prospectively; no mature D1/D7 retention is claimed.

For live proof, query **all known generated IDs**, preserving missing IDs in the denominator:

```bash
python3 tools/verify_manifest.py /absolute/path/run.jsonl --config config.json --settle-seconds 240 --output out/verification.json
```

This records the forwarded mobile span, its exact first backend child, all ancestor checks, failed spans, Redis attributes, product-supplied Embrace session link, separate pool-occupant traces and seed-7331 random exposed-canary samples. API retention is distinct from successful exporter receipts and from dashboard navigation. A transient query failure remains explicitly unverified.

Request enrichment originates in `scenarios.Plan.request` before the outcome. An allowlisted per-call context is scoped using `ContextVar` while builders execute, then passed explicitly to `backend.build_trace`; every descendant and correlated log receives the same applicable values. The OTLP encoder preserves their types and the current direct exporter applies no attribute transformation. Redis spans belong to the caller's resource and carry request attributes, never connection-level attribution. Both `db.system.name=redis` and legacy `db.system=redis` are emitted; `db.namespace=0` identifies the modeled database. Sanitized command text excludes raw identifiers. Custom `routing.*`, `cache.*`, `feature.*`, `incident.*` and pool/lock fields are application metadata. The simulator models propagation; these helpers are not a drop-in instrumentation library for real services.

Standalone `payment-authorization-service / authorization_worker.batch` traces need retention too. The demo tenant uses a narrowly scoped Allow dataset for that service/operation in addition to the existing `vantra-end-to-end` dataset. The request's failed acquisition links to these 50 occupying connections and the maintenance lock. Backend logs are optional and disabled in the live service; trace exception events remain available.

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

The current sparse-timeline/failure-intensity deployment is documented in `docs/session-tuning-verification.json`. Its fresh runtime cohort is separate from the earlier full incident audit in `docs/verification-summary.json`; percentages from that earlier snapshot have been superseded.


### V2 browser scenarios

Web targets use the Vantra estate: retail multi-page at `www.vantra.com`, banking single-page at `app.vantra.com`, and hybrid travel at `www.vantra.com/travel`. The same adapter serves the legacy web CLI. Capabilities are enforced per engine: Safari/Firefox omit unsupported CLS, INP, LoAF and Chromium resource fields. Releases determine stable bundle URLs and exception stacks.

```sh
python3 generate.py run spec/examples/web-burst.json --dry-run out/web-spec --quiet --seed 7331
python3 tools/check_output.py out/web-spec
python3 generate.py report out/web-spec spec/examples/web-burst.json
python3 generate.py lint out/web-spec
python3 generate.py run spec/examples/android-web-end-to-end.json --dry-run out/both --quiet --seed 7331
```

The preset catalog includes web release failures, mobile product LCP, promo-banner CLS, chat-widget INP, origin latency, banking routes and payment rage clicks. `web_performance` sets p75 targets per page group, browser and device. Payload-derived reports include per-segment percentiles and stable exception groups; latency-based fault evidence is approximate because healthy and regressed distributions overlap. Small percentile samples are marked insufficient for tolerance checks. The local URL rollup preview models static segment classifications and sampled sibling cardinality; actual rollups also depend on the server's history.

No live forwarding or dashboard acceptance is implied by a passing dry run or HTTP 200. Follow the owner-approved checks in `docs/v2/CODEX-HANDOFF.md` before live use.
