# Handoff: finish Session-Generator V2

> Current status: phases 4.1–4.9 were completed. The owner subsequently requested a simpler four-step UI, no AI/MCP UI, and finite continuous sending. See the current UI direction in `AGENTS.md` and the start/workflow instructions in `README.md`; those newer requirements supersede this document’s original studio/Copilot design. Tenant configuration and live acceptance remain owner-controlled.

You are taking over an unfinished build. Assume you know nothing about this project. This document explains what the product is, how every existing part works, exactly what is done, and exactly what you must build, in order, until **all of V2 is complete**. Read it fully before changing anything, then read the files it tells you to read.

Repository: `https://github.com/Aissa-emb/Session-Generator` (private). Work on branch **`v2`**. A local checkout is at `~/Session-Generator-V2` on the owner's Mac (macOS, Python 3.14, Node 25, `gh` authenticated as `Aissa-emb`). If you start from a fresh clone, run `git checkout v2`.

---

## 1. What this product is

**Session-Generator** produces synthetic but production-realistic telemetry for a sales/demo environment of two observability products owned by the same company:

- **Cortex XCOR RUM** (the product formerly called **Embrace**): real-user monitoring for mobile apps and websites. Apps embed an Embrace SDK that sends *sessions* (spans and logs describing what a user did, network calls, crashes, freezes, web vitals, JavaScript exceptions) to Embrace ingest. In code, APIs, URLs and SDK names it is still called Embrace (`a-<appId>.data.emb-api.com`, `@embrace-io/web-sdk`, `embrace-android-sdk`). In anything user-facing, call it Cortex XCOR RUM.
- **Cortex XCOR** (formerly **Chronosphere**): an observability platform that stores distributed backend traces (OpenTelemetry/OTLP). API hosts are `<tenant>.chronosphere.io`. User-facing: Cortex XCOR.

The generator sends data **directly** to both, in the exact wire formats the real SDKs and OpenTelemetry instrumentations produce, so a salesperson can demo: "a customer's checkout failed in the app → open the network request → **View trace in Cortex XCOR** → see the backend root cause (a database connection pool exhausted in `payment-authorization-service`)".

### 1.1 How the two products are stitched together ("end to end")

```
Android app session (Embrace envelope)          Backend services (OTLP protobuf)
  OkHttp network span                               api-gateway  SERVER  (parent = client span id)
    trace_id = T, span_id = C                         -> experience API -> domain services -> DB/cache/Kafka
    emb.w3c_traceparent = 00-T-C-01
        |                                                     |
        v  POST a-<appId>.data.emb-api.com/v2/spans            v  POST <tenant>.chronosphere.io/data/opentelemetry/v1/traces
   Embrace ingest -> session processed ->                 Chronosphere
   Network Span Forwarding (NSF) sends span C  ---------->  trace T: root = C (service <app>-android-production)
```

- For every simulated API call the generator creates a W3C traceparent, puts it on the client network span (`emb.w3c_traceparent`), and builds a backend trace with the same trace ID whose root's parent is the client span.
- Embrace's **Network Span Forwarding (NSF)** forwards the client span to Chronosphere after the session part ends and is processed; it becomes the trace root. The generator never sends the client span itself (an option `emit_client_spans` exists only for apps without NSF).
- The Embrace dashboard shows "View trace in Cortex XCOR" only when that request's NSF outcome was success, the app has a Chronosphere destination and tenant, and the organization has the trace-link feature enabled. NSF only forwards `perf.network_request` spans, only for apps included in forwarding and domains on the forwarding list; forwarding is decided once, at ingest; config changes take ~5 minutes to apply.
- **Platform limits (verified, treat as physics):** Chronosphere drops spans whose start time is more than 10 minutes before or after ingest, while still answering the OTLP request with success. Therefore traces can only exist for recent activity; the generator strips trace context from any session dated more than **8 minutes** before it is sent. Embrace accepts sessions dated up to 24 hours back and shows them at their original times in rolling ranges ("Last 24 hours"), but alerts, metric exports and version-over-version views do not include that history, and re-sending the same session double-counts. Embrace ingest returns HTTP 200 even for payloads it discards, so delivery must be verified through the Embrace API or MCP server, never HTTP status.

### 1.2 The demo world ("Vantra")

One fictional company, **Vantra**, with three lines of business sharing one customer base:

| Line of business (`business_unit`) | V1 profile key | API domain | Backend services (examples) |
| --- | --- | --- | --- |
| `retail` (shop) | `ecommerce` | `api.vantrashop.com` | `shop-experience-api`, `product-catalog-service`, `checkout-orchestrator`, `inventory-availability-service`, ... |
| `banking` (money) | `banking` | `api.vantramoney.com` | `banking-experience-api`, `account-summary-service`, `funds-transfer-service`, `payment-rails-gateway`, ... |
| `travel` | `travel` | `api.vantratravel.com` | `travel-experience-api`, `air-shopping-service`, `reservation-service`, `pnr-management-service`, ... |

Shared platform services: `api-gateway`, `customer-identity-service`, `customer-profile-service`, `payment-authorization-service` (shop checkouts and travel bookings both authorize there, so one payments incident hits both), `fraud-risk-scoring-service`, `customer-notification-service`.

Live deployment:
- **Android app** `5nrc2` "Vantra" (production, US) presented as one super app `com.vantra.android`; sessions are told apart by session property `business_unit`.
- **Web app** `kfs9f` "Vantra Web" (web, production, US). Not used yet (the web generator is unfinished). Embrace names forwarded client spans `lower(app name with spaces → "_")-<platform>-<environment>`, so this app forwards as **`vantra_web-web-production`** unless the owner renames it to "Vantra" (then `vantra-web-production`). Always derive names with `xcorgen.limits.forwarded_service_name()`.
- **Chronosphere tenant** `demo-sandbox`. It has a dataset `vantra-end-to-end` with a managed Allow behavior (100% retention) for `vantra-android-production`, `shop-experience-api`, `banking-experience-api`, `travel-experience-api`; web services must be added before web end-to-end acceptance.
- **Always-on V1 stream:** a macOS LaunchAgent `local.xcor-demo-generator` runs V1 (`generate.py --profile all --rate 10 --quiet`) from the separate checkout `~/xcor-demo-generator`. **Never switch branches, edit files or restart anything in `~/xcor-demo-generator`** without the owner's explicit approval; it feeds the live demo.
- Credentials: Chronosphere token file `/Users/aissamamdouh/Library/Application Support/Session-Generator/chronosphere.token` (owner-only). Never print, log or commit secrets.

### 1.3 Owner decisions already made (do not re-ask)

- Hosting: **local Mac first** (uvicorn, single user, secrets in the macOS Keychain); add the container for an always-on hosted stream in phase 7.
- Copilot: the owner has **no Anthropic API key yet**. Build the copilot completely, but it must be disabled with a clear "Add an Anthropic API key in Settings" state until a key is stored. Default model `claude-sonnet-5-5`, selectable `claude-opus-5-5`. Build the MCP server first; it needs no key.
- Web estate accepted: `https://www.vantra.com` hosts retail (multi-page, server-rendered) and travel (hybrid: server-rendered landing and search pages, client-routed booking flow under `/travel/book/...` on the **same origin**); `https://app.vantra.com` hosts online banking (single-page app); `https://static.vantra.com` is the CDN. Archetype defaults: retail `multi_page`, banking `single_page`, travel `hybrid`. (The original prompt put the booking flow on app.vantra.com; that would split user sessions because storage is per origin, so it stays on www.)
- Web app ID `kfs9f`.

### 1.4 Non-negotiable rules

1. **Nothing may reveal the data is generated.** Never put "demo", "fake", "test", "sample", "synthetic", "mock", "dummy", "placeholder", "staging" or similar into anything sent to Embrace or Chronosphere: session properties, attribute keys/values, service names, span names, URLs, page titles, CSS selectors, bundle names, IDs, log bodies, user IDs, device names. Copilot output too. The realism linter (`xcorgen/linter.py`) enforces this and blocks runs. Local run manifests may record provenance; it is never emitted.
2. **SDK-faithful formats.** Every signal must match what the real SDK emits. Before adding a signal, read the SDK source or a real capture. Local SDK clones: `~/Claudecode/embrace-android-sdk`, `~/Claudecode/embrace-apple-sdk`, `~/Claudecode/embrace-web-sdk`; Embrace backend `~/Claudecode/go`; dashboard `~/Claudecode/dashjs`. When a document and the SDK disagree, the SDK wins; record the discrepancy in `AGENTS.md`.
3. **Device- and browser-faithful.** Emit a signal only if that device/browser would produce it (capability matrix, `docs/v2/web-sdk-behavior.md`).
4. **Enterprise realism on the backend.** Service names: `<line-of-business>-experience-api`, `*-service`, `*-orchestrator`, `*-gateway`, `*-worker` (never `-bff`). Errors are real root causes with correct exception types, messages and stack traces shaped for the service's language (`telemetry.sdk.language`).
5. **Platform limits are physics.** Never promise data the platforms drop; the UI, API, MCP and copilot must state the limits (`xcorgen/limits.py` holds the verbatim texts).
6. **Secrets.** Embrace app IDs are not secret. Chronosphere tokens, Embrace API and symbol-upload tokens, and LLM keys are. Store them server-side only (macOS Keychain locally; environment variables or mounted files in the container), never return them to the browser after saving, never log them, never put them on a command line (`ps` shows arguments). Keep the rules in `SECURITY.md` and `xcorgen/security.py` (HTTPS with certificate checks, no redirects, no credentials in URLs, bounded responses, header-injection checks).
7. **Professional, precise copy** everywhere user-facing: no analogies, no casual wording; labels say what a control does.
8. **Git.** Commit as `Aissa-emb <200142933+Aissa-emb@users.noreply.github.com>` (already set in the repo's local git config). **No AI attribution trailers** (no Co-Authored-By, Assisted-by, "Generated with"). Push with `git -c credential.helper= -c "credential.helper=!gh auth git-credential" push`. Keep `.github/workflows/security.yml` passing (gitleaks over full history, unit tests, dry-run payload checks). Commit in coherent steps with descriptive messages; open a pull request from `v2` to `main` when phases are complete (PR text must not mention AI tools).
9. **Outbound actions need the owner.** Dry runs are free. Do not send live data to Embrace or Chronosphere, change tenant configuration, upload source maps, or restart the LaunchAgent without the owner's explicit go-ahead for that action. Before any live acceptance test, run the forwarding readiness probe (section 7.3).

---

## 2. Read these first

1. `docs/v2/V2-BUILD-PROMPT.md`: the complete product requirements for V2 (sections 0–16). It is the specification; this handoff tells you what is done and how to finish. Requirements in it that are not listed as done below are **your work**.
2. `docs/v2/V2-SCOPE.md`: verified platform findings (backfill, Chronosphere limits, Embrace query semantics, web findings).
3. `docs/v2/web-sdk-behavior.md`: the verified Web SDK 2.31 behavior (exact keys, types, triggers, per-engine differences). The web generator must follow it exactly.
4. `AGENTS.md` (project handoff notes, including the V2 section), `README.md`, `SECURITY.md`.
5. The code, in this order: `xcorgen/spec.py`, `xcorgen/planner.py`, `xcorgen/spec_runner.py`, `xcorgen/emitted.py`, `xcorgen/journeys.py`, `xcorgen/android_session.py`, `xcorgen/web_session.py` (V1 web, to be replaced), `xcorgen/calls.py`, `xcorgen/backend.py`, `xcorgen/profiles/*.py`.

---

## 3. Architecture of what exists

Everything is stdlib-only Python (no packages) so the CLI and LaunchAgent need no installs. Keep the engine stdlib-only; the server (phase 3) may use packages.

### 3.1 V1 engine (still the core; do not rewrite payload builders)

| File | What it does |
| --- | --- |
| `generate.py` | CLI. V1 flags (`--profile`, `--sessions`, `--rate`, `--platform`, `--incident`, `--dry-run DIR`, ...) and V2 subcommands (`run`, `validate`, `explain`, `preset`, `report`, `lint`, `capabilities`). |
| `xcorgen/runner.py` | `load_config` (config.json + env, token files), `Sender` (gzip POSTs to Embrace `/v2/spans|logs` and Chromium OTLP; dry-run writes one JSON file per delivery; checks OTLP partial-success), `Generator` (owns profiles, persona pools, a shared backend `Runtime`; `pick()` chooses line of business + visitor; `session(start_ms, skeleton=None, assignment=None, settings=None)` builds and runs one session and returns `(session, journey_name, deliveries)`), `run_burst`, `run_live`, `DIURNAL` curve, `append_manifest` (owner-only JSONL). |
| `xcorgen/profiles/{ecommerce,banking,travel}.py` | One line of business each: `services(...)` (backend `Service` objects with language, version, k8s namespace, pods), `ENDPOINTS` (`Endpoint(method, route, path, build, outcomes, size)`; `build(ctx)` returns `(root Node, HTTP status, data)` and branches on `ctx.outcome` and `ctx.incident`), V1 web `PAGES`, `PROFILE.android` (screens, package, versions), `make_user`, `properties`, `incidents` (forced-outcome weights per V1 incident). |
| `xcorgen/profiles/platform.py` | Shared hops: `gateway()` (api-gateway → experience API, token check, rate limiter), `profile_lookup`, `risk_score`, `authorize_payment(mode)` (Hikari pool exhaustion, processor timeouts, declines), `notify` (Kafka). |
| `xcorgen/backend.py` | Backend call-tree engine. `Node(svc, name, kind, role, attrs, ms, children, parallel, error, async_lag, timeout_ms, min_ms)`; helpers `server`, `http`, `grpc`, `sql`, `redis`, `nosql`, `kafka` (consumer laid out after producer with `async_lag`), `external`, `internal`, `err`, `stack(lang, type, message, frames)` (language-shaped stack text). `build_trace(root, trace_id, parent_span_id, start_ms, rng, context)` lays out timing and emits OTLP spans (per-service resources with k8s/AWS attributes and real instrumentation scope names). |
| `xcorgen/calls.py` | `simulate(session, key, params, start)`: asks the session's `Plan` for the outcome, builds the backend trace (or a forced `http_error` via `generic_faults`, or an idempotent replay via `operations`), lays out DNS/TCP/TLS/request/response timing, handles transport failures (`_transport` param: `timeout`/`reset` reached the server and keep a backend trace; `dns`/`offline`/`tls`/`cancelled` do not). `deliver_backend` queues the backend trace "held" until the session part ends; `release_backend` sends it 3–15 s after part end (`FORWARD_DELAY_MS`), just ahead of Embrace forwarding. |
| `xcorgen/scenarios.py` | `Plan`: the per-session story. V1 mode draws a primary scenario (clean/mobile/backend/slow/handled), incident, client fault (crash/blockage), crash family and transport failure from fixed weights. V2 mode receives an `assignment` from the planner (`Plan._assigned`). `Plan.request(key)` decides each call's outcome and request context (feature flags, release ring, incident phase/occurrence). `assign_ring`, `incident_phase_at`, `occurrence_id`, `Runtime` (shared cache and idempotency state). |
| `xcorgen/journeys.py` | Business journeys shared by Android and web: `ecommerce`, `banking`, `travel`. They call the session interface (`page_view`, `click`, `api`, `parallel`, `flow`, `log`, `wait`, `type_text`, `login`, `logout`, `leave`, `background`, `breadcrumb` via `milestones.mark`). In V2 mode they read `plan.shape` (planner-decided journey: priority flow, short visit, branch) instead of drawing it. `traced_attempt` wraps business calls in named root spans (`ROOT_OPERATIONS`); `crash_after` crashes right after a named response; `client_issue` places freezes, handled errors and stage crashes; `done` adds a handled follow-up after visible failures. |
| `xcorgen/client_faults.py` | Android crash families (`FAMILIES`, `fault()`), `followup()` (handled `retrofit2.HttpException`/transport exception after a failed request), `blockage()` (main-thread freeze as `perf.thread_blockage` with samples). |
| `xcorgen/android_session.py` | `AndroidSession`: builds Embrace Android SDK 9.3 envelopes (`resource`, `metadata`, `version 0.1.0`, `type spans|logs`, `data`), parts, startup span tree (with slow-startup regression), screen views, taps, OkHttp network spans (traceparent only when `self.traces`), logs, crashes (`sys.android.crash`, process termination, upload on relaunch). All attribute values are strings. `pick_version`, `shift_envelope` (time shift). |
| `xcorgen/web_session.py` | **V1 web session (defective; to be replaced in phase 2).** Raises Android exception families in JS stacks, uses old brands (Northline/Corvia/Vantra Travel), one part per page, vitals derived from timings. |
| `xcorgen/personas.py` | Visitor pools persisted in `.state/` (device IDs, user IDs, session counters, 30-minute last-seen rule using the planned clock). `ANDROID_DEVICES` + `DEVICE_TIERS`; `BROWSERS` (family, version, engine, Chromium version, OS, device type, UA, screens, weight; updated in phase 2 work) and the V1 `DEVICES` view; `_new_persona(profile, rng, overrides)` with browser/device-type/connection mixes. |
| `xcorgen/otlp.py` | OTLP JSON helpers (`attrs()`: bool→boolValue, int→intValue, float→doubleValue, list→arrayValue, None skipped), protobuf encoder (verified by `tools/check_protobuf.py`), partial-success decoder. |
| `xcorgen/milestones.py` | Breadcrumb catalog and User Flow definitions (sparse: only flow anchors and key context are emitted). |
| `xcorgen/operations.py` | Idempotent status reconciliation endpoints (`operation_status`, `hotel_revalidate`). |
| `xcorgen/pool_evidence.py` | Hikari pool "occupant" companion traces linked from the waiting span. |
| `xcorgen/security.py` | Endpoint/header validation, owner-only token files, bounded responses, no redirects. |
| `tools/check_output.py` | Validates a dry-run directory: Android envelopes against SDK rules and goldens; V1 web payloads against `reference/web-sdk-2.31.0-capture`; every backend trace has exactly one root whose parent is the client span; timing containment. |
| `tools/calibrate.py`, `tools/verify_manifest.py`, `tools/chronosphere_traces.py`, `tools/check_protobuf.py` | V1 calibration, live manifest verification, Chronosphere Data API queries (`POST /api/v1/data/traces`, `TRACE_IDS` without time range, `SERVICE_OPERATION` with ≤15-minute window), encoder check. |

### 3.2 V2 phase 1 (done, committed): the ScenarioSpec layer

A run is one JSON document, the **ScenarioSpec** (`spec/scenario.schema.json`, JSON Schema 2020-12, `spec_version` "2.0"). Fields: `targets[]` (Embrace apps: id, platform android/web/ios-reserved, `embrace_app_id`, `app_name`, environment, region, `api_domains` per line of business, `android{package, ui_toolkit, versions[{version, version_code, sdk_version, released, share}]}`, `web{sdk_version, estate{lob: {origin, archetype}}, cdn, releases[{version, deployed, share, bundles}], router_integration, sdk_upload_spans, origin_header{policy, value}, propagation_allow_list}`), `chronosphere{tenant, token_ref}`, `destinations{mode sessions_only|end_to_end, backend_logs, backend_metrics, emit_client_spans}`, `population{platform_mix, line_of_business_mix, signed_in_share, returning_share, journey_depth, session_properties, android{device_weights, os_version_mix}, web{browser_mix, device_type_mix, connection_mix}}`, `traffic{mode live|burst|backfill, rate_per_minute, duration_minutes, daily_curve{flat|diurnal|custom, hourly_weights, time_zone}, count, window{last_minutes}|{start,end}, throttle_payloads_per_second}`, `client_faults[]`, `web_performance[]`, `backend_faults[]`, `incidents[]`, `rollouts[]`, `verification_cases[]`, `seed`, `stories`, `notes`. Fault entries: `{id, type, platform, rate, quota auto|exact|probability, exclusive_group, steer_journey, filters{lines_of_business, app_versions, web_releases, os_versions, device_models, device_tiers, browser_families, browser_versions, device_types, connections, screens, page_groups, routes, endpoints, signed_in, release_rings}, details{...}}`; backend faults add `variant, service, endpoints, lines_of_business, platforms, schedule, blast_radius`.

How a spec runs:

```
spec.json --spec.validate()--> Planner --skeleton per session--> allocation (quota.py) --> Planner.assignment()
          (schema, semantics,   (target, line of business, visitor,      (exact or probability,    (V1 Plan fields: story,
           limits, linter)       app version, ring, start time, trace      exclusive groups,         incident, client fault,
                                 eligibility, journey shape)               availability)             transport, http errors...)
      --> Generator.session(..., skeleton, assignment) --> V1 journeys and builders --> deliveries
      --> SpecRun (time shift per session, strip traces older than 8 min, manifest, send) --> Emitted report (payloads only)
```

| File | Role |
| --- | --- |
| `xcorgen/schema.py` | Stdlib validator for the schema's keyword subset; rejects unknown keywords at load. |
| `xcorgen/spec.py` | `default_spec()`, `ANDROID_TARGET`, `WEB_TARGET`, `normalized()` (fills defaults; fatal faults default to group `fatal`; all backend faults share one group), `validate(spec, now_ms)` → issues `{severity, path, message, code}` (errors block runs), `apply_patch` (RFC 6902), `explain()` (plain language), incident windows (`incident_window`, `phase_at`, `ramp`), `traffic_window`, `JOURNEY_ORDER`, `CRASH_STAGE_ENDPOINT`. **Web targets are currently rejected by validation** (remove that once phase 2 works). |
| `xcorgen/limits.py` | Verified limits, `TRACE_CAVEAT` (verbatim UI text), `BACKFILL_NOTES`, `PLATFORM_LIMITS`, guardrails (`MAX_SESSIONS_PER_JOB`, `MAX_RATE_PER_STREAM`, `MAX_STREAMS_PER_TARGET`), `trace_eligible()`, `forwarded_service_name()`, `capabilities()`. |
| `xcorgen/catalog.py` | `CLIENT_FAULTS` (type → platforms, supported platforms, fatal, summary; web types listed but unsupported), `NETWORK_FAILURES`, `BACKEND_VARIANTS` (17 variants mapping V2 types to V1 incidents with `story`, `endpoints` per lob, `ends_journey`, `evidence`), `EVIDENCE`, `PLANNED_BACKEND_TYPES` (`cache_outage`, `rate_limited`, `kafka_consumer_lag`, `deploy_regression`: rejected until built), `ROOT_SPANS`, `resolve_variant`, `describe()`. |
| `xcorgen/quota.py` | `allocate_exact` (count fixed from all eligible sessions, placed only where `available`, systematic sampling over time-bucket-shuffled order), `StreamingQuota` (exact at block boundaries for live), `draw_probability`. |
| `xcorgen/planner.py` | `Fault` (normalized fault: lobs, window, variant, `endpoint(lob, skel)`, `family()`, `rate_at`, `onset_rate_at`, `requested_at`), `Planner` (`skeleton`, `eligible` (denominator), `available` (placement: exclusive groups, nothing after a fatal crash or `ends_journey` failure, journey-shape compatibility), `allocate_batch`, `allocate_one`, `assignment`, `settings` per session), `natural_shape`, `reach`, `steer`. |
| `xcorgen/spec_runner.py` | `SpecRun`: builds one `Generator` per target (shared super-app, overrides from the target, shared `Runtime`), verification cases (V1 forced cases), `run_batch` (plan all sessions, generate in time order, per-session shift only if still running at send time, strip traces, record, send backend first then Embrace, throttle), `run_live` (simulatable clock), `open_manifest` (`.runs/<run id>/manifest.jsonl` or the dry-run dir), `strip_traces`, `app_overrides`, `planned_starts`, `curve_factor`. |
| `xcorgen/emitted.py` | Requested vs emitted from payloads only: `observe()` (Android envelopes + backend traces → session facts), `affected()` per fault type/variant evidence (client statuses, latency, breadcrumbs, logs, backend spans), `eligible()`, `Emitted.summary()/table()`, `from_dry_run(dir, spec)`. **Android only today.** |
| `xcorgen/linter.py` | `lint_spec` (emitted spec fields), `lint_payloads` (forbidden words with SDK-term allowlist, PII outside identifier fields, service names, stack language vs runtime, durations, HTTP vs span status, device/OS combos), `lint_dry_run`. |
| `xcorgen/presets.py` | `catalog_presets()`: `baseline` (V1 default policy as fault rates), Android presets, one per backend variant, stories; `apply()`, `merge()`, `listing()`. |
| `xcorgen/generic_faults.py` | `http_error` traces for any endpoint (`ENDPOINT_SERVICE`, language-specific exceptions per status family). |
| `xcorgen/secrets.py` | `resolve(token_ref)` for `keychain:service[/account]`, `file:/abs/path`, `env:VAR`. |
| `spec/examples/` | `baseline-burst.json`, `live-end-to-end.json` (V2 equivalent of the LaunchAgent), `verification-plan.json` (V1's 45 verification cases). |
| `tests/test_spec.py` | Validation, patch, quotas, trace window, copilot example 1, Baseline reproduction (end to end), new Android faults, verification plan, backfill, live (simulated clock), linter. |

Rules the numbers depend on (keep them true): rates are shares of eligible sessions; exact quotas match within one session; one backend story per session; fatal faults share one group; `details.with_fault` couples a fault to another's sessions; the payments pool phase is fixed per session from its planned start; sessions older than 8 minutes at send carry no `emb.w3c_traceparent`/`emb.forward_telemetry` and no backend trace; latency-only evidence without traces is labeled approximate (±2 pp).

### 3.3 Phase 2 work in progress (committed, not yet wired into the engine)

| File | State |
| --- | --- |
| `xcorgen/web_sites.py` | Done as data: the Vantra estate pages per lob (`RETAIL`, `BANKING`, `TRAVEL` dicts of `Page(key, route, group, render server|client, label, path, lcp (selector, element, image path fn), cls selector, doc_height, elements, measures, chunk, controls)`), `BRAND`, `PAGE_GROUP_RULES`, `THIRD_PARTY` tags (timing = Timing-Allow-Origin), `THIRD_PARTY_SCRIPTS`, `first_party(cdn, lob, release, page)` assets, `content_hash`, `hex_id`, `server_timing()`. Page keys match the journeys' `page_view` keys. Banking should switch to a webpack-style asset layout (`/money/static/js/main.<hash>.js`, `742.<hash>.chunk.js`) so `ChunkLoadError` is realistic there; retail/travel stay Vite-style. |
| `xcorgen/web_capabilities.py` | Done: `capabilities(persona)` flags (cls, inp, loaf, element_timing, event_timing, device_info, render_blocking_status, delivery_type, resource_status, not_restored_reasons, navigation_api, native_soft_navigation, soft_navigation_polyfill, soft_navigation_vitals, fractional_time) and `MATRIX`/`unsupported()` for validation. `event_timing` is true for Chromium and Gecko (polyfill soft-nav spans on Firefox 147+); INP stays off for Gecko and WebKit until verified. |
| `xcorgen/web_vitals.py` | Done: thresholds, `rating`, baselines per device type and page group, `targets()` from spec `web_performance`, log-normal timings calibrated on p75 with a shared latent per page load, zero-inflated CLS, `PageVitals` with `ttfb_parts`, `lcp_parts`, `inp_parts`, `precision()` (Chromium fractional, others integers). |
| `xcorgen/web_faults.py` | Done: JS exception `FAMILIES` with per-engine messages, `HANDLING` defaults, `exception(family, engine, release, page_bundle, vendor_bundle, chunk_url, chunk_id)` → `(type, name, message, stack)` with engine-specific stack formats and release-stable minified names. |
| `xcorgen/personas.py` | `BROWSERS` catalog with engines and versions; web overrides. |
| `reference/web-sdk-golden/` | Per-engine (chromium, firefox, webkit) integration goldens from the SDK repo. |
| `reference/web-sdk-2.29-sample/web_2_29_0_sample.scrubbed.jsonl` | **What a real deployed web session looks like**: four `/v2/spans` and `/v2/logs` payloads (JSON lines, each with `device_id`, `receive` and the OTLP body) from a production single-page app on Chrome 155 (SDK 2.29): session parts with soft-navigation rollovers, route-template surfaces, documentLoad with resources, fetch and XHR spans (including cross-origin ones with only fetchStart/responseEnd and third-party status 0), custom spans, element timing, the native Soft Navigation span with correlation arrays, LoAF, DOM state, user timing, scroll depth and logs. Personal and account data were removed by `tools/scrub_web_sample.py` (IDs replaced consistently, so traceparents and parent links still match). Study it before building `BrowserSession`; never commit the unscrubbed original from `~/Claudecode/go/tool/internal/schema-stats/testdata/`. |
| `docs/v2/web-sdk-behavior.md` | Verified SDK behavior. |

---

## 4. What you must build (all of it), in order

Each phase ends with: tests passing (`python3 -m unittest discover -s tests -v`), the V1 dry-run checks passing (section 6), CI green, docs updated (`AGENTS.md` V2 section, `README.md`), and a commit pushed to `v2`. Do not stop after a phase; continue until section 4.9 is done.

### 4.1 Phase 2: the web generator

Goal: web sessions indistinguishable from `@embrace-io/web-sdk` 2.31 for B2C multi-page, single-page and hybrid sites, filling Web Vitals, Pages health, Page Load Insights, JavaScript Exceptions and the user timeline, optionally stitched to backend traces. Follow `docs/v2/web-sdk-behavior.md` for every key and rule. Requirements are in `V2-BUILD-PROMPT.md` section 7.

**A. `xcorgen/browser_session.py` → class `BrowserSession`** implementing the same interface as `AndroidSession` so `journeys.py` drives it unchanged: `page_view(key, **params)`, `click(element_html, selector=None, breadcrumb=None)`, `breadcrumb(message)`, `api(key, **params)`, `parallel(*calls)`, `flow(name)` (context manager → custom span in scope `embrace-web-sdk-traces`, `emb.type=perf`), `log(severity, message, **attrs)`, `exception(exc_type, message, handled, function)` (map V1/Android callers to a JS family; never emit Java/Kotlin types), `wait`, `type_text`, `login`, `logout` (endUserSession → part end `user_session_ended`, final, `manual`), `leave` (page hidden → `background`), `background(lo, hi)` (tab hidden then visible: `background` / `foreground` part, same page id), `finish()`, attributes `t`, `start_ms`, `end_ms`, `user_session_id`, `user_id`, `persona`, `profile`, `plan`, `calls`, `deliveries`, `part` (with `.id`), `properties`, `traces`, `api_host`, `connected_hosts`, `client_user_agent`, `app_version`, `emit_client_spans`, `client_resource_spans(call)`, `_deliver(...)`. Add `long_task(duration, script)` so `client_faults.blockage()` maps to long animation frames on web.

Constructor inputs: profile (lob), persona (browser), app ID, start time, plan, shared=True, `web` (target web block), release, origin policy, spec `web_performance` entries. Derive: `lob`, site pages, `origin`, `archetype`, `cdn`, `caps = capabilities(persona)`, fractional timing, per-origin storage state in `persona["web_origins"][origin]` = `{device_id (32 uppercase hex), user_session_number, session_part_number, previous_user_session_id, exception_number, external_user_id}` (separate per origin), app instance ID per session (new tab), resource attributes per `web-sdk-behavior.md` (Chromium-only keys gated). Deliveries set `headers` explicitly: `User-Agent` and `Origin` only per the target's `origin_header` policy (extend the schema enum with `site`: send each page's own origin; default `none`).

`page_view` decides the navigation:
- First page of the session → **hard load** (`init` part, `cold_start=true`).
- `multi_page` → every page is a hard load: the current page's surface span ends, the part ends `background` (page hidden by navigation, spans exported, logs flushed), a short gap, then a new SDK instance: new part `init`, `cold_start=true`, new `app.surface.id`, `documentLoad`/`documentFetch`/`resourceFetch` (only if the load event fires), vitals, server timing, element timing, dom-state/scroll logs at part end, `emb.page_load` on the page's first part. Insert realistic back/forward navigations (about 8% of MPA navigations): restored from bfcache when the engine allows (`bfcache_share` default 0.6): part `foreground`, `cold_start=false`, no `emb.page_load`, same page ID as the restored page, vitals re-reported with `navigation_type=back-forward-cache` (TTFB 0, small FCP/LCP); otherwise a fresh load with `browser.navigation_timing.type=back_forward` and Chromium `not_restored_reasons` on documentFetch and vitals.
- `single_page` → **soft navigation** after the first load: if `caps.navigation_api`, the part ends `web_soft_navigation` and a new part starts `web_soft_navigation` at one shared boundary; otherwise the same part continues and only the surface span changes. New page ID and surface span (route template as name and `app.surface.name`, label from the page, `browser.url.full` read at span end so the outgoing span carries the new URL). Emit the `Soft Navigation` span (native on Chromium 151+, polyfill where Navigation API + Event Timing exist and the triggering click lasted ≥16 ms) with correlation arrays of span IDs/types and log IDs/types inside its window. Native soft navigations also report FCP/LCP (and later CLS/INP) with `navigation_type=soft-navigation`, `navigation_id`, `interaction_id`.
- `hybrid` → hard load when `page.render == "server"`, soft navigation otherwise.

Hard load timeline (all times from navigation start = zero time): draw `PageVitals` from `web_vitals.targets(spec.web_performance, page.group, device_type, browser)` plus active web faults; connection reuse for later pages on the same origin; `ttfb_parts` gives documentFetch events (fetchStart, domainLookup*, connect*, secureConnectionStart only when TLS, requestStart, responseStart = TTFB, responseEnd); resources discovered after the HTML (blocking CSS, entry/vendor/route chunks, font via CSS, LCP image at TTFB + resourceLoadDelay for resourceLoadDuration, product/hero images, third-party tags with `cors_opaque` sizes 0 and only fetchStart/responseEnd when the vendor sends no Timing-Allow-Origin); returning visitors get cache hits (`transferSize 0`, `delivery_type=cache` on Chromium) and occasional `cache_revalidated` (transferSize 300); domInteractive, DCL, domComplete, load event after the last load-blocking resource; paint events per engine; SDK init → part start (`emb.sdk_startup_duration` 5–60 ms); TTFB log at zero time, FCP at FCP time, LCP reported on first input or page hide with the LCP timestamp, CLS on hide (Chromium only) with `clsLayoutShifts` built from the page's CLS selector, INP on hide only after an interaction (Chromium only) with `interactionTarget`, `element_type` and `loaf_scripts`; vitals' `emb.session_part_id` chosen by timestamp; element-timing spans (Chromium) for the page's identifiers; user-timing measure spans for the page's measures after the related API result, marks as `emb-user-timing` logs; `emb-server-timing` logs for server-rendered documents (`cdn-cache`, `edge`, `origin`, `db`, `app`); DOM-state images-above-fold fields once per page.

Per part end (every part): surface span ends first, `max-scroll-depth` log, `dom-state` log, LoAF `emb-loaf-report` and `emb-loaf-scripts` (Chromium 123+, at least one long frame: page-load script frames, third-party tag frames, slow handlers; TBD = Σ blockingDuration of non-first frames not tied to UI events; rating mix from `web_performance.tbd_rating_mix`), pending rage-click flush, then the part span with end attributes (counters only when > 0) and the `/v2/spans` delivery `[part span, ...all spans ended since the previous part end]` grouped by scope. Logs batch every 1 s by emit time and flush on hide. With `sdk_upload_spans`, add `POST https://a-<appId>.data.emb-api.com/v2/logs` and `/v2/spans` network spans for uploads that complete while the page is alive (never on unload), with no traceparent.

Interactions: `click()` builds `view.name` from the journey's label using realistic markup per control (primary buttons `<button class="btn btn-primary">`, product links `<a class="product-card__title">`, sometimes the innermost `<span class="btn__label">`), 30-character truncation with `...`, never `data-testid`; `tap.coords` inside the viewport; `first-interaction` once per part (`tap` on touch devices, sometimes `scroll` first on mobile with element type `document` and empty selector); `rage-click` logs when ≥3 clicks hit the same control within 500 ms (the journeys' repeated "Confirm payment" taps after a failed payment already do this); breadcrumbs as `emb-breadcrumb` events.

Network: API calls use `calls.simulate` (backend traces, held delivery) and produce fetch spans (scope `@opentelemetry/instrumentation-fetch` 0.223.0; some XHR spans with `open`/`send` events, scope `@opentelemetry/instrumentation-xml-http-request` 0.223.0); `emb.w3c_traceparent` only when `self.traces` and the API host is in `propagation_allow_list`; 4xx/5xx `error.type`=status string and span status 2; transport failures status 0 with error status (offline/CORS: no backend trace; abort: backend may have run); WebKit sometimes omits `responseStart`. Add a few third-party calls per page (analytics/config endpoints, status 200 or 0).

Web identity: one shared app (super-app mode), `business_unit` and the other Android session properties as `emb.properties.*` (strings), `user.id` set when signed in, and signed-in web customers reuse user IDs from the Android pool with some probability (same customers use both). Releases: `app_version` = web release; pick per session from `target.web.releases` by share and deploy time (default two releases relative to now); tabs opened before a deploy keep the old release.

**B. Wire web into the engine.**
- `runner.Generator`: use `BrowserSession` for web targets (V2), and make V1 `--platform web` use it too with the Vantra estate (the old separate brands go away). Delete or retire `web_session.py` once nothing depends on it (check `tools/check_output.py` and tests).
- `spec.py`: remove the web-target rejection; validate web filters against web targets (browser families/versions, device types, connections, page groups (from `web_sites`), routes, web releases); validate capability conflicts with `web_capabilities.unsupported()` (CLS or TBD targets/faults on Safari/Firefox-only filters, soft-navigation faults on a `multi_page` archetype or non-Chromium filters, INP outside Chromium); validate `web_performance` targets and the Origin policy.
- `catalog.CLIENT_FAULTS`: mark web types supported for `web` as you implement them; `http_error`, `slow_root_span`, `network_failure` become supported on web.
- `planner.py`: web skeletons (`target.platform == "web"`: persona from the web pool with `population.web` overrides, release, browser family/version, device type, connection; signed-in; journey shape) and web eligibility dimensions; map `page_groups`/`routes` filters to journey stages (Product → `get_product`, Checkout → `place_order`/`create_booking`/`shipping_options`, Search → `search`/`flight_search`/`hotel_search`, Cart → `get_cart`, Account → `accounts`/`trips`, Statements → `statement`, Home → always reached); assignment field `web_faults` (list of `{id, type, details}`) consumed by `BrowserSession`.
- Web fault types (each with filters and exact quotas): `js_exception` (family, handling, page, optional rage clicks and error log follow-up), `chunk_load_failures` (only sessions on a release older than the latest; fails on the next lazy route/component load; Vite-style TypeError on www, `ChunkLoadError` on app.vantra.com), `poor_web_vitals` (target p75 per vital), `lcp_regression` (`cause: hero_image` raises resourceLoadDuration and the hero image transfer size; `cause: origin` raises TTFB and Server-Timing `origin`/`db`), `layout_instability` (CLS target with the page's promo banner, ad slot or web-font selectors), `interaction_latency` (INP target; LoAF frames and `loaf_scripts` attributed to a named first-party bundle or third-party tag from `THIRD_PARTY_SCRIPTS`; TBD rises), `slow_server_response` (TTFB from origin latency; in end-to-end mode the page's first data request shows the slow backend too), `soft_navigation_slowness` (long `emb.soft_navigation.duration`, poor soft-nav LCP/INP; Chromium 151+ and SPA/hybrid only), `rage_clicks` (on failing or unresponsive controls after a failed request), `page_abandonment` (visitor leaves before the load event on slow connections: no documentLoad, `emb.page_load=false`, session ends), `network_failure` (status 0 offline/CORS/abort), plus `http_error` and `slow_root_span`. Data-shape controls: `bfcache_share`, `third_party_bloat` (add to the schema under `population.web`).
- `emitted.py`: web observations from OTLP payloads (session from the part span, lob from `emb.properties.business_unit`, release from `app_version`, browser family/version/device type parsed from `user_agent.original`, page group from `PAGE_GROUP_RULES`, vitals list, exceptions by family (type/name/message per engine), network statuses mapped to endpoints, root spans, rage clicks, page_load false, soft navigations). Report p75 per vital and segment (page group × device type × browser family) next to the requested p75 (±10% tolerance) and web fault rates (exact quotas within one session).
- `linter.py`: web checks — Chromium stacks never contain `@` in the message; ratings match values; no CLS/LoAF/INP from Safari/Firefox; no soft-navigation vitals outside Chromium 151+; resources' parents in the same payload; `ux.session_part` in every spans payload; Origin policy satisfied; URL rollup preview (apply the grouper rules to generated URLs and flag templates that would not collapse).
- `tools/check_output.py`: detect engine from `user_agent.original`; build the reference schema from the 2.31 capture **and** `reference/web-sdk-golden/` per engine; enforce engine-specific keys (no `device_memory`/`network_effective_type`/`render_blocking_status` off Chromium; no resource `http.response.status_code` on WebKit), integer-millisecond timestamps and values off Chromium, vitals math (LCP and INP and TTFB sums within rounding), ratings, unique vital IDs, ID formats, payload grouping, surfaces named by template, `emb.w3c_traceparent` only on allow-listed hosts. Also validate against the real production session in `reference/web-sdk-2.29-sample/web_2_29_0_sample.scrubbed.jsonl` (keys, value types and event lists; its values are anonymized).
- Presets (`presets.py`): web presets from `V2-BUILD-PROMPT.md` section 9 (checkout TypeError after a release, chunk load failures after deploy, slow product pages on mobile, promo banner layout shift, search interaction latency from the chat widget, slow origin, online banking route slowness on `/accounts/:accountId`, rage clicks on a failing payment button) and the "Web release breaks checkout" and "Shared payments outage seen in the app and on the website" stories; add a web target to the story specs.
- `spec/examples/`: a web burst example and an Android + web end-to-end example (`platform_mix` across `vantra-android` and `vantra-web`).
- Tests: per-engine payload checks, archetype lifecycle (MPA parts per page, SPA rollover per browser, hybrid mix, bfcache), vitals p75 within ±10% of targets per segment with consistent attribution and ratings, capability gating, exception messages/stacks per engine, issue stability within a release, URL rollup, every web fault type, Origin policy.
- Optional (do it if time allows in this phase, otherwise in 4.8): symbolication — generate a real minified bundle + source map per release, compute bundle IDs the way `packages/web-cli/src/processSourceFiles.ts` does, send `emb.js_file_bundle_ids`, upload maps to `POST /v2/store/sourcemap` (needs the owner's symbol upload token; outbound, ask first) before sending exceptions.
- Acceptance: dry runs pass per-engine checks and the linter; then, **with the owner's approval**, a live 30-minute web run on `kfs9f` meets `V2-BUILD-PROMPT.md` section 14 items 4–7 (verify with the Embrace MCP tools `get_web_vital_distribution`, `get_web_vital_timeseries`, `list_exceptions`).

### 4.2 Backend fault builders still missing

Implement and add to `catalog.BACKEND_VARIANTS` (with `story`, `endpoints`, `evidence`), remove from `PLANNED_BACKEND_TYPES`, add presets, tests and evidence rules:

- `cache_outage` (variant e.g. `catalog-cache-outage`, `accounts-cache-outage`): Redis cluster unreachable (`io.lettuce.core.RedisConnectionException: Unable to connect to ...` for Java, `dial tcp ...: i/o timeout` for Go, `redis.exceptions.ConnectionError` for Python), the service falls back to the database: every request misses, DB queries appear, latency rises, HTTP 200 (story `backend`), with a circuit-breaker log; `details.fallback=false` turns it into 503s (story `mobile`).
- `rate_limited`: api-gateway returns **429** with `Retry-After`, the rate-limit Redis `EVALSHA` returns over-limit, no experience-API child (story `mobile`); the client retries after the delay (journeys already retry transport failures; add a bounded retry for 429).
- `kafka_consumer_lag`: the consumer span (`process <topic>`) starts minutes after the producer (`async_lag` in the tens of seconds to minutes) with `messaging.kafka.consumer.lag` attributes; the user-visible effect is a delayed status (order confirmation email, booking ticketing, ledger posting) and `operation_status` polls returning pending (story `backend`). Remember Chronosphere's 10-minute window: lag must stay well below 10 minutes or the consumer span is dropped.
- `deploy_regression`: errors only on a given `service.version` (a canary pod/version of a named service; `Service` needs a per-instance version), with `rollouts[kind=service_version]` controlling canary share and `rollback_at`; after rollback the errors stop. Use realistic regressions (e.g. `NullPointerException` in a new serializer, `KeyError` in a renamed field).

### 4.3 Backfill: idempotency and resume

- Record every planned session (index, session ID, part IDs, device ID, start) in the run manifest as it is **sent** (receipt), not only generated.
- `generate.py run spec.json --resume <run id>` (and the server's resume action): regenerate the same plan deterministically (same seed, same planned start times and allocations), skip every session whose receipt exists, continue sending the rest. Refuse to resume when the spec version differs. Never re-send a recorded session.
- Throttle (spec `throttle_payloads_per_second`, conservative default), stop on repeated failures, and resume later.
- After a backfill, verify hourly session counts and vitals percentiles against the plan through the Embrace API or MCP (section 4.4 verification), present results with rolling ranges, and show `limits.BACKFILL_NOTES`.

### 4.4 Phase 3: server and runs (`server/`)

Stack: FastAPI, Pydantic v2 (models generated from `spec/scenario.schema.json` with `datamodel-code-generator`; regenerate in CI and fail on drift), SQLAlchemy or SQLModel on SQLite, uvicorn. Put dependencies in `server/requirements.txt` (pinned). The engine stays importable without them.

Tables: `targets` (Embrace apps and the Chronosphere tenant; secrets only as references), `scenario_specs` (id, version, spec JSON, author `user|copilot|mcp:<client>`, created, parent version), `runs` (id, spec id+version, mode, destination, status queued/running/paused/stopped/completed/failed, started, ended, trace-eligible-from time, manifest path), `run_events` (timestamped events for SSE), `receipts` (per delivery: target, kind, HTTP status, accepted/rejected, OTLP partial-success counts, trace IDs), `verifications` (per run: Chronosphere retention/completeness per lob and platform, Embrace counts/vitals/exception groups, timestamps), `dashboard_setup` (per target and setup item: state unknown/ok/missing/needs_action, evidence, checked_at), `api_tokens` (MCP tokens: hashed, name, created, last used, revoked), `streams` (always-on stream objects: spec, target, health, daily summaries).

Job runner: each live stream is a managed asyncio task (or subprocess) that re-reads the **latest spec version at every session start**, so edits apply without restarts; bursts/backfills/previews are bounded jobs; pause/resume/stop (SIGINT-equivalent flush of pending deliveries on stop); guardrails from `limits.py` (max sessions per job, max rate, max concurrent streams per target). Progress over Server-Sent Events (`GET /api/runs/{id}/events`). The run's `Sender` records receipts.

REST API (JSON; same functions back the MCP tools): `GET /api/capabilities`; targets `GET/POST /api/targets`, `POST /api/targets/{id}/test` (Chronosphere: token check with an empty OTLP export; Embrace: app lookup; sends nothing visible); `GET /api/targets/{id}/readiness`, `GET /api/targets/{id}/setup-kit?format=md|json`; presets `GET /api/presets`, `GET /api/presets/{id}`; specs `GET/POST /api/specs`, `GET /api/specs/{id}`, `PATCH /api/specs/{id}` (JSON Patch → new version), `POST /api/specs/validate`, `POST /api/specs/{id}/explain`, `POST /api/specs/{id}/preview` (dry run returning expected counts per fault, expected vitals percentiles per segment, one sample Android timeline, one sample web timeline, one trace waterfall, linter results, URL rollup preview, applicable limits); runs `POST /api/runs` (mode live|burst|backfill, destination, `confirm: true` required), `POST /api/runs/{id}/stop|pause|resume`, `GET /api/runs`, `GET /api/runs/{id}` (status, trace-eligible-from time, live counters, requested vs emitted), `POST /api/runs/{id}/verify`; incidents `POST /api/runs/{id}/incidents` (schedule), `DELETE` (cancel); streams CRUD + `POST /api/streams/{id}/pause|resume`, `GET /api/streams/{id}/summary?day=`; settings: secrets `PUT /api/settings/secrets/{name}` (write-only; never returned), MCP tokens create/revoke/list (last used), defaults.

Secrets: store in the macOS Keychain using the Security framework or the `keyring` package (never a command line); fall back to environment variables or mounted files in the container. Rows hold `token_ref` values only.

Verification (`verify_run`): Chronosphere Data API by every known trace ID from the manifest (`POST https://<tenant>.chronosphere.io/api/v1/data/traces`, header `API-Token`, `query_type: TRACE_IDS`, ≤10 traces per call, page through all; count complete traces with the app span as root per lob/platform; record `behavior_dataset_slug` and `tail_sampling_rate`); Embrace via the Embrace public API with an Embrace API token, or by documenting the Embrace MCP tools for the operator (`get_session_distribution`, `list_network_domains`, `list_root_spans`, `get_web_vital_distribution`, `list_exceptions`) when no token is configured.

Readiness and dashboard setup kit (`server/readiness.py`), per target, as a checklist with copy buttons, Markdown/JSON export, and automatic verification where an API allows: NSF (app included with the Chronosphere destination; every API domain on the forwarding list; trace-link feature enabled; a short **forwarding probe**: send a few end-to-end sessions, wait ~5 minutes, confirm the app span is the trace root in Chronosphere and the request shows "Forwarded" in Embrace — on 2026-10-30 16:37–17:37 UTC no forwarded spans arrived although the generator was healthy, so never skip this); Chronosphere dataset + Allow behavior covering the app services (`forwarded_service_name(...)` for each target) and experience APIs, retention measured on known trace IDs, and — only if the token has configuration permission and the owner confirms the exact diff — create or extend the dataset; Embrace User Flows from `milestones.definitions()` / `docs/user-flows.json`; saved session filters from `docs/session-filters.json`; web allowed origins matching the Origin policy; Page Group rules from `web_sites.PAGE_GROUP_RULES`; source maps when symbolication is on; Embrace verification access.

Hosting locally: `uvicorn server.app:app` serving the built UI from `apps/web/dist`. Tests: API tests with FastAPI's TestClient; end-to-end run tests against local stand-ins for Embrace ingest and an OTLP receiver (loopback HTTP servers; note the macOS reverse-DNS hang in `http.server`, override `server_bind`).

Acceptance: live and burst runs can be started, observed (SSE), paused, edited (new spec version applies at the next session) and stopped over the API; receipts and verification stored.

### 4.5 Phase 4: the web UI (`apps/web`)

Stack: React 18, TypeScript, Vite, Tailwind CSS, shadcn/ui (Radix), TanStack Query, React Hook Form + Zod (schemas generated from `spec/scenario.schema.json` with `json-schema-to-zod`), Framer Motion (150–250 ms transitions), Recharts or visx, cmdk (Cmd+K palette for presets, runs, navigation), one icon set (Phosphor or Radix Icons; not Lucide).

Brand (Cortex XCOR): dark-first; background `#000000`, surfaces `#0B0B0C` and `#141416`; primary accent Cortex green `#00CC66` with the `#0F9347 → #00CC66` gradient for primary actions; red `#FF0000` only for failures and destructive states; Figtree for all text with tabular numerals; generous spacing; 1px borders; soft glow on focus; WCAG AA contrast; full keyboard navigation; responsive to laptop width; official logo assets only if the owner provides them.

Screens:
- **Connections:** Embrace targets (Android/web app ID, region; web: origins, Origin policy, estate, router integration, SDK self-upload setting), Chronosphere tenant + token (write-only), "Test connection", readiness checklist and setup kit.
- **Scenario studio:** platform switcher (Android, Web, Both); preset gallery (cards grouped Android, Web, Backend, Stories, each with a one-line outcome); selected faults as editable chips (rate as "1 in N sessions" or %, exact-quota toggle, filters, details); population and traffic editors including the browser mix and per-lob archetype selector (Multi-page, Single-page, Hybrid); **web performance editor** (grid page group × device type × browser family, target p75 per vital colored by rating thresholds, inline capability warnings such as "Safari does not report CLS", live distribution preview); exception editor (family picker, handling, per-engine message preview); incident timeline over the chosen window (drag onset, peak, recovery). Every edit validates through the API and shows issues inline.
- **Preview:** expected counts by fault, platform and lob; a sample Android session rendered like the Embrace user timeline; a sample web timeline (page loads, soft navigations, vitals, clicks, exceptions, network); vitals histograms with threshold bands; a sample exception with its stack; a trace waterfall like Chronosphere's; URL rollup preview; linter results; applicable limits.
- **Copilot panel** (right side): chat, suggested prompts, proposed spec diff with Apply/Discard; disabled state until an Anthropic key exists.
- **Runs and streams:** destination badge; run timeline with a muted "Sessions only" segment for history and a green "Sessions and traces" segment from run start; the verbatim `limits.TRACE_CAVEAT` next to the mode control and in the run summary; "Trace history available from HH:MM" and a growing complete-trace count; live counters per platform (sessions, payloads, accepted/rejected spans, errors); requested vs emitted (rates per fault, p75 per vital and segment, exception counts per family); verification results with links into Embrace and Chronosphere; stop, pause, resume; streams list with health and daily summaries.
- **Settings:** MCP access tokens (create, revoke, last used), Anthropic API key, model choice, symbol upload token, defaults.

Tests: Playwright smoke tests for every screen; a visual review against the brand rules.
Acceptance: a sales engineer can go from empty to a verified stream without the CLI.

### 4.6 Phase 5: MCP server and copilot

MCP (`server/mcp.py`, official MCP Python SDK): streamable HTTP mounted on the server with bearer-token auth (per-user tokens from Settings, hashed at rest, last-used tracking) and a stdio entry point for local clients (`python -m server.mcp`). Tools, JSON-schema typed, idempotent where possible, calling the same functions as the REST API: `get_capabilities` (platforms, fault types, limits, capability matrix, backfill rules, the trace caveat); `list_targets`, `create_target` (secrets accepted on create, never returned), `test_target`, `check_readiness`, `get_dashboard_setup`; `list_presets`, `get_preset`; `get_spec`, `create_spec`, `patch_spec` (JSON Patch), `validate_spec`, `explain_spec`; `preview_spec`; `start_run` (mode, destination), `stop_run`, `pause_run`, `list_runs`, `get_run_status` (includes trace-eligible start time); `schedule_incident`, `cancel_incident`; `verify_run`. Resources: the spec JSON Schema, the fault catalog with descriptions, the capability matrix, the preset library. Prompts: `prepare-demo` (asks audience, story, time until the demo) and `design-incident`. Outbound or destructive actions (`start_run`, large backfills, Chronosphere configuration changes, source-map uploads) require `confirm: true` and are logged with the caller. Copilot and MCP inputs go through the same validation and size limits; text in user fields is data, never instructions.

Copilot (`server/copilot.py`): server-side tool loop with the Anthropic API (official `anthropic` Python SDK; model from settings, default `claude-sonnet-5-5`, option `claude-opus-5-5`; key from the Keychain). Give the model the MCP tool functions (read catalog, capability matrix, current spec, schema, validate, preview). The model never emits raw payloads: it returns a JSON Patch against the spec plus a plain-language summary; the server validates the patch (schema, limits, capabilities, linter) and the UI shows the diff (e.g. "Adds: jvm_crash, 33.3% of sessions where business_unit=retail and app_version=10.28.0"); Apply creates a new spec version (author `copilot`) that running streams pick up at the next session start. Keep copilot turns and versions. When a request conflicts with a capability or limit (CLS for Safari, traces for yesterday), explain why and propose the closest valid alternative; never promise trace history. Use prompt caching for the static system context.

Acceptance: every example request in `V2-BUILD-PROMPT.md` section 8.1 produces a correct validated patch (write these as tests with recorded model responses; live model calls only when a key is configured); Claude Code connected over MCP can preview and start a run with confirmation.

### 4.7 Phase 6: destinations, demo preparation, backfill end to end

Implement `V2-BUILD-PROMPT.md` section 10 fully: destination segmented control (default: the target's last choice); the automatic split at "now − 8 minutes" for end-to-end runs with backfill; switching modes on a running stream applies at the next session start (no retroactive traceparents); "Prepare ahead" (starts an end-to-end live run with a planned duration, default 2 hours, and an optional incident near the end or during the demo); **demo preparation** as one action with a live checklist: readiness checks for the chosen mode and targets → up to 24 h Embrace backfill with the chosen story → in end-to-end mode a burst covering the last 8 minutes with stitched traces → start or confirm a live stream → optionally schedule the story's incident to begin a few minutes into the demo; recommend "Prepare ahead" when the demo is more than two hours away; consistent messaging in `get_capabilities` and the copilot.

Acceptance: a sessions-only 24-hour backfill shows the planned hourly session counts and vitals in Embrace for "Last 24 hours" and emits zero traceparents; an end-to-end run with backfill shows no "View trace" links on sessions older than 8 minutes and complete traces for every eligible request from the run start; the caveat appears on the run screen, in the run summary, in `get_capabilities` and in copilot answers about past traces.

### 4.8 Phase 7: hosted always-on stream

`Dockerfile` (non-root user, slim Python base, UI built in a separate stage, secrets from environment variables or mounted files, no secrets in the image), `docker-compose.yml` for local testing, health endpoint, a first-class Stream object (health, pause/resume, daily summary of what it emitted), structured logs without secrets. Document how to run it on a small VM and how multiple runner regions diversify Embrace locations (location comes from the sender IP). Do not migrate the live LaunchAgent without the owner's approval; document the cut-over steps.

### 4.9 Later items from the build prompt (do them after 4.1–4.8)

iOS sessions (read `~/Claudecode/embrace-apple-sdk` for envelope and span formats; one app ID per platform; the owner must create the iOS app), backend RED metrics to Chronosphere (raw points ≤2 h late; aggregated a few minutes; ≤1 min future), multi-region runners, symbolication if not done in phase 2.

---

## 5. Live acceptance (from `V2-BUILD-PROMPT.md` section 14), owner-approved

After a 30-minute run on `demo-sandbox` with Android `5nrc2` and web `kfs9f`: emitted rates within ±2 pp of requested; ≥95% of eligible traces complete with the app span as root for both forwarded app services; Embrace shows sessions, crashes, network domains and root spans; `get_web_vital_distribution` grouped by browser, device type and surface label matches the plan within ±10% on p75 and `get_web_vital_timeseries` shows the scheduled regression; `list_exceptions` shows the planned families grouped into stable issues; Page Load Insights shows page-load p95 and resources for Chrome desktop; Pages health shows views, LCP and failures per template; network requests on forwarded domains show "View trace in Cortex XCOR" and the link opens complete traces. Prerequisites the owner must do in Embrace: include `kfs9f` in network span forwarding with the three API domains; configure allowed origins if enforcement is on; optionally rename the app to "Vantra". In Chronosphere: add the web forwarded service to `vantra-end-to-end`.

---

## 6. Commands you will use

```bash
cd ~/Session-Generator-V2
python3 -m unittest discover -s tests -v
# V1 checks (must keep passing)
python3 generate.py --profile all --sessions 120 --quiet --dry-run out/a && python3 tools/check_output.py out/a
python3 generate.py --profile all --sessions 120 --quiet --platform web --dry-run out/w && python3 tools/check_output.py out/w
python3 generate.py --profile all --sessions 120 --quiet --incident payments-db-pool --dry-run out/i && python3 tools/check_output.py out/i
# V2 spec checks
for s in spec/examples/*.json; do python3 generate.py validate "$s"; done
python3 generate.py run spec/examples/baseline-burst.json --dry-run out/spec --quiet --seed 7331
python3 tools/check_output.py out/spec && python3 generate.py report out/spec spec/examples/baseline-burst.json && python3 generate.py lint out/spec
python3 generate.py run spec/examples/verification-plan.json --dry-run out/verify --quiet && python3 tools/check_output.py out/verify
# Security (bandit is not installed globally; use a venv)
python3 -m venv /tmp/sg-venv && /tmp/sg-venv/bin/pip install -q bandit && /tmp/sg-venv/bin/bandit --ini .bandit -r xcorgen generate.py tools server
# Push
git -c credential.helper= -c "credential.helper=!gh auth git-credential" push
```

Add the new phases' checks (server tests, UI build and Playwright, MCP tool tests, web dry runs per engine) to `.github/workflows/security.yml` as you build them.

---

## 7. Pitfalls already hit (avoid repeating them)

- Exact-quota allocation must count the denominator from **all** eligible sessions and place outcomes only where they can occur; filtering the denominator by exclusive-group availability under-allocates.
- Batch runs must shift sessions individually (only those still running at send time); one global shift pushes sessions out of the 8-minute trace window and breaks planned incident phases.
- An `ends_journey` failure (e.g. inventory 409 at add-to-bag) prevents later stages; the planner treats it like a fatal stage.
- Latency evidence overlaps natural latency; use backend span evidence when traces exist, app-side breadcrumbs/logs for fallbacks, and label client-latency-only rows approximate.
- The PII linter must skip identifier fields: all-digit hex span IDs can pass a Luhn check.
- Tokenize camelCase only on lower→upper boundaries; IDs like `78S7QA` must not split into `QA`.
- Bandit B105 fires on `"api_token": None`; mark with `# nosec B105` and a reason, as V1 does.
- Python's `http.server` hangs on macOS doing reverse DNS in `server_bind`; override it in local test servers.
- zsh does not word-split `$var`; use arrays.
- The Chronosphere query API returns at most ~10 traces per call; page.
- `reference/` files must not contain secrets or personal data; real samples go through `tools/scrub_web_sample.py` (a test keeps the committed sample clean). The gitleaks allowlist is narrow on purpose: add exact file paths, never directories.

---

## 8. Definition of done

All of `V2-BUILD-PROMPT.md` sections 1–15 is implemented as described here; web targets validate and generate SDK-faithful sessions for all three archetypes on Chromium, WebKit and Gecko; all four new backend fault types work; backfills are idempotent and resumable; the server, UI, MCP server, copilot (key-gated), readiness checks and dashboard setup kit work locally; the container runs the always-on stream; every test and CI check passes; `AGENTS.md`, `README.md` and `SECURITY.md` describe the final system; a pull request from `v2` to `main` describes the changes; and the owner has the exact list of Embrace and Chronosphere configuration steps and live acceptance commands still requiring their action.
