# Build prompt: Session Generator V2

This is the complete V2 build prompt.

**What it covers:**
- the web UI;
- scenario and distribution settings;
- destination modes and backfill;
- the copilot and MCP access;
- the Android generator changes;
- the new web generator for B2C multi-page sites, single-page apps and hybrid sites;
- hosting;
- testing and the owner questions.

Feasibility and the verified platform findings are in `V2-SCOPE.md`.

Copy everything below the line into your coding agent, run from a fresh clone of `https://github.com/Aissa-emb/Session-Generator`.

---

You are building **V2 of Session-Generator**. It is an internal tool that produces realistic Cortex XCOR RUM (Embrace) sessions for Android apps and websites, together with matching distributed backend traces in Cortex XCOR (Chronosphere). Sessions and traces are stitched end to end through Embrace network span forwarding.

V1 is a Python command-line generator in this repository. V2 turns it into a product:
- **Web UI and API.** A user connects one or more Embrace apps (Android and web) and a Chronosphere tenant, picks failure scenarios and data shapes, sets exact distributions and web performance targets, previews the result, and starts live streams or bounded jobs.
- **Agents.** A natural-language copilot in the UI, and any external agent over MCP, can create and adjust scenarios.
- **Web generator.** V2 adds a web generator that reproduces what `@embrace-io/web-sdk` 2.31 sends for B2C multi-page sites, single-page apps and hybrid sites, including web vitals and JavaScript exceptions.

Read `AGENTS.md`, `README.md` and `SECURITY.md` completely before writing code. They record verified platform behavior and hard-won fixes; do not undo them.

## 0. Starting state

**Code.** `main` at `db5c59a` (or later).

| Area | Modules |
| --- | --- |
| CLI | `generate.py` |
| Engine | `xcorgen/runner.py` (config, senders, shared-app mode, burst and live scheduling)<br>`calls.py` (one API call, held backend delivery, `FORWARD_DELAY_MS` 3–15 s)<br>`backend.py` (call-tree engine and hop builders)<br>`otlp.py` (OTLP JSON, protobuf encoder, partial-success decoding) |
| Session simulators | `android_session.py`<br>`web_session.py` |
| Scenario policy | `scenarios.py` (seeded session policy, weights, incident phases, request context)<br>`client_faults.py` (five Android crash families, main-thread blockage)<br>`journeys.py` (business-state journeys shared by both platforms, root operations)<br>`milestones.py` (breadcrumb catalog and Embrace User Flow definitions)<br>`operations.py` (status reconciliation and idempotent replay)<br>`pool_evidence.py` (Hikari pool occupant traces) |
| Security | `security.py` (owner-only token files, endpoint and header validation, no redirects, bounded responses) |
| Personas and profiles | `personas.py`<br>`profiles/{platform,ecommerce,banking,travel}.py` |
| Tools | `check_output.py`, `check_protobuf.py`, `chronosphere_traces.py`, `calibrate.py`, `verify_manifest.py` |
| Docs, tests and CI | `docs/*.json` (User Flows, session filters, verification plans)<br>`tests/`<br>`.github/workflows/security.yml` (gitleaks, bandit) |

**Deployment.**
- **Embrace app:** Android `5nrc2` ("Vantra", production, US), in shared super-app mode (`com.vantra.android`, `business_unit` = `retail` | `banking` | `travel`).
- **Network span forwarding:**
  - Forwarded domains: `api.vantrashop.com`, `api.vantramoney.com`, `api.vantratravel.com`.
  - Forwarded client spans appear as service `vantra-android-production`.
- **Chronosphere:** tenant `demo-sandbox`, with dataset `vantra-end-to-end` (managed Allow behavior) so app-rooted traces are not tail-sampled away.
- **Generator service:** a macOS LaunchAgent runs `generate.py --profile all --rate 10 --quiet` continuously. It stops while the Mac sleeps.

**Known defects V2 must fix:**
1. Burst mode spreads sessions over the last 30 minutes (`--spread` default). Chronosphere drops spans older than 10 minutes, so most burst traces are lost (section 5).
2. Web sessions raise Android exception families inside JavaScript stacks. For example, `NumberFormatException` and `onDestroyView` appear in a `.js` frame (`web_session.exception` is fed by shared journeys and `client_faults`).
3. Web mode still uses three separate brands (Northline Goods, Corvia Bank, Vantra Travel) and has no shared super-app mode.
4. Web vitals are derived from load timing and cannot be controlled.
5. Web mode lacks:
   - soft-navigation spans and soft-navigation vitals;
   - long-animation-frame data;
   - DOM-state, rage-click, user-timing, element-timing and server-timing signals;
   - multi-page lifecycle and back/forward-cache behavior;
   - per-browser capability gating.
6. On 2026-10-30, 16:37–17:37 UTC, no forwarded app spans reached Chronosphere (0 of 186 traces had an app root) while the generator was healthy. Before any live acceptance test, confirm forwarding with the readiness probe (section 12).

**V1 behavior to keep:**
- the held backend delivery;
- the 30-minute last-seen rule in the persona pool;
- idempotent status replay instead of invented confirmations;
- shared incident occurrences across lines of business;
- the security rules;
- the always-string attribute rule for Android envelopes.

## 1. Non-negotiable constraints

1. **No sign that the data is generated.**
   - Never put "demo", "fake", "test", "sample", "synthetic", "mock" or similar into anything that reaches Embrace or Chronosphere. That covers session properties, attribute keys and values, service names, span names, URLs, page titles, CSS selectors, script and bundle names, IDs, log bodies, user IDs and device names.
   - This applies to everything the copilot generates as well.
   - Enforce it in code with the realism linter (section 8.4), which blocks a run.
   - Local run manifests may record provenance; it must never be emitted.
2. **SDK-faithful formats.**
   - Keep V1's payload builders.
   - Before adding any client signal, trace how the real SDK emits it. Read the SDK source in the owner's local clones (`~/Claudecode/embrace-android-sdk`, `embrace-apple-sdk`, `embrace-web-sdk`) or capture real payloads.
   - Extend `tools/check_output.py` to validate each new shape.
   - When this document and the SDK disagree, the SDK wins; record the discrepancy in `AGENTS.md`.
3. **Device- and browser-faithful.** A signal is emitted only if the simulated device or browser would produce it (section 7.4).
4. **Enterprise realism on the backend.**
   - Service names follow enterprise conventions: `<line-of-business>-experience-api`, `*-service`, `*-orchestrator`, `*-gateway`, `*-worker`.
   - Errors are root causes a real team would debug, with correct exception types, messages and language-shaped stack traces.
5. **Platform limits are physics, not bugs.** Implement and surface them (section 5). The UI and the copilot must never promise something the platforms drop.
6. **Secrets.**
   - Embrace app IDs are not secret. Chronosphere API tokens, Embrace API or symbol upload tokens, and LLM API keys are.
   - Store secrets server-side only, encrypted at rest: the OS keychain on macOS, a KMS or secret manager when hosted.
   - Never return a secret to the browser after saving it, and never log one.
   - Keep the `security.py` rules. Nothing secret is ever committed.
7. **Professional, precise UI copy.** No analogies or casual wording. Labels state what a control does.
8. **Git.** Commit as `Aissa-emb <200142933+Aissa-emb@users.noreply.github.com>` with no AI attribution trailers. Work on branches and open pull requests. Keep the security workflow passing.

## 2. Architecture

Keep the generator engine in Python and wrap it; do not rewrite the payload builders.

```
apps/web  (React + TypeScript, Vite)  ──REST + SSE──►  server/ (FastAPI)  ──►  xcorgen engine (existing package, refactored)
                                                         │   ├─ job runner (live streams, bursts, backfills, previews)
Claude Code / Claude Desktop / any MCP client ──MCP──────┤   ├─ MCP server (same tool functions as REST)
                                                         │   ├─ copilot (LLM tool loop over the same tools)
                                                         │   ├─ readiness and dashboard-setup checks
                                                         │   └─ SQLite (targets, specs, runs, receipts, verifications)
                                                         └──► Embrace ingest (Android and web), Chronosphere OTLP and Data API,
                                                              Embrace API or MCP for verification, optional source-map upload
```

- **One source of truth: `ScenarioSpec`.** The UI, the REST API, the copilot and MCP clients all read and write the same versioned JSON document (section 3). The engine executes a spec; nothing else configures it.
  - Replace V1's scattered settings with spec fields, using the mapping in section 3.2.
  - Keep a CLI that runs a spec file (`generate.py run spec.json`) so V1 workflows still work.
- **Server.**
  - FastAPI, with Pydantic models generated from the spec schema.
  - Live streams run as managed jobs (one asyncio task or subprocess per stream). Each reads the latest spec version at every session start, so edits apply without restarts.
  - Progress is exposed over Server-Sent Events.
- **Persistence.** SQLite via SQLModel or SQLAlchemy, with these tables:
  - `targets`;
  - `scenario_specs` (versioned, with author: user, copilot or MCP client);
  - `runs` and `run_events`;
  - `receipts` (per-delivery accept or reject results, Chronosphere partial-success counts);
  - `verifications`;
  - `dashboard_setup` (state of each setup item per target).
- **Hosting.**
  - Locally on a Mac: `uvicorn`, plus the Vite build served by FastAPI.
  - In one container: a `Dockerfile`, non-root, with secrets from environment variables or mounted files.
  - A hosted always-on runner matters because the LaunchAgent stops when the Mac sleeps.
- **Frontend stack:**
  - React 18, TypeScript, Vite, Tailwind CSS, shadcn/ui (Radix primitives), TanStack Query.
  - React Hook Form with Zod, using schemas generated from the same JSON Schema.
  - Framer Motion for transitions, and Recharts or visx for charts.
  - `cmdk` for a command palette.
  - One consistent icon set, for example Phosphor or Radix Icons.

## 3. ScenarioSpec (JSON Schema, versioned)

Define the spec in `spec/scenario.schema.json` and generate Pydantic and Zod types from it.

### 3.1 Fields

**`targets`** (list): one entry per Embrace app.
- `id`, `platform` (`android` or `web`; `ios` is reserved for later), `embrace_app_id`, `region` (`us` or `eu`).
- **Android:** package, app versions with release dates and adoption share, SDK version, UI toolkit (Compose or Views).
- **Web:**
  - site estate and archetype per line of business (section 7.1);
  - origins;
  - releases with bundle manifests and deploy times;
  - SDK version (2.31.0);
  - router integration (route templates or raw paths);
  - whether SDK self-upload requests appear as network calls;
  - the Origin header policy (none, or one allowed origin).
- **All targets:** `api_domains` per line of business, and an optional OTLP collector endpoint override.

**`chronosphere`**: `tenant`, `token_ref` (a reference to a stored secret, never the value). Required only in end-to-end mode.

**`destinations`**:
- `mode`: `sessions_only` | `end_to_end` (section 10.1).
- Optional: `backend_logs` (trace-correlated backend logs over OTLP, as V1's `--backend-logs`) and `backend_metrics` (RED metrics, a later phase).
- `emit_client_spans`: advanced, only for apps without network span forwarding.

**`population`**:
- **Mixes:** platform mix (share of sessions per target) and line-of-business mix.
- **User base:** signed-in share, and new versus returning visitors. **Signed-in customers share one user ID pool across Android and web**, because customers use both.
- **Session properties:** generators for `business_unit`, `market`, `membership_tier`, `entry_point`, and the line-of-business properties.
- **Android:** device catalog weights (reuse the V1 catalogs) and OS version mix.
- **Web:** browser family and version mix, OS, device type (desktop, mobile, tablet), and connection type. Defaults must include enough Chrome desktop traffic for Page Load Insights (section 5).

**`traffic`**:
- `mode`: `live` | `burst` | `backfill`.
- **Live:** average sessions per minute and a daily curve (flat, the V1 diurnal curve, or 24 custom hourly weights in a time zone).
- **Burst and backfill:** session count and window (`start`, `end`).

**`client_faults`** (list of `{ type, platform, rate, quota, filters, details }`):
- **Android types:**
  - `jvm_crash`: a V1 crash family or a custom exception;
  - `main_thread_blockage`: Embrace's ANR signal, with a duration range;
  - `handled_exception`;
  - `network_failure`: timeout, connection reset, DNS or offline, with no HTTP status;
  - `slow_startup`: cold start on low-end devices.
- **Web types:** the catalog in section 7.6.
- **Both platforms:**
  - `http_error`: 4xx or 5xx returned by a backend fault;
  - `slow_root_span`: a named flow span with a target p50 and p95.
- **`filters`** narrow who is affected:
  - line of business, app version or web release;
  - OS version, device model or tier;
  - browser family and version, device type, connection;
  - screen, page group or route, endpoint;
  - signed-in state.

**`web_performance`**: baseline distributions for healthy pages, per page group, device type and browser family. Each entry sets a target p75 for TTFB, FCP, LCP, INP and CLS, plus the TBD rating mix. Regressions are faults on top of the baseline.

**`backend_faults`** (list of `{ type, service, endpoints, rate | schedule, blast_radius, details }`):
- **Types:**
  - `db_pool_exhaustion`, `db_deadlock`, `slow_query`, `n_plus_one`;
  - `downstream_timeout`, `dependency_5xx` (external provider), `cache_outage` (with fallback);
  - `rate_limited` (429 at the gateway), `kafka_consumer_lag` (delayed async work);
  - `deploy_regression` (errors only on a given `service.version`, with a rollback time);
  - `partial_results` (fan-out supplier timeout).
- **Builders:** map each type to the V1 helpers where they exist (section 9) and to new builders where not.
- **Shared incidents:** backend faults affect Android and web sessions alike, so one incident shows in both apps.

**`incidents`**: timeline entries `{ fault_ref, start, onset_minutes, peak_minutes, recovery_minutes, peak_rate }`. A story like "payments pool exhaustion from 14:00 to 14:25 yesterday" is then data, not code. Reuse V1's scheduled, onset, incident and recovery phase logic.

**`rollouts`**: canary share, rollout and rollback times per Android app version, web release or service version (V1 has canary and rollback phases).

**`stories`**: named presets that bundle the fields above (section 9).

**Other fields:** `seed`, `spec_version`, `notes`.

**Validation runs on every write** and returns actionable messages for:
- rates outside [0, 1];
- mutually exclusive outcomes that sum to more than 1 per session;
- later faults placed after a fatal crash or a page abandonment (both end the session);
- filters that match nothing;
- a schedule outside the traffic window;
- a fault whose platform has no target;
- browser capability conflicts, for example a CLS target on a Safari-only filter, or soft-navigation faults on a multi-page archetype;
- platform-limit violations (section 5);
- realism-linter failures.

### 3.2 V1 settings and their V2 fields

| V1 | V2 |
| --- | --- |
| `--profile` | `population.line_of_business_mix` |
| `--platform` | `targets[].platform` and `population.platform_mix` |
| `--sessions`, `--spread` | `traffic.mode=burst`, count and window; trace-eligible only in the last ~8 minutes |
| `--rate`, `--duration`, `--diurnal` | `traffic.mode=live`, rate, duration, daily curve |
| `--incident`, `--incident-phase`, `--list-incidents` | `backend_faults`, `incidents`, and the preset catalog |
| `--scenario` and `generation.scenario_weights` | Fault rates. Ship a "Baseline" preset that reproduces the V1 default mix |
| `generation.crash_session_rate`, `blockage_session_rate` | `client_faults` `jvm_crash` and `main_thread_blockage` rates |
| `--crash-family` | `client_faults[].details.family` |
| `--rollout-phase`, cohort settings | `rollouts` |
| `--manifest` | Always-on run manifest |
| `--verification-plan` | Bounded verification cases on a run, after which the normal distribution resumes |
| `--backend-logs`, `--emit-client-spans` | `destinations` |
| `otlp.endpoint` | Target collector override |
| `--seed` | `seed` |
| `--dry-run` | `preview_spec` and dry-run jobs |

## 4. Exact distributions

Users will say "one in three sessions crashes". Implement two modes per fault:
- **Probability:** each eligible session draws independently.
- **Exact quota** (the default for bursts and backfills): pre-allocate outcomes across the planned sessions with stratified assignment, shuffling within time buckets and within filters. Emitted counts then match the requested share within one session, while staying spread over time and across devices and browsers.

**Combination rules:**
- A session carries at most one fatal outcome.
- Main-thread blockage, handled exceptions and web exceptions can co-occur with backend faults.
- Crash and exception timing is placed at a realistic moment in the journey, for example right after a failed payment response.

**Web performance targets:**
- Draw values from per-segment distributions (log-normal for timings, a zero-inflated distribution for CLS), calibrated so the emitted p75 lands on the target.
- Each value's rating must match the dashboard thresholds (section 7.3).
- Attribution fields must sum to the value (section 7.3).

**Requested versus emitted.** After every run, compute emitted counts and percentiles from the generated payloads (extend `tools/calibrate.py`). Show them next to the requested values: rates per fault, and p75 per vital and segment. Never report a rate or percentile from the plan alone.

## 5. Platform limits the product must enforce

These limits were verified. Encode them in `server/limits.py`, show them in the UI, and expose them through the copilot and MCP (`get_capabilities`).

**Chronosphere:**
1. **Spans with a start time more than 10 minutes before or after ingest are dropped** ("Tracing ingest limits"). The OTLP request still returns success.
   - Backend traces can only exist for activity in the last ~10 minutes.
   - A forwarded client span is dropped if Embrace forwards it more than 10 minutes after the request started.
2. **Metrics:** raw points are accepted up to 2 hours late; aggregated points only a few minutes late; points more than 1 minute in the future are rejected.
3. **Tail sampling.** App-rooted traces fall into whatever dataset and behavior matches. On `demo-sandbox`, the unassigned dataset sampled at 50% until `vantra-end-to-end` was created.
   - The readiness check queries known trace IDs after a run and reports retention per line of business and platform.
   - If the token has configuration permissions, offer to create or extend the dataset and Allow behavior for the target's services, including `vantra-web-production`. Show the exact change and require confirmation.

**Network span forwarding and the trace link:**

4. **Forwarding requirements:**
   - The app is included in forwarding, with Chronosphere as the destination, and every API domain is on the forwarding list.
   - Forwarding is decided once, at ingest, and configuration changes take about five minutes to take effect.
   - Only `perf.network_request` spans are forwarded. Web document loads and resource fetches are never linked to traces.
5. **The "View trace in Cortex XCOR" link** appears only when the request's forwarding outcome is `success`, the app has a Chronosphere destination and tenant, and the organization has the trace-link feature (`chronosphereTraceLinkEnabled`) enabled.

**Embrace ingest and processing:**

6. **Ingest returns 200 for everything,** including payloads it discards. Verify through the Embrace API or the Embrace MCP server, never from HTTP status alone.
7. **Location** comes from the sender's IP. A single runner shows one location; document it, and support multiple runner regions where geographic spread is required.
8. **Web ingest rules:**
   - **Origin:** payloads whose Origin header is not on the app's allowed-origins list are dropped silently when enforcement is on. Send none, or an allowed one (`go/lib/nginx/processor/processor.go:443-480`).
   - **Session part span:** every `/v2/spans` payload needs a `ux.session_part` span.
   - **Resource parents:** resources whose parent document load is not in the same payload are dropped (`go/loader/session/web-resource/internal/inserter/inserter.go:86-112`).
   - **Dropped payloads:** `file://` URLs are dropped, and so is SpeedCurve PTST traffic.
   - **Vitals are not deduplicated,** so send each one once.
   - **Surface names are not rolled up,** so use route templates.
   - **Page Load Insights** defaults to Chrome 107+ desktop.
9. **Backfill rules:** section 10.

## 6. Android generator

Keep V1's Android session builder (envelopes, parts, startup tree, views, taps, OkHttp network spans, breadcrumbs, logs, crashes and thread blockage). Add:

- **`network_failure`:**
  - Requests that fail without a response, following the SDK's OkHttp instrumentation: `error.type` is the exception class (for example `java.net.SocketTimeoutException`, `java.net.UnknownHostException`, `javax.net.ssl.SSLHandshakeException`), `exception.message` is set, and there is no status code.
  - Retries follow, and they usually succeed.
  - In end-to-end mode, a timeout after the request reached the gateway still produces a backend trace; a DNS or offline failure produces none.
- **`slow_startup`:** cold-start spans over 4 s on budget devices, with the time in the startup span tree where a real regression would put it (application init, SDK init, first render).
- **Configurable `slow_root_span`** for the journey flow spans in `journeys.ROOT_OPERATIONS`.
- **Exact-quota versions** of crashes, blockage and handled exceptions.

## 7. Web generator (B2C multi-page, single-page app, hybrid)

**The goal:** web sessions indistinguishable from what `@embrace-io/web-sdk` 2.31 sends, filling every Cortex XCOR RUM web view, optionally stitched to backend traces. These views are Web Vitals, Pages health, Page Load Insights, JavaScript Exceptions and the user timeline.

### 7.0 Sources of truth

- **SDK source:** `~/Claudecode/embrace-web-sdk/packages/web-sdk/src` (`SRC/` below).
- **Real payloads per engine:** `~/Claudecode/embrace-web-sdk/tests/integration/tests/__golden__/{chromium,firefox,webkit}-vite-7-es2015-*.json`. Copy the Firefox and WebKit goldens into `reference/web-sdk-golden/` and validate against all three, not only Chromium.
- **Real 2.29 session sample:** `~/Claudecode/go/tool/internal/schema-stats/testdata/web_2_29_0_sample.json` (JSON lines: session part, surfaces, document load, resources, network, vitals, LoAF, user timing).
- **Existing capture:** `reference/web-sdk-2.31.0-capture/`, which contains soft-navigation vitals, the `Soft Navigation` span, LoAF and DOM state.
- **Backend parsing and dashboard queries:** cited inline. Verify against the code before relying on them.

### 7.1 Archetypes and lifecycle

**B2C multi-page: every navigation is a hard page load.**
- **Storage.** The user session lives in localStorage under `<appID>__embrace_user_session_state`. The app instance ID lives in sessionStorage (`<appID>__embrace_app_instance_id`), so it is the same across page loads in one tab and new in each tab.
- **Leaving a page.** The old page's part ends with `emb.session_part_end_reason=background` when the page becomes hidden. Ending a non-final part sets an inactivity deadline 30 minutes out.
- **Arriving on a page.** Each page load starts a new SDK instance and a new part:
  - `emb.session_part_start_reason=init`, or `foreground` / `web_activity` if the tab was not focused at load;
  - `emb.cold_start=true` on the first part of every page load;
  - `emb.page_load` on that part's end only: `true` if the page finished loading, `false` if the visitor left before the load event (abandonment).
- **Counters.** The same `emb.user_session_id`; incremented `emb.user_session_part_index` and `emb.session_part_number`; a new `emb.session_part_id`; a new `app.surface.id` per page load.
- **One spans payload per page load,** containing:
  - the part span;
  - a `documentLoad` span (`ux.document_load`, `url.full`, `user_agent.original`, navigation-timing events plus `firstPaint` and `firstContentfulPaint`);
  - a `documentFetch` child (`ux.resource_fetch`, `browser.navigation_timing.type`);
  - `resourceFetch` children for resources present at load, with `parentSpanId` set to the document load span. They must be in the same payload (section 5).
- **Back/forward:**
  - **Restored from bfcache:** no new document load. A part starts with `foreground`, `cold_start=false` and no `emb.page_load`, under the same page and instance IDs. Vitals are re-reported with `navigation_type=back-forward-cache`.
  - **Not restored:** a fresh load with `browser.navigation_timing.type=back_forward` (underscore). `browser.navigation_timing.not_restored_reasons` (a string array, Chromium only) is set on `documentFetch` and on every vital.
- **Expiry:**
  - A user session expires after 30 minutes without engagement or 12 hours in total. The next session then carries `emb.user_session_previous_id`.
  - 30 minutes in the foreground with no input ends the part as `web_foreground_inactivity`, with termination `inactivity`.
- **Traffic shapes:**
  - landing on product or category pages from search and ads rather than the home page;
  - high bounce on landing pages;
  - category, product and cart paths;
  - reloads and back navigation, some restored from bfcache;
  - abandoned loads on slow mobile connections.

**Single-page app: one hard load, then soft navigations.**
- **Rollover.** Route changes roll the part over only through the Navigation API: a `currententrychange` whose URL differs, including query-only and hash changes.
  - Start and end reasons are both `web_soft_navigation`, sharing one boundary timestamp (`SRC/managers/EmbracePageManager/EmbracePageManager.ts:109-129`).
  - Supported on Chromium 102+, Safari 26.2+ and Firefox 147+. Older Safari and Firefox keep one part for the whole visit.
- **`ux.surface` spans** (scope `NavigationInstrumentation` 1.0.0):
  - **Name:** the route template when the app uses a router integration that calls `setCurrentRoute({path, url})` (for example `/accounts/:accountId`, with `app.surface.name` set). Otherwise the raw pathname, with no `app.surface.name`.
  - **Label:** `app.surface.label` is the route label, or `document.title` at span end.
  - **URL:** `browser.url.full` is read at end, so the outgoing span carries the new URL.
  - **End:** on a route change or at part end.
  - **Use templates.** Surfaces are not rolled up and Pages health counts views per name (`go/lib/codec/session_surface_mapper.go:31-90`).
- **`Soft Navigation` span** (scope `SoftNavigationPerformanceInstrumentation`, no `emb.type`):
  - **Sources:** native (`emb.soft_navigation.source=performance_observer`) on Chrome 151+; polyfill (`source=polyfill`) where the Navigation API and event timing exist.
  - **Polyfill condition:** a click timing entry of at least 16 ms around the navigation. Keyboard, programmatic and back-button navigations produce no polyfill span.
  - **Attributes:** `emb.soft_navigation.interaction_id`, `.duration`, `.start_time`. Native adds `.navigation_id`, `.paint_time` and `.presentation_time`.
  - **Correlation arrays:** `.span_ids`, `.span_id_types`, `.log_ids` (`log.record.uid` values) and `.log_id_types`.
  - **Limit:** at most 100 per part.
- **Soft-navigation vitals** (Chromium with native soft navigations only): each soft navigation reports CLS, FCP, INP and LCP, never TTFB, with `navigation_type=soft-navigation`, `navigation_id` and `interaction_id`.
- **Traffic shapes:** sign-in once, then many route changes; long visits with idle gaps (tab hidden and shown); background tabs; polling API calls.

**Hybrid.**
- **Structure:** server-rendered entry pages (multi-page rules) lead into client-routed flows (single-page rules) on one origin. Example: a server-rendered product page leads into a client-side checkout.
- **Trace context:** server-rendered pages may include `<meta name="traceparent">`. The SDK then starts `documentLoad` inside that context only when a global propagator exists (forwarding on).
- **No trace link for page loads:** the dashboard does not link document loads to traces. Emitting a matching server-rendering trace in Chronosphere is optional.

**Archetype per line of business.** Make it configurable. Default: retail is B2C multi-page, banking is a single-page app, travel is hybrid.

### 7.2 Common attributes and batching

**Common attributes:**
- **Every span, at end:** `browser.url.full`, `app.surface.id`, `app.surface.label`, `emb.perf_clock_lag_ms`, and `app.surface.name` when a route path exists.
- **Every log:**
  - identity: `log.record.uid`, `emb.user_session_id`, `emb.user_session_previous_id`, `emb.session_part_id` (`''` if none);
  - page: `browser.url.full`, `url.full`, `emb.is_tab_engaged`, `app.surface.*`, `emb.perf_clock_lag_ms`;
  - `user.id` once known.
- **ID formats:** session, part, instance, page and log record IDs are 32 uppercase hex characters. Trace and span IDs are lowercase hex.
- **Timestamps:** fractional milliseconds on Chromium; whole numbers on Firefox and WebKit.

**Batching:**
- **Spans:** one `POST /v2/spans` per part end, containing the part span plus every span that ended since the previous part ended.
- **Logs:** batched separately to `/v2/logs`.
- **Headers:** `X-EM-AID`, `X-EM-DID` (32 hex), `Content-Type: application/json`, `Content-Encoding: gzip`, and the Origin policy from the target.

### 7.3 Web vitals and long animation frames

**Web vitals** are `ux.web_vital` logs with `eventName=browser.web_vital`, severity 9 and no severity text (`SRC/instrumentations/web-vitals/WebVitalsInstrumentation/WebVitalsInstrumentation.ts`).
- **Attributes:** `browser.web_vital.name` (`lcp|cls|inp|fcp|ttfb`), `.value`, `.delta`, `.rating`, `.id` (`v6-<ms>-<13 digits>`), `.navigation_type`, `.navigation_id`, and `.interaction_id` when present.
- **Part assignment:** `emb.session_part_id` is chosen by the metric's own timestamp.
- **Body:** JSON of the primitive attribution fields.

**Per metric:**

| Metric | Attributes (`emb.web_vital.attribution.` prefix) | Body | Timestamp | Emitted |
| --- | --- | --- | --- | --- |
| TTFB | `redirect`, `domainLookup`, `tcpConnection`, `tlsNegotiation`, `serverResponse`, `unattributed` (integers) | `waitingDuration`, `cacheDuration`, `dnsDuration`, `connectionDuration`, `requestDuration` | Part zero time | Once, at load |
| FCP | — | `timeToFirstByte`, `firstByteToFCP`, `loadState` | Entry start | Once |
| LCP | `elementType`, `elementBoundingRect` (JSON x, y, width, height) | `target`, `url` (images), `timeToFirstByte`, `resourceLoadDelay`, `resourceLoadDuration`, `elementRenderDelay` | Entry start | First input or page hidden |
| INP | `element_type`, `loaf_scripts` (JSON `{"<script url or (inline)>": {total_duration, style_and_layout_duration, count}}`) | `interactionTarget`, `interactionTime`, `nextPaintTime`, `interactionType` (`pointer` or `keyboard`), `inputDelay`, `processingDuration`, `presentationDelay`, `loadState` | Interaction time | Page hidden, only after an interaction |
| CLS | `clsLayoutShifts` (JSON array of `{selector, startRect, endRect}`, at most 50), `clsLayoutShiftsDroppedCount` | `largestShiftTarget`, `largestShiftTime`, `largestShiftValue`, `loadState` | Earliest shift in the window | Page hidden; can repeat |

**Internal consistency.** Attribution must add up:
- LCP = TTFB + resource load delay + resource load duration + element render delay.
- INP = input delay + processing duration + presentation delay.
- The LCP and CLS targets name real elements of the page.

**Ratings must match values,** using the dashboard thresholds (`dashjs/src/helpers/webVital.ts`). The backend stores the rating as sent, and the dashboard colors p75 itself. Values are in milliseconds except CLS.

| Metric | Good up to | Poor above |
| --- | --- | --- |
| LCP | 2.5 s | 4 s |
| FCP | 1.8 s | 3 s |
| TTFB | 0.8 s | 1.8 s |
| INP | 200 ms | 500 ms |
| CLS | 0.1 | 0.25 |
| TBD | 200 ms | 600 ms |

**Sending rules:**
- Send each vital exactly once, as a log only. The backend also accepts vitals as session span events, and the table has no dedupe (`go/domain/webvital/table/table.go:13`).
- Use exactly the SDK's attribution key spellings so the LCP, INP and CLS breakdown cards fill. The backend prefixes most keys with the vital name (`target` becomes `LCPTarget`) and keeps `interactionTarget`, `largestShiftTarget` and `element` verbatim (`go/lib/codec/web_vital_mapper.go:115-161`).

**Long animation frames** (Chromium only; `SRC/instrumentations/loaf/`). At each part end with at least one long frame, emit two logs:
- **`emb-loaf-report`:**
  - `emb.type=ux.web_vital`, `browser.web_vital.name=tbd`, `browser.web_vital.id=embracev2-<ms>-<rand>`.
  - `browser.web_vital.value`: the sum of blocking durations, excluding the page's first long frame and frames tied to UI events.
  - `browser.web_vital.rating`: up to 200 good, up to 600 needs improvement, otherwise poor.
  - `emb.tbd.loaf_total_duration`, `_work_duration`, `_style_and_layout_duration`, `_count`, `_longest_duration`, `_longest_duration_excluding_first`.
  - No delta and no navigation fields.
- **`emb-loaf-scripts`:** `emb.type=ux.loaf_scripts`, a JSON body of per-script totals, and `emb.was_from_inp`.
- **Script attribution** names first-party bundles (hashed file names per release) and third-party tags (tag manager, analytics, chat widget, A/B testing). This makes an INP or TBD regression explainable in a demo.

### 7.4 Browser capability matrix

Implement the matrix as data in `xcorgen/web_capabilities.py`, and enforce it in generation, spec validation and the linter.

| Capability | Chrome, Edge, Samsung Internet (desktop and Android) | Safari (macOS, iOS) | Firefox |
| --- | --- | --- | --- |
| TTFB, FCP, LCP | yes | yes | yes |
| CLS | yes | no | no |
| INP | yes | verify (newer versions only) | verify (newer versions only) |
| LoAF / `tbd` | yes | no | no |
| Navigation API rollover | 102+ | 26.2+ | 147+ |
| Native soft-navigation span and soft-navigation vitals | 151+ | no | no |
| `device_memory`, `network_effective_type` resource attributes | yes | no | no |
| Resource `render_blocking_status`, `delivery_type` | yes | no | no |
| Resource `http.response.status_code` | yes | no | yes |
| `not_restored_reasons` | yes | no | no |
| `responseStart` timing event | yes | may be missing | yes |

For "verify" cells, check the golden payloads and the web-vitals 6.2.3 support table before enabling. Until verified, do not emit.

### 7.5 Other web signals

**Resources** (`ux.resource_fetch`):
- **Attributes:** `url.full`, `http.response_content_length` (plus `_uncompressed` when the decoded size differs), `http.request.initiator_type` (`navigation`, `script`, `link`, `css`, `img`, `fetch`, `other`), `http.response.status_code` (not on Safari), `http.response.size`, `http.response.body.size`, `http.response.decoded_body_size`.
- **Chromium only:** `http.request.render_blocking_status`, `http.response.delivery_type`. Cache hits count only when `delivery_type=cache`.
- **Optional flags:** at most one of `http.response.cors_opaque`, `http.request.incomplete` or `http.request.prevented`, plus `http.response.cache_revalidated`.
- **File types:** resource URLs need file extensions (`.js`, `.css`, `.woff2`, `.avif`, `.webp`, `.png`).
- **Page Load Insights** lists only link, script, css, img and video initiators.

**Pages, URLs and Page Groups:**
- **URL rollup.** The backend rolls up `browser.url.full` for vitals and resource page URLs (`go/lib/grouper/`):
  - numbers, UUIDs, long hex and any segment with two or more hyphens collapse immediately (`/p/merino-crew-sweater` becomes `/p/«slug»`);
  - other words collapse only after 25 distinct values at one level.
- **URL schemes** should look like a real site: hyphenated product slugs, numeric IDs, a bounded set of category words, and query strings for search and filters.
- **Page Groups** (`surface_label`) are dashboard regex rules over `browser.url.full`, with the SDK label as fallback. Generate the rules for each site (section 12).
- **Pages health** joins surfaces, LCP, network failure rate, page-load p95 and exceptions by `app.surface.name`, so every signal for a page carries the same template.

**Interaction and page-state signals** (all on by default in the SDK):
- **Clicks:** a `click` event on the part span, with `emb.type=ux.tap`, `tap.coords=x,y`, and `view.name=<tag class="...">first 30 characters of text...</tag>`. Include the class only when it is non-empty, and the trailing `...` only when the text is truncated.
- **`first-interaction` log** (`emb.otel_log`):
  - `first_interaction.interaction_type`: `click`, `tap`, `keypress` or `scroll`;
  - also `.element_type`, `.element_selector` (empty for scroll), `.time`, and `.x` and `.y` for clicks.
- **`max-scroll-depth` log** at part end: `.pixels`, `.did_scroll`, `.percent`, `.document_height`.
- **`dom-state` log** at part end:
  - `dom_state.element_count`, `.average_depth` (or `.traversal_limit_reached`), `.document_height`, `.document_width`;
  - once per page: `dom_state.images_above_fold.count`, `.viewport_height`, `.viewport_width`, `.timestamp`, `.capture_time`.
- **`rage-click` log:** emitted when 3 or more clicks hit the same target, or land within 50 px, inside 500 ms.
  - Attributes: `rage_click.element_type`, `.element_selector`, `.x`, `.y`, `.count`, `.interaction_type`.
  - Tie rage clicks to failures, such as a button that does nothing after an API error.

**Timing signals:**
- **User timing:**
  - Measures become spans named after the measure, with `ux.user_timing`, `emb.instrumentation=user_timing`, `emb.user_timing.entry_type=measure`, `.start_time`, `.duration` and `.detail`.
  - Marks become `emb-user-timing` logs.
  - Use product names, for example `checkout:payment-sheet-ready` or `search:results-rendered`.
- **Element timing:** spans named after the `elementtiming` identifier (hero image, price block), with `ux.element_timing` and `emb.element_timing.*`.
- **Server timing:**
  - One `emb-server-timing` log per entry of the main document's `Server-Timing` header: `ux.server_timing`, `emb.server_timing.name`, `.duration`, `.description`.
  - Use `cdn-cache`, `edge`, `origin`, `db` and `app` from the server-rendering layer.

**Network** (scopes `@opentelemetry/instrumentation-fetch` 0.223.0 and `@opentelemetry/instrumentation-xml-http-request`):
- **Span shape:** the span name is the method, kind CLIENT, with `http.request.method`, `url.full`, `http.response.status_code`, `server.address`, `server.port` and the timing events.
- **XHR events:** `open`, `send`, then `loaded`, `error`, `abort` or `timeout`.
- **Failures:** offline, CORS and aborted requests are `status_code=0` with an error status.
- **Traceparent:** `emb.w3c_traceparent` is present only when forwarding is on, and only on same-origin or allow-listed cross-origin URLs.
- **SDK uploads:** the SDK records its own uploads to `a-<appId>.data.emb-api.com` as network spans by default. This is a target setting.

### 7.6 JavaScript exceptions and web fault catalog

**Exception records** (`SRC/managers/EmbraceLogManager/EmbraceLogManager.ts`):
- `emb-exception` log: severity 17, `ERROR`, with the message as body (at most 1024 characters).
- **Attributes:**
  - `emb.type=sys.exception`, `emb.exception_handling`, `emb.exception_cause` (`present` or empty);
  - `exception.type`, `exception.name`, `exception.message`, `exception.stacktrace`;
  - `emb.js_file_bundle_ids`, `emb.state`, and `emb.exception_number` (keeps counting across visits).
- **Part span counters:** `emb.unhandled_exceptions_count`, `emb.error_log_count`.
- **Handling values:**
  - `handled`: from `log.logException`. Hidden by the JavaScript Exceptions list's default filter.
  - `unhandled_error`: a window error.
  - `unhandled_rejection`: an unhandled promise rejection.
  - `unhandled`: a React error boundary, with `react.component_stack` and `emb.instrumentation=react_error_boundary`.
  - A cross-origin `Script error.` has `exception.type=string`.

**Exception families.** These replace the Android families on web. Each family carries messages per browser engine:
- `TypeError` reading a property of undefined:
  - Chrome: `Cannot read properties of undefined (reading 'price')`
  - Safari: `undefined is not an object (evaluating 't.price')`
  - Firefox: `t is undefined`
- `TypeError: x is not a function` after a partial deploy.
- `ChunkLoadError: Loading chunk 742 failed.` after a release, in tabs opened before the deploy.
- `SyntaxError: Unexpected token '<', "<!DOCTYPE "... is not valid JSON` when an API returns an HTML error page.
- Unhandled rejections: `TypeError: Failed to fetch` (Safari: `Load failed`), and `AbortError: signal is aborted without reason`.
- `QuotaExceededError` from storage.
- React hydration errors on server-rendered pages: `Minified React error #418`.
- `Script error.` from a third-party tag.

**Stacks:**
- Use the engine's format: Chrome and Edge use `at fn (url:line:col)`; Safari and Firefox use `fn@url:line:col`.
- The backend picks the parser from the first matching line. A Chrome-format stack whose message contains `@` is parsed as Firefox and loses its frames, so keep messages free of `@` for Chrome stacks (`go/lib/codec/js.go`).

**Grouping:**
- An issue is a hash of `exception.name` plus the chosen frame's method and file (`go/domain/crash/id.go:330-337`).
- Keep function and file names stable within a release, and change bundle hashes per release. Issues then group within a release, and a new release shows new issues, as in production.

**Symbolication** (optional phase; needs the owner's symbol upload token):
- Generate a real minified bundle and source map per release.
- Compute the bundle ID the way the web CLI does (the source map `debugId`, or the MD5 of the JS file) and send `emb.js_file_bundle_ids` in the CLI format (`packages/web-cli/src/processSourceFiles.ts`).
- Upload the map to `POST /v2/store/sourcemap` before sending exceptions. Uploading later does not regroup stored exceptions.

**Web fault types for `client_faults`** (all with filters and exact quotas):

| Fault type | Effect |
| --- | --- |
| `js_exception` | By family, handling, page and browser. Optionally followed by rage clicks and an error log |
| `chunk_load_failures` | After a release, for tabs older than the deploy |
| `poor_web_vitals` | Target p75 per vital per segment |
| `lcp_regression` | A heavier hero image or a slower origin, shifting `resourceLoadDuration` or `timeToFirstByte` |
| `layout_instability` | A late promo banner, ad slot or web fonts, with consistent CLS targets and selectors |
| `interaction_latency` | Slow handlers attributed through `loaf_scripts` to a named bundle or third-party tag; INP and TBD rise together |
| `slow_server_response` | TTFB from origin latency, tied to the backend trace of the matching request in end-to-end mode |
| `soft_navigation_slowness` | Long `emb.soft_navigation.duration` and poor soft-navigation LCP and INP |
| `rage_clicks` | Rage clicks on failing or unresponsive controls |
| `page_abandonment` | `emb.page_load=false` on slow connections |
| `network_failure` | Status 0 offline, CORS or aborted requests |

Data-shape controls: `bfcache_share` and `third_party_bloat` (more and heavier tags).

### 7.7 Web identity for the shared app

Web needs its own Embrace web app; `5nrc2` is Android. The owner creates "Vantra" (web, production). Forwarded web spans then appear in Chronosphere as `vantra-web-production`.

**Proposed estate** (configurable, confirm with the owner):
- `www.vantra.com`: retail (B2C multi-page, server-rendered) and the travel landing pages.
- `app.vantra.com`: online banking and the booking flow (single-page app).
- `static.vantra.com`: CDN with hashed bundles per release.
- The same `business_unit` session property as Android, and the same customer base.

Two origins mean separate storage and therefore separate user sessions, which is realistic. The API domains match Android (`api.vantrashop.com`, `api.vantramoney.com`, `api.vantratravel.com`); they are cross-origin calls, so the target's propagation allow list must include them.

## 8. Natural-language control

### 8.1 Copilot in the UI

A right-hand panel where the user types requests such as:
- "Make one in three checkout sessions on 10.28.0 crash after the payment fails, Android 16 only."
- "Add a payments database pool outage from 2:00 to 2:20 pm Eastern yesterday that affects shop and travel, on Android and web, then recover."
- "Slow the hotel search root span to a p95 of 4 seconds for budget Android devices."
- "Make LCP p75 on product pages 3.8 seconds for mobile Safari and keep desktop Chrome healthy."
- "After the next web release, show chunk load failures on checkout for tabs opened before the deploy."
- "Raise INP on search to about 450 ms, caused by the chat widget script."
- "What is this stream generating right now?"

**How it works:**
- **Model:** an LLM tool loop on the server using the Anthropic API. The default model is `claude-sonnet-5-5`, configurable to `claude-opus-5-5`, with the key stored as a server secret.
- **Tools:** the same functions the MCP server exposes (8.2).
- **Output:** the model never emits raw payloads. It reads the catalog, the capability matrix, the current spec and the schema, and returns a **JSON Patch against the spec** plus a plain-language summary.
- **Review:** the server validates the patch (schema, limits, browser capabilities, realism linter). The UI shows a diff, for example "Adds: jvm_crash, 33.3% of sessions where business_unit=retail and app_version=10.28.0", with **Apply** and **Discard**.
- **Running streams:** applying creates a new spec version that takes effect at the next session start.
- **History:** keep copilot turns and spec versions, with authorship.
- **Unsupported requests:** when a request conflicts with a capability (CLS for Safari, traces for yesterday), the copilot explains why and proposes the closest valid alternative.

### 8.2 MCP server (bring your own agent)

Expose the generator as an MCP server so Claude Code, Claude Desktop or any MCP client can drive it. Use the official MCP Python SDK, offering:
- **streamable HTTP** for remote use, with bearer-token auth and per-user tokens created in Settings;
- **stdio** for local use.

**Tools** (JSON-schema typed, idempotent where possible):

| Group | Tools |
| --- | --- |
| Capabilities | `get_capabilities` (platforms, fault types, limits, browser capability matrix, backfill rules) |
| Targets | `list_targets`, `create_target`, `test_target` (secrets accepted on create, never returned)<br>`check_readiness`, `get_dashboard_setup` (section 12) |
| Presets | `list_presets`, `get_preset` |
| Specs | `get_spec`, `create_spec`, `patch_spec` (JSON Patch), `validate_spec`, `explain_spec` (plain-language summary) |
| Preview | `preview_spec`: a dry run returning expected counts per fault, expected vitals percentiles per segment, one sample Android timeline, one sample web timeline, one trace waterfall, and linter results |
| Runs | `start_run` (`mode`: `live` \| `burst` \| `backfill`; `destinations`: `sessions_only` \| `end_to_end`)<br>`stop_run`, `pause_run`, `list_runs`<br>`get_run_status` (includes the trace-eligible start time in end-to-end mode) |
| Incidents | `schedule_incident`, `cancel_incident` |
| Verification | `verify_run`: Chronosphere retention and completeness for the run's trace IDs; Embrace counts, vitals percentiles and exception groups for the window |

**Resources:** the spec JSON Schema, the fault catalog with descriptions, the capability matrix and the preset library.

**Prompts:** `prepare-demo` (asks for the audience, the story and the time until the demo) and `design-incident`.

**Confirmation:** destructive or outbound actions (`start_run`, large backfills, Chronosphere configuration changes, source-map uploads) require an explicit `confirm: true` argument and are logged with the caller.

### 8.3 Guardrails

- **Size limits:** a maximum number of sessions per job, a maximum rate per stream, and a maximum number of concurrent streams per target.
- **Same validation everywhere:** copilot and MCP inputs go through the same validation as the UI.
- **User text is data:** text in user-supplied fields, such as a custom exception message or a page title, is data and never instructions.

### 8.4 Realism linter

The linter runs on specs and on sampled generated payloads, and a failing lint blocks `start_run`. It checks:
- **Wording and names:**
  - the forbidden-word list (section 1.1), case-insensitive, including inside IDs, URLs, selectors and bundle names;
  - service-name conventions.
- **Consistency:**
  - exception type and stack language match the service's `telemetry.sdk.language` and, on web, the browser engine's stack format;
  - durations are plausible (no zero-length network calls, no negative gaps);
  - HTTP status and span status agree;
  - device and OS combinations exist, and browser and OS combinations exist.
- **Web capabilities and formats:**
  - no CLS or LoAF from Safari or Firefox;
  - no soft-navigation vitals outside Chromium 151+;
  - ratings match values;
  - resource parents are in the same payload;
  - Chrome stacks have no `@` in the message;
  - the Origin policy is satisfied.
- **URLs:** a rollup preview of generated URLs.
- **Data safety:** no PII-looking values in clear text.

## 9. Preset library

Each preset is a spec fragment with a description written for a sales engineer.

**Android:**
- **Release regression:** 10.28.0 crashes on the booking confirmation screen (`NullPointerException` in the mapper) for a third of sessions that reach it. Previous versions are clean; rollback happens at a chosen time.
- **ANR on results lists:** 6–12 s main-thread blockage while rendering hotel or product results on low-memory devices.
- **Handled exception storm:** handled parsing exceptions after a backend response change, with no crashes.
- **Slow cold start:** cold starts over 4 s on budget devices after a dependency update.
- **Flaky network:** client timeouts and connection resets on cellular; retries succeed.
- **Slow critical flow:** the `place_order` or `create_booking` root span p95 doubles.

**Web:**
- **Checkout `TypeError` after a release:** unhandled, on the checkout route, Chrome and Safari messages per engine.
- **Chunk load failures after deploy.**
- **Slow product pages on mobile:** LCP p75 3.5–4 s from an unoptimized hero image; desktop is healthy.
- **Promo banner layout shift:** CLS from a late banner on Chromium browsers.
- **Search interaction latency:** INP and TBD from a third-party chat widget.
- **Slow origin:** TTFB regression from the server-rendering tier, traced in end-to-end mode.
- **Online banking route slowness:** single-page app soft-navigation regression on `/accounts/:accountId`.
- **Rage clicks on a failing payment button.**

**Backend** (shared by both platforms; these reuse the V1 incidents):

| Preset | V1 incident | Effect |
| --- | --- | --- |
| Payments pool exhaustion | `payments-db-pool` | Shop and travel fail together; the slow `payment_tokens` lock query is visible |
| Card processor timeouts | `payment-timeouts` | Checkout returns 504 |
| Review media N+1 | `reviews-n-plus-one` | Slow product pages |
| Recommendation latency | `recs-latency` | Recommendations take 1–3 s |
| Inventory sync conflicts | `inventory-sync` | 409 errors on add to bag and checkout |
| Fraud model latency | `fraud-latency` | Transfers hit the 4 s deadline |
| Instant-payments outage | `rails-outage` | 503 from the instant-payments network |
| Core banking latency | `core-banking-latency` | Slow balance inquiries |
| Account read fallback | `account-read-fallback` | Served from a snapshot |
| Statement rendering latency | `statements-slow` | Statement PDFs take 3–6 s |
| Supplier timeouts | `supplier-timeouts` | Partial results |
| PNR creation timeouts | `gds-pnr-timeouts` | Bookings fail with 504 |
| Hotel supplier outage | `hotel-supplier-outage` | Results fall back to cached rates |

New backend presets: cache outage with database fallback, deploy regression on `checkout-orchestrator` with rollback, gateway rate limiting, and Kafka consumer lag.

**Stories** (composites across platforms):
- "Bad release and rollback"
- "Shared payments outage seen in the app and on the website"
- "Peak traffic checkout degradation"
- "Search degradation from a slow supplier"
- "Web release breaks checkout"

**Baseline:** reproduces V1's default distribution.

## 10. Destinations, backfill and demo preparation

### 10.1 Destination modes

Every run, preset and copilot request resolves to one of two modes. The mode is chosen on the run screen with a segmented control, defaulting to the target's last choice.

- **Cortex XCOR RUM sessions only.**
  - Sends Embrace data only, for Android and web.
  - No traceparents, so Embrace forwards nothing and sessions show no "View trace in Cortex XCOR" links. No Chronosphere credentials are needed.
  - Supports up to 24 hours of backfill plus live streaming.
- **End to end (sessions and distributed traces).**
  - Sends Embrace sessions plus Chronosphere backend traces, joined through network span forwarding.
  - Session history can be backfilled; traces exist only from the start of the run onward.

**Backfill in end-to-end mode.** A run that combines backfill with end-to-end mode is split automatically at "now minus 8 minutes":
- earlier sessions are sent as sessions only;
- later and live sessions carry traceparents and backend traces.

The run timeline shows the split: a muted "Sessions only" segment for history and a green "Sessions and traces" segment from the start of the run. Show this caveat verbatim near the mode control and in the run summary:

> Cortex XCOR accepts trace data only for the last 10 minutes, so distributed traces cannot be backfilled. Traces are generated from the moment this run starts; sessions dated earlier are sent to Cortex XCOR RUM without trace links. For a fuller trace history, start the run at least two hours before the demo.

**Also:**
- **Run screen in end-to-end mode:** show "Trace history available from HH:MM" (the run's start time) and a growing count of complete traces from `verify_run`.
- **"Prepare ahead":** starts the run with a planned end-to-end live duration (default two hours) and an optional incident scheduled near the end of that window or during the demo.
- **Consistent messaging:** `get_capabilities` and the copilot state the same limitation and refuse to promise traces for past time ranges.
- **Switching modes:** on a running stream, a switch takes effect at the next session start. Traceparents are never added retroactively.

### 10.2 Backfill rules

**Chronosphere traces** cannot be backfilled beyond 10 minutes (section 5). Any session older than ~8 minutes at send time is generated without a W3C traceparent on its network requests, on Android and web.

**Embrace sessions can be backfilled for the past 24 hours.** Embrace stores event time, partitions by receive date, filters on both, and buckets by event time. Sessions sent now with start times over the past 24 hours therefore appear at their original times in rolling ranges, across:
- session counts and lists;
- crashes and crash-free rate;
- network;
- root spans and logs;
- web vitals, page loads and JavaScript exceptions.

File references are in `V2-SCOPE.md`.

**Rules for a backfill job:**
- **Window:** at most 24 hours, never in the future.
- **Planning:**
  - Plan the whole window first: the daily curve, incidents at their scheduled hours, app version and web release adoption.
  - Then generate sessions in chronological order, so returning visitors, user-session counters, exception counters and release adoption stay consistent.
- **Session realism:**
  - Each backfilled session keeps full SDK-faithful parts, user-session IDs, stable device IDs and realistic part lengths.
  - Web sessions keep their payload grouping per part.
- **No traces for history:** requests older than ~8 minutes at send time carry no traceparent and produce no backend trace.
- **Idempotency:** record every session and part ID in the run manifest and never re-send them. Re-sending double-counts.
- **Throttling:** throttle sending (configurable payloads per second, conservative default) and stop on repeated failures. Embrace's ingest rate limits are not documented in the repositories.
- **Verification:** afterwards, verify with the Embrace API or MCP that hourly session counts and vitals percentiles match the plan.
- **Tell the user**, in the UI and through `get_capabilities`:
  - present history with rolling ranges ("Last 24 hours"), not a calendar "Yesterday";
  - alerts and new-issue alerts do not fire for history;
  - metric exports do not include it;
  - version-over-version views treat it as received today.

**Demo preparation mode** ("I have a demo in 10 minutes") is one action that:
1. runs the readiness checks for the chosen mode and targets;
2. backfills up to 24 hours of Embrace sessions with the chosen story;
3. in end-to-end mode, sends a burst covering the last 8 minutes with full stitched traces;
4. starts or confirms a live stream (with traces in end-to-end mode);
5. optionally schedules the story's incident to begin a few minutes into the demo.

Show a checklist with live status for each step, and in end-to-end mode the caveat with the time traces start. When the demo is more than two hours away, recommend "Prepare ahead".

**Always-current demo data** comes from an always-on hosted stream per demo tenant. Make that a first-class "Stream" object with health, pause and resume, and a daily summary of what it emitted.

## 11. UI (ultra-modern, Cortex XCOR branding)

**Brand:**
- **Color:**
  - Dark-first: a black background (`#000000`) with surface layers around `#0B0B0C` and `#141416`.
  - Cortex green `#00CC66` is the primary accent, with the `#0F9347 → #00CC66` gradient for primary actions and highlights.
  - Red `#FF0000` is reserved for failures and destructive states.
- **Type and layout:** Figtree for all text, with tabular numerals for counts; generous spacing; subtle 1px borders; a soft glow on focused or active elements.
- **Motion:** restrained, 150–250 ms.
- **Accessibility and assets:** WCAG AA contrast, full keyboard navigation, responsive down to a laptop screen. Use official Cortex XCOR logo assets only if the owner provides them.

**Screens:**

1. **Connections.**
   - **Embrace targets:** add Embrace apps (Android or web; app ID, region). Web targets also take origins, the Origin header policy, the site estate, router integration and the SDK self-upload setting.
   - **Chronosphere:** add the tenant (tenant name, API token) for end-to-end runs.
   - **Test connection** sends nothing visible: a Chronosphere token check with an empty export, and an Embrace app lookup.
   - **Readiness checklist and dashboard setup kit:** section 12.
2. **Scenario studio.**
   - **Platform switcher:** Android, Web, or Both.
   - **Preset gallery:** cards grouped as Android, Web, Backend and Stories, each with a one-line outcome.
   - **Fault chips:** selected faults become editable chips with rate controls ("1 in N sessions" or %, with the exact-quota toggle), filters and details.
   - **Population and traffic editors,** including the browser mix and a site archetype selector per line of business (Multi-page, Single-page, Hybrid).
   - **Web performance editor:**
     - a grid of page group × device type × browser family;
     - target p75 per vital, with rating colors from the thresholds;
     - capability warnings inline, for example "Safari does not report CLS";
     - a live preview of the resulting distribution.
   - **Exception editor:** family picker, handling, per-engine message preview.
   - **Incident timeline** over the chosen window: drag to place onset, peak and recovery.
3. **Preview.**
   - Expected counts by fault, platform and line of business.
   - A sample Android session rendered like the Embrace user timeline.
   - A sample web session timeline: page loads, soft navigations, vitals, clicks, exceptions and network.
   - Vitals histograms with threshold bands.
   - A sample exception with its stack.
   - A sample trace waterfall like Chronosphere's.
   - The URL rollup preview, linter results, and the platform limits that apply.
4. **Copilot panel:** chat, suggested prompts, and the proposed spec diff with Apply and Discard.
5. **Runs and streams.**
   - **Status:** the destination mode badge, and the run timeline with the sessions-only and end-to-end segments.
   - **Live counters per platform:** sessions, payloads, accepted or rejected spans, errors.
   - **Trace history** (end-to-end mode): "Trace history available from HH:MM" and the complete-trace count.
   - **Requested versus emitted:** rates per fault, p75 per vital and segment, exception counts per family.
   - **Verification:** results with links into Embrace and Chronosphere.
   - **Controls:** stop and pause.
6. **Settings:** MCP access tokens (create, revoke, last used), the LLM key, the symbol upload token, and defaults.

**Command palette** (Cmd+K) for presets, runs and navigation.

## 12. Readiness checks and dashboard setup kit

For each target, the server derives the exact configuration that the data needs. It shows the configuration as a checklist with copy buttons, exports it as Markdown or JSON, and verifies each item where an API allows it.

| Area | Items |
| --- | --- |
| Embrace network span forwarding | App included with the Chronosphere destination; every API domain on the list; trace-link feature enabled; a short probe proves forwarding before any large run |
| Chronosphere | Dataset and Allow behavior covering the app services (`vantra-android-production`, `vantra-web-production`) and the experience APIs; retention measured on known trace IDs |
| Embrace User Flows | Definitions from `xcorgen/milestones.py` (as in `docs/user-flows.json`), including the shared payment-authorization anchor with `business_unit` filters |
| Saved session filters | From `docs/session-filters.json` |
| Web: allowed origins | Must match the generator's Origin policy |
| Web: Page Group rules | Regex rules per site: Home, Category, Product, Search, Cart, Checkout, Account, Statements |
| Web: source maps | When symbolication is on |
| Embrace verification access | An Embrace API token or the Embrace MCP server |

## 13. Engine changes inside `xcorgen`

**Spec execution:**
- Refactor `runner.Generator` and `scenarios.Plan` to execute a `ScenarioSpec` (targets, fault lists with filters, quotas and schedules) instead of fixed weights.
- Keep V1 profiles, journeys and builders.
- One run can drive several targets. Backend faults and incidents are shared across them.

**Time model and manifest:**
- Every session has a planned start time. The sender decides from section 10 whether its requests carry traceparents and whether backend traces are sent.
- Keep `FORWARD_DELAY_MS` behavior for live traffic.
- Emit a manifest per run (session IDs, trace IDs, fault assignments, vitals targets) for verification, building on `--manifest` and `tools/verify_manifest.py`.

**New Android and backend fault builders:** client `network_failure`, `slow_startup`, configurable `slow_root_span`, and backend `deploy_regression`, `rate_limited`, `kafka_consumer_lag` and `cache_outage`.

**Web: split `xcorgen/web_session.py` into an archetype-aware simulator:**
- **Page and route models:** a hard-load page model (multi-page), a soft-navigation route model (single-page app), and their combination (hybrid).
- **Shared signal emitters:** vitals, LoAF, resources, DOM state, rage clicks, and user, element and server timing.
- **`xcorgen/web_capabilities.py`:** the matrix in section 7.4.
- **`xcorgen/web_faults.py`:** JavaScript exception families with per-engine messages and stacks. Remove the Android families from the web path.
- **`xcorgen/web_vitals.py`:** the target-driven vitals model, with consistent attribution.
- **Site catalogs per line of business:**
  - routes and URL schemes that roll up correctly, and page labels;
  - resource manifests per release (bundles, CSS, fonts, images, third-party tags);
  - Server-Timing entries, user-timing names and element-timing identifiers.
- **Shared super-app mode for web:** the same `business_unit` properties and customer base as Android.
- **Optional symbolication:** bundle and source-map generation and upload.

## 14. Testing and acceptance

**Unit tests:**
- spec validation, quota allocation, limit enforcement, the linter and JSON Patch application;
- each new fault builder;
- the vitals model: emitted p75 within tolerance, ratings consistent, attribution sums;
- capability gating per browser;
- exception messages and stack formats per engine;
- URL rollup preview.

**Contract tests:**
- Android and web payloads pass `tools/check_output.py`. Web payloads are validated per engine against the Chromium, Firefox and WebKit goldens and the 2.29 sample.
- OTLP encoding passes `tools/check_protobuf.py`.
- The security suite (`python3 -m unittest discover -s tests -v`) and the security workflow pass.

**End to end:** test every run mode against a local stand-in for Embrace ingest and an OTLP receiver.

**Live acceptance** before each release, after a 30-minute run on `demo-sandbox` with Android app `5nrc2` and the web app:
1. Emitted rates are within ±2 percentage points of requested.
2. At least 95% of eligible traces are complete with the app span as root, for both `vantra-android-production` and `vantra-web-production`.
3. Embrace shows the sessions, crashes, network domains and root spans for the window.
4. `get_web_vital_distribution`, grouped by `browser_name`, `device_type` and `surface_label`, matches the plan within ±10% on p75. `get_web_vital_timeseries` shows the scheduled regression.
5. `list_exceptions` shows the planned families grouped into stable issues.
6. Page Load Insights shows page-load p95 and resources for Chrome desktop.
7. Pages health shows views, LCP and failures per template.
8. In end-to-end mode, network requests on forwarded domains show "View trace in Cortex XCOR", and the link opens complete traces.

**UI:** Playwright smoke tests for each screen, and a visual review against the brand rules.

## 15. Phases

1. **Spec and engine.**
   - Build: ScenarioSpec, validation, quotas, limits, the linter and the CLI `run spec.json`; V1 Android behavior reproduced from a spec; the burst-window fix.
   - Accept when V1's verification plan passes from specs.
2. **Web generator.**
   - Build: section 7, the web exception fix, shared super-app web mode and capability gating.
   - Accept when dry runs pass the per-engine checks and a live run on the web app meets acceptance items 4–7.
3. **Server and runs.**
   - Build: FastAPI, SQLite, the job runner, SSE, receipts, verification, and multi-target runs.
   - Accept when live and burst runs can be started, observed and stopped over the API.
4. **UI.**
   - Build: Connections with readiness and the setup kit, the studio (including the web performance editor), Preview, and Runs.
   - Accept when a sales engineer can go from empty to a verified stream without the CLI.
5. **MCP and copilot.**
   - Build: the tools, auth, and the copilot panel with diffs.
   - Accept when the example requests in 8.1 produce correct validated patches, and Claude Code connected over MCP can preview and start a run with confirmation.
6. **Destinations, demo preparation and backfill.**
   - Build: section 10 end to end.
   - Accept when:
     - a sessions-only 24-hour backfill shows the planned hourly session counts and vitals in Embrace for "Last 24 hours", and emits zero traceparents;
     - an end-to-end run with backfill shows no "View trace" links on sessions older than 8 minutes, and complete traces for every eligible request from the run start;
     - the caveat appears on the run screen, in the run summary, in `get_capabilities` and in copilot answers about past traces.
7. **Hosted always-on stream:** container deployment with secrets, health and daily summaries.

**Later:** iOS sessions (read `embrace-apple-sdk` for the envelope and span formats; one app ID per platform), backend RED metrics, multi-region runners, and symbolication if not done in phase 2.

## 16. Before you start

Read the code paths in section 0, run the V1 dry-run checks from `AGENTS.md` to establish a baseline, and confirm with the owner:
1. **Hosting:** local only, or a server for a team. This decides authentication and secret storage.
2. **LLM key:** whether the copilot may use the owner's Anthropic API key, and which model.
3. **Web app:** the web app ID, or a go-ahead to create "Vantra" (web, production); and the allowed origins configured for it.
4. **Web design choices:** the site estate and archetype per line of business; whether SDK self-upload requests should appear as network calls; whether to set up symbolication (requires a symbol upload token); and the Page Group rules to create.
5. **Platform access:**
   - Chronosphere configuration permissions, to create or extend the retention dataset.
   - Embrace verification access: an API token or the Embrace MCP server.
6. **Volume:** a 24-hour backfill at today's live rate is about 14,000 sessions per platform. Confirm the tenant can take bursts of that size.
7. **Forwarding health:** after the 2026-10-30 gap, a recent session's network request should show the forwarding badge "Forwarded" in Embrace.
