# Handoff notes for AI agents

You are picking up **Session-Generator**: a stdlib-only Python tool that generates realistic mobile (Android) and web sessions for **Cortex XCOR RUM** (formerly Embrace) and matching full distributed backend traces for **Cortex XCOR** (formerly Chronosphere), stitched together so a session's network request opens a complete trace. It feeds a live sales/demo tenant. Read this file fully before changing anything; `README.md` is the user-facing guide.

## Current UI direction (owner feedback, 2026-10-30)

The owner replaced the multi-page studio requirements with a four-step workflow: destination → customization → payload preview → send. The latest approved UI direction uses a compact Cortex navy/slate shell, restrained mint accents, and the approved Cortex XCOR SVG logo. The owner wants the typography from their Good Company project; its source location is still needed. Source Sans 3 is the provisional font in the redesign working tree, not a verified Good Company match. All customization is through UI controls; remove Copilot/MCP from the UI and keep experimental HTTP integrations disabled by default. Continuous sending needs a rate, a finite duration (including one/two-hour presets), actual time-based schedule progress, refresh recovery, and pause/resume/stop. The latest owner feedback requests lighter glass-inspired navigation/controls, less text, and visible traffic animation. Animate only while the worker is running with fresh observations; stop animation on pause, completion, or uncertain connectivity. Show delivery receipts directly and distinguish API acknowledgements from confirmed dashboard ingestion. These direct owner instructions supersede the old handoff's UI/integration requirements.

`tools/studio.py` and `Start Session Generator.command` run the local API in a detached owned process. Do not stop the owner's working application server at the end of a coding session. Temporary visual-test servers must still be cleaned up. The old fetch failures were caused by ending the temporary server while its cached page stayed open. Keep truthful offline/reconnect states and idempotent run startup.

## V2 (branch `v2`)

V2 turns the generator into a product driven by one versioned document, the **ScenarioSpec** (`spec/scenario.schema.json`). The build prompt and the verified feasibility findings are in `docs/v2/V2-BUILD-PROMPT.md` and `docs/v2/V2-SCOPE.md`; read both before V2 work. The prompt's phases: (1) spec and engine, (2) web generator, (3) server and runs, (4) UI, (5) MCP and copilot, (6) destinations, demo preparation and backfill, (7) hosted stream.

**Continuing V2:** start with `docs/v2/CODEX-HANDOFF.md` (zero-context handoff: architecture, state, remaining work in order) and `docs/v2/web-sdk-behavior.md` (verified Web SDK 2.31 behavior).

**Phase 1 status (done):** spec, stdlib schema validator, semantic validation, platform limits, exact quotas, realism linter, presets with a computed Baseline, `generate.py run spec.json`, requested-versus-emitted report, new Android faults, and the burst trace-window fix. Live, burst and backfill modes run from specs; V1 flags still work unchanged for the LaunchAgent.

**Owner decisions (2026-10-30):** local Mac first (uvicorn, single user, macOS Keychain secrets; container later); no Anthropic key yet, so build the MCP server before the in-app copilot; web estate accepted (www.vantra.com: retail multi-page and travel hybrid, with the booking flow client-routed under /travel/book on the same origin because storage is per origin; app.vantra.com: banking single-page app; static.vantra.com: CDN); web app `kfs9f` "Vantra Web" (web, production, US). Embrace names forwarded client spans `lower(app name with spaces as underscores)-<platform>-<environment>` (`go/lib/common/data_destinations_config.go:114`), so this app forwards as **`vantra_web-web-production`**, not `vantra-web-production`, unless it is renamed "Vantra". Derive service names with `limits.forwarded_service_name`, never by hand.

### How a spec runs

```
spec.json --validate()--> Planner --skeleton per session--> allocation (exact quota / probability) --> assignment
          (schema, limits,   (target, line of business, visitor, app version, ring, start time,      (V1 Plan fields: story,
           linter)            trace eligibility, journey shape)                                       incident, client fault ...)
                                                                    --> V1 journeys and builders --> Emitted report (payloads only)
```

| File | Role |
| --- | --- |
| `spec/scenario.schema.json` | The ScenarioSpec. `xcorgen/schema.py` validates it with the stdlib (the engine stays dependency-free); the server will generate Pydantic and Zod types from the same file |
| `xcorgen/spec.py` | Defaults (`ANDROID_TARGET`, `WEB_TARGET`), `validate()` (actionable issues; severity `error` blocks a run), JSON Patch, `explain()`, incident windows and ramps |
| `xcorgen/limits.py` | Verified platform limits and the verbatim trace caveat; `trace_eligible()` (8-minute rule), `forwarded_service_name()` |
| `xcorgen/catalog.py` | Client fault types and backend variants (V1 incidents) with story, endpoints and emitted evidence |
| `xcorgen/quota.py` | Exact allocation (count from every eligible session, placed only where available) and streaming quotas |
| `xcorgen/planner.py` | Skeletons, journey shapes and steering, eligibility versus availability, V1 `Plan` assignments |
| `xcorgen/spec_runner.py` | `SpecRun`: live streams, bursts, backfills, per-session time shift, trace stripping, run manifest, throttling |
| `xcorgen/emitted.py` | Requested versus emitted from payloads only (never the plan) |
| `xcorgen/linter.py` | Realism linter for specs and payloads (forbidden words, service names, stack language, status, durations, device/OS, PII) |
| `xcorgen/presets.py` | Preset library; `baseline()` re-expresses V1's default policy as fault rates |
| `xcorgen/generic_faults.py` | Backend traces for spec-defined `http_error` faults, with language-shaped exceptions |
| `xcorgen/secrets.py` | `token_ref` resolution (`keychain:`, `file:`, `env:`); specs never hold secret values |

Rules that keep the numbers honest:
- **Rates are shares of eligible sessions.** Session-level filters (line of business, app version, OS, device model and tier, signed-in, release ring) define eligibility. `steer_journey` (default true) makes affected sessions reach the fault's stage; with it false, only sessions that reach the stage naturally are eligible (`filters.endpoints`).
- **Exact quotas** (default for bursts and backfills) fix the count from all eligible sessions and place it only where the fault can happen: free in its exclusive group, not after a fatal crash or a journey-ending failure (`ends_journey`), and compatible with the journey shape. This is why emitted shares match within one session.
- **One backend story per session** (all backend faults share one exclusive group); fatal faults share one group. `details.with_fault` couples a fault to another's sessions (for example a crash after a failed payment).
- **Payments pool phase:** in spec mode the phase is fixed per session from its planned start (exact counts); V1 mode still evaluates it per request. Onset sessions get the slow lock-query story without failing.
- **Time model:** a session dated more than 8 minutes before it is sent carries no `emb.w3c_traceparent`/`emb.forward_telemetry` and no backend trace (`strip_traces`). Batch sessions keep their planned time; only sessions still running at send time move back. The V1 `--sessions` burst now applies the same rule and defaults to `--spread 8`.
- **Evidence:** the report counts crash logs, blockage spans, handled exceptions by type, transport failures, startup and root-span durations, HTTP statuses, app-side fallback breadcrumbs and logs, and backend spans when the session has traces. Latency-only evidence without traces is labeled approximate (±2 pp).

### SDK behavior verified for V2 (and V1 discrepancies fixed)

- `emb.w3c_traceparent` and `emb.forward_telemetry="true"` are set together, only for domains with network span forwarding (`NetworkRequestDataSourceImpl.kt:123-127`). Sessions-only runs omit both. V1 set `emb.forward_telemetry=false` on transport failures; the SDK never does, so V2 keeps `"true"`.
- Transport failures: `error.type` is the exception's canonical class name and `exception.message` its message, with no status code (`OkHttpDataSource.kt:156`). Timeouts and resets happen after the request reached the gateway, so the backend trace is still built; DNS, offline, TLS and cancelled requests produce none.
- Handled exceptions use `throwable.javaClass.simpleName` for `exception.type` (`LogsApiDelegate.kt:104-121`). In spec mode the handled error a screen logs after a failed request is the failure itself (`retrofit2.HttpException: HTTP 502 ` or the transport exception), not V1's `NumberFormatException`, so it is distinguishable from a requested handled-exception fault.

### V2 workflow

```bash
python3 generate.py validate spec/examples/baseline-burst.json
python3 generate.py run spec/examples/baseline-burst.json --dry-run out/spec --seed 7331 --quiet
python3 tools/check_output.py out/spec && python3 generate.py report out/spec spec/examples/baseline-burst.json && python3 generate.py lint out/spec
python3 generate.py run spec/examples/verification-plan.json --dry-run out/verify --quiet && python3 tools/check_output.py out/verify
python3 -m unittest discover -s tests -v
```

`spec/examples/live-end-to-end.json` is the V2 equivalent of the LaunchAgent (Baseline, 10 sessions per minute, end to end). Do not point the LaunchAgent at this branch until the owner approves; it runs from `~/xcor-demo-generator`. Non-dry runs write an owner-only manifest to `.runs/<run id>/manifest.jsonl` and lint a preflight sample first; any realism error blocks sending.

**Implementation status:** handoff phases 4.1–4.9 are implemented on `v2`. The remaining gates are owner configuration, live forwarding/dashboard acceptance, a real Anthropic key and any deployment/cut-over approval. Read `docs/v2/OWNER-ACCEPTANCE.md`, `LATER-FEATURES.md` and `HOSTING.md`. Older deployment/backlog notes below describe V1 history; the phase-specific notes at the end supersede them.

## What the owner cares about

1. **It must look like real production data.** Viewers must not be able to tell it is synthetic. Never put "demo", "fake", "test", "sample" or similar into session properties, attribute values, service names, IDs, URLs or log text. Use the formats real SDKs and backends produce.
2. **Enterprise realism.** Service names follow enterprise conventions (`<line-of-business>-experience-api`, `*-service`, `*-orchestrator`, `*-gateway`, `*-worker`). The owner rejected names like `travel-bff`. Errors must be believable root causes a real team would debug (connection pool exhaustion, deadlocks, timeouts), with exception types and stack traces in the right language.
3. **Professional tone** in anything user-facing: no analogies or folksy wording.
4. **Correctness over guesses.** Payload formats here were taken from the SDK source code and real captures, not invented. Keep it that way: when you add a signal, find how the real SDK emits it first.

## How the stitching works

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

- The generator gives every API call a traceparent, records it on the client network span (`emb.w3c_traceparent`), and builds the backend trace with the same trace ID and the client span as the parent of the backend root.
- Embrace NSF forwards the client span to Chronosphere, where it becomes the trace root. The generator does **not** send the client span itself (`--emit-client-spans` exists only for apps without NSF; with NSF it would duplicate the root).
- The Embrace dashboard shows **View trace in Cortex XCOR** only when the NSF outcome for that request is `success` (dashjs `NetworkRequestExpand.tsx`). NSF requires the app to be included in forwarding and the request's domain to be on the forwarded-domain list. Forwarding is decided once, at ingest; old sessions are never re-forwarded.
- **Delivery timing:** Embrace forwards client spans after a session part ends and is processed. Backend traces are held until that part ends and sent 3–15 seconds later (`calls.release_backend`, `FORWARD_DELAY_MS`); burst mode sends backend traces first. Earlier arrival-order conclusions were confounded by active sampling, and must not be treated as universal Chronosphere guarantees. Measure known trace IDs and their sampling policy before adjusting timing.
- **Sampling fix verified 2026-10-29:** Vantra app-rooted traces inherited the `unassigned` dataset's baseline behavior (`tail_sampling_rate=0.5`). A controlled run accepted all 15 exports but retained only 3/8 shop and 1/3 travel traces; money retained 4/4 backend traces without app spans. At 16:06 UTC, `demo-sandbox` was configured with dataset `vantra-end-to-end`, service `IN` [`vantra-android-production`, `shop-experience-api`, `banking-experience-api`, `travel-experience-api`], assigned `__chronosphere_managed_allow` at first priority. Existing assignments and their relative order were preserved. New traces must show `behavior_dataset_slug=vantra-end-to-end` and `tail_sampling_rate=1`. This policy does not fix missing Embrace domain forwarding or recover old sampled-out traces. Query every expected trace ID: querying only retained traces cannot measure loss.

## Verified facts (with where they came from)

The owner has local clones in `~/Claudecode/` (Embrace SDKs, Go backend `go/`, dashboard `dashjs/`). Facts below were verified there on 2026-10-29.

- **Android ingest**: gzipped JSON envelope `{"resource", "metadata", "version": "0.1.0", "type": "spans"|"logs", "data"}` to `https://a-<appId>.data.emb-api.com/v2/spans|logs`, headers `Accept`, `User-Agent: Embrace/a/<sdk>`, `Content-Type: application/json`, `Content-Encoding: gzip`, `X-EM-AID`, `X-EM-DID` (32 uppercase hex, stable per device), `X-EM-PAYLOAD-TYPES`. All attribute values are strings. Span/log shapes, names and keys come from `embrace-android-sdk` source and its integration-test golden files (copies in `reference/android-sdk-golden/`). Key points: network span name `emb-<METHOD> <path>` (query stripped), status `Unset` for <400 and `Error` + `emb.error_code=failure` for >=400 (never `Ok`); taps are `emb-ui-tap` events (Compose: label text; Views: `pkg:id/name`); Fragments are not auto-captured (we use `Embrace.startView` style spans with `emb.manual_instrumentation`); crashes are `sys.android.crash` logs with JSON-array stack traces.
- **Web ingest**: gzipped OTLP/JSON from `@embrace-io/web-sdk` 2.31.0, captured from a real browser into `reference/web-sdk-2.31.0-capture/`.
- **Backend parsing**: `go/lib/codec` maps `emb.type=perf.network_request` spans to network requests and reads `emb.w3c_traceparent`; NSF processor is `go/loader/session/nsf/internal/processor.go`; forwarded span service name is `<app name>-<platform>-<environment>` lowercased (`go/lib/common/data_destinations_config.go`). Parts of a user session are grouped by `emb.user_session_id` + device ID, within ±3 days. Timestamps are clamped, not dropped. Location comes from the sender's IP (MaxMind), so all sessions show the machine's location.
- **Chronosphere OTLP**: `https://<tenant>.chronosphere.io/data/opentelemetry/v1/traces`, protobuf, gzip, header `API-Token`. The sender checks OTLP partial-success rejections. `tools/check_protobuf.py` proves the encoder matches the official protos.
- **Chronosphere Data API** (for verification): `POST /api/v1/data/traces`, header `API-Token`. `TRACE_IDS` queries must not include a time range; `SERVICE_OPERATION` queries need one of at most 15 minutes. See `tools/chronosphere_traces.py`.

## Current deployment state (2026-10-29)

- **Embrace app**: one Android app, `5nrc2` ("Vantra", production, US), used for all three profiles. Because several profiles share one app ID, the generator runs in **shared super-app mode**: one app `com.vantra.android` with three lines of business told apart by the session property `business_unit` = `retail` | `banking` | `travel`, plus `market`, `membership_tier`, `entry_point` and business-specific properties. One user base and account system across the app.
- **Forwarded domains in Embrace NSF**: `api.vantrashop.com`, `api.vantramoney.com`, `api.vantratravel.com`. The owner added the missing money domain at approximately 2026-10-29 16:11 UTC. The NSF configuration manager defaults to a five-minute refresh (`NETWORK_SPAN_DESTINATIONS_REFRESH_PERIOD`). Sessions processed before the refreshed domain configuration can still lack app spans. At approximately 16:19 UTC, all 5/5 known Money traces generated after that refresh window were complete, with app roots joined to their backend children. The post-retention-fix Shop cohort was 5/5 complete and Travel 1/1 complete. Every one of these traces carried `behavior_dataset_slug=vantra-end-to-end` and `tail_sampling_rate=1`.
- **Chronosphere tenant**: `demo-sandbox`. Credentials belong outside the checkout, referenced by `chronosphere.api_token_file` or `CHRONOSPHERE_API_TOKEN_FILE`; `CHRONOSPHERE_API_TOKEN` can override this. Token files and any config containing inline secrets must be owner-only. **Never commit credentials or print them.** `config.example.json` shows the shape.
- **Current presentation mix:** the owner requested quieter timelines and more errors on 2026-10-29. Runtime targets are now 30% captured main-thread freezes (6–12 seconds) and 10% fatal crashes, with independent backend scenarios. Keep only sparse business/flow milestones; business work has named root spans. Do not restore the earlier dense-breadcrumb or ~1.4% crash/~1.6% blockage targets. `docs/calibration.json` measures emitted evidence; `docs/session-tuning-verification.json` records fresh deployment checks. The owner will create User Flows; preserve their anchors without creating definitions.
- **Always-on service**: macOS LaunchAgent `local.xcor-demo-generator` (copy in `deploy/`), running `generate.py --profile all --rate 10 --quiet`. Logs in `logs/generator.log` (status line every 10 min). Preserve the pending deliveries when restarting: SIGINT flushes them; a forced kill loses them. Launchd KeepAlive may immediately restart an old invocation, so verify the resulting PID and arguments after reloading. It stops while the Mac sleeps.
- `.state/` holds the returning-visitor pool (device IDs, user IDs, session counters). Deleting it makes every visitor new; do not delete it casually.

## Code map

| File | Role |
| --- | --- |
| `generate.py` | CLI: burst (`--sessions`) or live (`--rate`) mode, `--platform`, `--incident`, `--dry-run`, `--diurnal` |
| `xcorgen/runner.py` | Config loading, senders (Embrace + OTLP), shared-app detection, burst/live scheduling, status logging |
| `xcorgen/android_session.py` | Android session simulator: envelopes, parts, startup, screens, taps, network spans, logs, crashes |
| `xcorgen/web_session.py` | Web session simulator (Web SDK OTLP/JSON shapes) |
| `xcorgen/calls.py` | One API call: outcome choice, network timing, backend trace build, held backend delivery |
| `xcorgen/backend.py` | Backend call-tree engine (`Node`, layout, OTLP span emission, hop builders, stack traces) |
| `xcorgen/profiles/platform.py` | Shared platform services and helpers: `gateway`, `profile_lookup`, `risk_score`, `authorize_payment`, `notify` |
| `xcorgen/profiles/{ecommerce,banking,travel}.py` | Lines of business: services, endpoints (call graphs + outcome weights), pages/screens, journeys, incidents, Android app data |
| `xcorgen/personas.py` | Web and Android device catalogs, returning-visitor pool |
| `xcorgen/otlp.py` | OTLP JSON helpers, protobuf encoder, partial-success decoder |
| `tools/check_output.py` | Validates a `--dry-run` directory against the SDK formats and trace integrity |
| `tools/check_protobuf.py`, `tools/chronosphere_traces.py` | Encoder check; live verification in Chronosphere |

Journeys are plain functions that drive a session object (`page_view`, `click`, `api`, `parallel`, `flow`, `log`, `exception`, `login`, `logout`, `leave`, `wait`). The same journeys run on web and Android; the session class translates them (for example an unhandled `exception` becomes a Kotlin crash on Android). Endpoint builders return `(root Node, status, data)` and pick behavior from `ctx.outcome` (weighted per endpoint, overridden by `--incident`).

## Workflow for any change

1. Make the change.
2. Dry run and validate every platform and at least one incident:
   ```bash
   python3 generate.py --profile all --sessions 120 --quiet --dry-run out/a && python3 tools/check_output.py out/a
   python3 generate.py --profile all --sessions 120 --quiet --platform web --dry-run out/w && python3 tools/check_output.py out/w
   python3 generate.py --profile all --sessions 120 --quiet --incident payments-db-pool --dry-run out/i && python3 tools/check_output.py out/i
   ```
   Dry runs do not touch `.state/`.
3. If you touched `otlp.py`, run `tools/check_protobuf.py` in a venv with `opentelemetry-proto`.
   Run `python3 -m unittest discover -s tests -v` for the security regression suite. Secret scans must use redacted output and the narrow fixture exception in `.gitleaks.toml`. Never broaden that exception to entire directories. Bandit intentionally excludes B311 for the simulator's seeded non-cryptographic randomness; it must never be used for real credentials.
4. Restart the LaunchAgent, wait ~8 minutes, then confirm in Chronosphere:
   ```bash
   python3 tools/chronosphere_traces.py completeness <restart time, RFC3339 UTC>
   ```
   and in Embrace (the Embrace MCP server, app `5nrc2`: sessions, network domains, root spans).
5. Commit as the owner (see below).

## Git conventions

Commit and push as **Aissa-emb** (`git config user.name "Aissa-emb"`, `git config user.email "200142933+Aissa-emb@users.noreply.github.com"`). No `Co-Authored-By`, `Assisted-by` or "Generated with" trailers. Push with GitHub CLI credentials if the keychain holds another account: `git -c credential.helper= -c "credential.helper=!gh auth git-credential" push`.

## Backlog (suggested order)

1. **Maintain the forwarding and retention prerequisites.** All three domains and the scoped `vantra-end-to-end` Allow behavior were verified on 2026-10-29. When moving tenants or changing settings, verify every known generated ID and its parent chain; the completeness tool's retained-trace samples cannot measure how many traces were discarded.
2. **Tune the hold delay.** `FORWARD_DELAY_MS` (3–15 s after part end) is based on the observations above. Measure completeness over a few hours with `tools/chronosphere_traces.py` and adjust if some app spans still go missing.
3. **Run somewhere always on.** The LaunchAgent pauses when the Mac sleeps. Package it for a small VM or container (cron-less; it is a long-running process) and keep `config.json` as a secret. Running from more than one region would also diversify Embrace locations, which come from the sender IP.
4. **Scheduled incidents.** A daily or on-demand window where `payments-db-pool` (or another incident) switches on for 15–30 minutes, so dashboards show a spike and recovery without manual restarts.
5. **iOS sessions.** Same approach as Android: read `embrace-apple-sdk` for the envelope and span formats, then add an `IOSSession` and an iOS device catalog. One app ID per platform.
6. **Backend logs and metrics in Chronosphere.** `--backend-logs` already emits trace-correlated logs; consider enabling it in the service, and adding RED metrics (request rate, errors, duration per service) so Chronosphere dashboards and SLOs have data.
7. **More app realism.** Network failures without a response (offline, `java.net.SocketTimeoutException` on the client), ANRs, push-notification entry flows, more screens per line of business.
8. **Web mode consistency.** Web still uses the separate brands (Northline Goods, Corvia Bank, Vantra Travel). If web is used with a shared app ID, apply the same super-app treatment as Android.
9. **Tests.** `tools/check_output.py` is the safety net today; unit tests for `backend.layout`, `otlp` encoding and the Android envelope builder would make refactors safer.

## Pitfalls already hit

- Sessions sent before NSF is enabled for their domain are never forwarded; their traces stay root-less forever.
- Python's `http.server` hangs on macOS doing a reverse DNS lookup in `server_bind`; local test servers must override it.
- zsh does not word-split `$var`; use `${=var}` or arrays in shell loops.
- The Chronosphere query API returns at most ~10 traces per call; page by time window and operation.


### V2 browser implementation (2026-10-30)

`BrowserSession` now drives both V2 web targets and the legacy `--platform web` path. `web_session.py` only preserves the import and timestamp-shift API. The adapter uses the Vantra estate, origin-scoped identities, browser capability gates, hard/soft/bfcache lifecycles, real JS exception families, resource timing, vitals, LoAF, DOM state, and explicitly controlled Origin headers. Scenario allocation and payload-derived reporting handle web targets. The web examples and browser contract tests run in CI.

Source correction: WebVitalsInstrumentation's rounded TTFB breakdown uses `responseEnd - startTime`, whereas the metric uses `responseStart - startTime`; do not force those totals to be identical. LoafInstrumentation does not emit `emb.was_from_inp`; its IDs use `embracev2-`. Source and per-engine goldens take precedence over the original build prompt.

Phase 2 local checks: 13 browser tests plus the 57 existing tests; all three 120-session V1 dry runs, baseline/verification specs, and Android+web dry runs pass payload checks. Live ingestion, forwarding, retention and dashboard acceptance remain owner-gated. Next work is handoff 4.2 (four backend faults), then durable backfill receipts/resume, server, UI, MCP/copilot, hosting and later items.

### V2 backend fault builders

Handoff 4.2 is implemented in `backend_faults.py`: cache fallback or fail-closed, gateway admission with one Retry-After retry, delayed Kafka processing with pending status reconciliation, and canary-specific serializer failures with rollback. Resource cache keys include service version; internal routing metadata never enters request evidence. Presets expose all variants. Six new tests cover quotas, failure structure, retry timing, asynchronous timing, rollout attribution and lag bounds.

### Durable bounded runs

Handoff 4.3 uses `journal.py` (stdlib SQLite, FULL synchronous commits). A frozen RNG/pool/clock snapshot regenerates the same IDs, allocations and payload hashes. Intent is committed before each POST; accepted deliveries are never retried, and complete-session receipts follow delivery. In-flight, ambiguous and partially accepted exports block automatic resume. Retry-free strict receipt mode prevents hidden duplicate POSTs. At live send time stale backend exports expire and stale app payloads lose trace context. Five interruption/security tests exercise resume. Live stream persistence is provided by the server phase.

### V2 control plane

Handoff 4.4 now has `server/` with generated Pydantic v2 contracts, SQLAlchemy/SQLite persistence, versioned specs, isolated workers, receipt capture, SSE, run/stream controls, previews, readiness kits, write-only secrets and verification. Job processes use an OS lock and re-read the latest live spec before starting a session. Tests exercise API workflows and certificate-verified loopback ingest. `tests/server` intentionally runs separately so stdlib engine tests do not require server dependencies.

Embrace MCP source correction: `get_session_distribution` groups by a dimension, not hour. Web-vital tools use `name` (uppercase) and `dimension`, not `metric`/`group_by`. The server discovers and validates remote tool schemas; hourly platform exports can be attached through the observations endpoint for comparison with the sent cohort. API acceptance never establishes dashboard visibility.

### V2 scenario studio

Handoff 4.5 provides the React 18/TypeScript/Vite application in `apps/web`. Generated Zod validation supplements server validation; Radix controls, TanStack Query, RHF, Framer Motion, Recharts and cmdk provide the requested interactions. Browser tests cover every screen, keyboard commands, capability restrictions, laptop overflow, and a complete confirmed dry run. Future live incident previews use a projected clock and cannot send telemetry. UI tests own a separate `.state/ui-tests` server on loopback port 8001.

### V2 MCP and Copilot

Handoff 4.6 uses MCP SDK 2's `MCPServer` (the former FastMCP name was removed). SDK model properties are snake_case (`input_schema`, `is_error`, `structured_content`), while protocol JSON remains camelCase. HTTP and stdio integration tests exercise the actual protocol. REST authentication supplies caller attribution through a context variable. Copilot exposes only read operations and a terminal proposal tool; do not give it mutating MCP tools. Recorded responses cover all seven requested examples, unsupported capabilities, key gating, confirmation and optimistic concurrency. No live Anthropic calls are part of tests.

### V2 demo preparation

Handoff 4.7 adds durable preparation plans and a checklist in the studio. Each stage reuses a deterministic spec ID and run idempotency key. Separate process and transition locks protect orchestration restart and cancellation. Readiness is destination-sensitive; only explicit dry runs bypass live readiness, visibly marked simulated. Live mode switches retain existing delivery ownership and record the first trace-eligible session time. Three integration tests execute bounded stages locally, check no trace exports for history, exercise restart/cancellation, gate readiness and schedule the live incident.

### V2 hosted streams

Handoff 4.8 adds a multi-stage, non-root Docker image, hardened local Compose configuration, local-only token bootstrap and network-disabled container acceptance. Authorized active streams recover after restart; paused/stopped streams do not. Graceful shutdown drains then pauses managed streams. Operational logs use an explicit field allowlist. The live worker preserves cumulative counters across process restart. `HOSTING.md` documents private deployment, actual egress-based geography and an owner-approved cut-over; the existing LaunchAgent remains untouched.


### V2 later capabilities and final audit

Handoff 4.9 adds `IOSSession` and device/contract checks, `backend_metrics.py` with cumulative service RED exports, `regions.py` plus regional plan/apply tooling, and real executable JS bundles/maps in `web_symbols.py`. Source-map upload is explicitly confirmed and recorded before live symbolication; no upload occurred during development. Shared customer identities span platforms without adding nondeterministic resume state. Browser major-version ranges and lazy chunks without part rollover now work; UserTiming includes SDK mark logs.

Apple SDK source `a2e0e5c33766ea35b5ba85dcc983b29145fd384b` (6.21.0): envelope version `1.0`; `session.id` rather than Android user-session attributes; lowercase span status, omitted root parent, links arrays; string log nanoseconds. NSF emits `emb.w3c_traceparent` without Android's `emb.forward_telemetry`. Startup is `sys.startup`; thread samples use the exact SDK term `thread_blockage_sample`, narrowly allowlisted by the realism linter. Apple foreground sessions are separate SDK sessions and verification accounts for them. The iOS app must be created by the owner.

Web source correction: `_EmbraceFileBundleIDs` maps captured injection stack strings to bundle IDs, not URL keys. Use a map debugId without hyphens, or the original JS MD5 before snippet injection. The upload host is `dsym-store.emb-api.com`, not the app ingest host. Source code and exact-source tests govern these details.

Server visitor pools are per job, eliminating concurrent writers. Start idempotency and stream-limit checks share an OS lock; streams also have creation idempotency. Live reports state their process/version scope and retain completed version reports. Tests use temporary state and local transports. Never change `~/xcor-demo-generator` or restart its LaunchAgent without owner approval, even though the older workflow section lists restart as a historical step.
