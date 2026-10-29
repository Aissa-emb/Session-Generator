# Handoff notes for AI agents

You are picking up **Session-Generator**: a stdlib-only Python tool that generates realistic mobile (Android) and web sessions for **Cortex XCOR RUM** (formerly Embrace) and matching full distributed backend traces for **Cortex XCOR** (formerly Chronosphere), stitched together so a session's network request opens a complete trace. It feeds a live sales/demo tenant. Read this file fully before changing anything; `README.md` is the user-facing guide.

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
- **Critical timing rule:** Embrace forwards client spans only after a session part ends and is processed (minutes after the request). Chronosphere dropped client spans that arrived minutes after the rest of their trace (traces showed a `<Missing>` root even though Embrace said "Forwarded"). The generator therefore holds each backend trace until its session part ends and sends it 70–130 s later (`calls.release_backend`, `FORWARD_DELAY_MS`). Burst mode sends Embrace payloads first and waits 70 s before backend exports. Do not undo this.

## Verified facts (with where they came from)

The owner has local clones in `~/Claudecode/` (Embrace SDKs, Go backend `go/`, dashboard `dashjs/`). Facts below were verified there on 2026-10-29.

- **Android ingest**: gzipped JSON envelope `{"resource", "metadata", "version": "0.1.0", "type": "spans"|"logs", "data"}` to `https://a-<appId>.data.emb-api.com/v2/spans|logs`, headers `Accept`, `User-Agent: Embrace/a/<sdk>`, `Content-Type: application/json`, `Content-Encoding: gzip`, `X-EM-AID`, `X-EM-DID` (32 uppercase hex, stable per device), `X-EM-PAYLOAD-TYPES`. All attribute values are strings. Span/log shapes, names and keys come from `embrace-android-sdk` source and its integration-test golden files (copies in `reference/android-sdk-golden/`). Key points: network span name `emb-<METHOD> <path>` (query stripped), status `Unset` for <400 and `Error` + `emb.error_code=failure` for >=400 (never `Ok`); taps are `emb-ui-tap` events (Compose: label text; Views: `pkg:id/name`); Fragments are not auto-captured (we use `Embrace.startView` style spans with `emb.manual_instrumentation`); crashes are `sys.android.crash` logs with JSON-array stack traces.
- **Web ingest**: gzipped OTLP/JSON from `@embrace-io/web-sdk` 2.31.0, captured from a real browser into `reference/web-sdk-2.31.0-capture/`.
- **Backend parsing**: `go/lib/codec` maps `emb.type=perf.network_request` spans to network requests and reads `emb.w3c_traceparent`; NSF processor is `go/loader/session/nsf/internal/processor.go`; forwarded span service name is `<app name>-<platform>-<environment>` lowercased (`go/lib/common/data_destinations_config.go`). Parts of a user session are grouped by `emb.user_session_id` + device ID, within ±3 days. Timestamps are clamped, not dropped. Location comes from the sender's IP (MaxMind), so all sessions show the machine's location.
- **Chronosphere OTLP**: `https://<tenant>.chronosphere.io/data/opentelemetry/v1/traces`, protobuf, gzip, header `API-Token`. The sender checks OTLP partial-success rejections. `tools/check_protobuf.py` proves the encoder matches the official protos.
- **Chronosphere Data API** (for verification): `POST /api/v1/data/traces`, header `API-Token`. `TRACE_IDS` queries must not include a time range; `SERVICE_OPERATION` queries need one of at most 15 minutes. See `tools/chronosphere_traces.py`.

## Current deployment state (2026-10-29)

- **Embrace app**: one Android app, `5nrc2` ("Vantra", production, US), used for all three profiles. Because several profiles share one app ID, the generator runs in **shared super-app mode**: one app `com.vantra.android` with three lines of business told apart by the session property `business_unit` = `retail` | `banking` | `travel`, plus `market`, `membership_tier`, `entry_point` and business-specific properties. One user base and account system across the app.
- **Forwarded domains needed in Embrace NSF**: `api.vantrashop.com`, `api.vantramoney.com`, `api.vantratravel.com`. The owner was asked to add all three; confirm with them before assuming.
- **Chronosphere tenant**: `demo-sandbox`. The API token is in `config.json` (git-ignored, mode 600). **Never commit `config.json` or print the token.** `config.example.json` shows the shape.
- **Always-on service**: macOS LaunchAgent `local.xcor-demo-generator` (copy in `deploy/`), running `generate.py --profile all --rate 3 --diurnal --quiet`. Logs in `logs/generator.log` (status line every 10 min). After code changes restart it: `launchctl kickstart -k gui/$(id -u)/local.xcor-demo-generator`. It stops while the Mac sleeps.
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
4. Restart the LaunchAgent, wait ~8 minutes, then confirm in Chronosphere:
   ```bash
   python3 tools/chronosphere_traces.py completeness <restart time, RFC3339 UTC>
   ```
   and in Embrace (the Embrace MCP server, app `5nrc2`: sessions, network domains, root spans).
5. Commit as the owner (see below).

## Git conventions

Commit and push as **Aissa-emb** (`git config user.name "Aissa-emb"`, `git config user.email "200142933+Aissa-emb@users.noreply.github.com"`). No `Co-Authored-By`, `Assisted-by` or "Generated with" trailers. Push with GitHub CLI credentials if the keychain holds another account: `git -c credential.helper= -c "credential.helper=!gh auth git-credential" push`.

## Backlog (suggested order)

1. **Confirm NSF domains** for `api.vantrashop.com` and `api.vantramoney.com` with the owner, then verify retail and banking traces are complete too (travel was the first domain enabled).
2. **Tune the hold delay.** `FORWARD_DELAY_MS` (70–130 s) was chosen from observed Embrace forwarding latency. Measure completeness over a few hours with `tools/chronosphere_traces.py` and adjust if some app spans still go missing.
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
