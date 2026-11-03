# Native iOS, backend metrics, regional runners and source maps

## iOS

The Apple adapter reuses the journey/call scheduler and implements the Apple wire contract independently of Android envelopes. It targets SDK 6.21.0 at source revision `a2e0e5c33766ea35b5ba85dcc983b29145fd384b`:

- `EmbraceCore/Payload/{PayloadEnvelope,ResourcePayload,LogPayload}.swift` and `Payload/Spans/SpanPayload.swift`: version `1.0`, lowercase status, string log timestamps, links arrays, optional root parent.
- `Session/SessionSpanUtils.swift`: `session.id`, foreground sessions and termination/heartbeat attributes.
- `EmbraceSemantics/Span/SpanSemantics+{Startup,NetworkRequest,Hang,View}.swift`, `SpanEvent/SpanEventSemantics+Hang.swift`: startup, URLSession, view and hang capture. Startup durations are capped below the SDK's ten-second capture limit.
- `Capture/Network/URLSessionTaskHandler.swift`: traceparent without Android's forwarding flag.
- `Internal/Logs/EmbraceLogAttributesBuilder.swift` and the SDK KSCrash fixtures: `sys.ios.crash`, provider and embedded crash report structure.

The device catalog uses Apple model identifiers with compatible OS versions. `ios00` is reserved for local previews; live CLI/API starts reject it. Use `ios-checkout-crash` or `spec/examples/ios-burst.json`. Session properties retain the shared Vantra business units. Multi-platform runs draw some accounts from one deterministic shared customer cohort, while devices remain platform-specific.

A journey can span several iOS SDK sessions after foreground transitions. Manifests record each ID/start time; `sessions` counts generated journeys and `sdk_sessions` counts platform sessions. Hourly verification counts iOS SDK sessions individually. Android/web count user sessions.

## RED metrics

`destinations.backend_metrics=true` emits cumulative OTLP sums `http.server.request.count`, `http.server.request.error.count` and histogram `http.server.request.duration` (seconds). Request rate is the derivative of the request counter. Error count comes from SERVER span status; duration and explicit buckets come from the same SERVER spans. No client, database-client or Kafka-consumer spans inflate request counts. Each run uses a separate `service.instance.id`; process/spec-segment resets start a new producer. Pod labels are excluded from the aggregate to avoid attributing totals to one pod.

The transport is `/data/opentelemetry/v1/metrics`, gzip/protobuf with `API-Token`. Raw policy permits points up to two hours old; default aggregated policy conservatively permits two minutes. All policies reject more than one minute ahead. These checks run immediately before POST and record `expired` receipts. Sessions-only/historical app traffic produces no metrics, so enabling this feature does not create historical traces or arbitrary old metrics. Concurrent producers need independent state. Query totals by service and rate by producer before summing; do not average counters.

Sources: [Cortex XCOR OTLP endpoints](https://docs-xcor.paloaltonetworks.com/ingest/metrics-traces/otel/otlp-endpoints) and the verified metric limits in [V2-SCOPE](V2-SCOPE.md). `tools/check_protobuf.py` validates the encoder against `opentelemetry-proto` 1.38.0, including zero-valued sums, histogram buckets and partial success.

## Regional runners

Deploy the container from [HOSTING](HOSTING.md) on actual hosts in each desired region, with separate volumes, private TLS hostnames, credentials and `SG_RUNNER_REGION` labels. The label is exposed in health/capabilities and local run records; it cannot change geography. Embrace derives geography from the real egress IP. Never spoof forwarding/IP headers.

A connections file contains no credentials:

```json
[
  {"region":"us-east-1","url":"https://traffic-us.company.example","weight":2,"token_env":"SG_US_RUNNER_TOKEN"},
  {"region":"eu-west-1","url":"https://traffic-eu.company.example","weight":1,"token_env":"SG_EU_RUNNER_TOKEN"}
]
```

Use your deployed private DNS names. Securely inject the named API tokens into the operator process environment; do not put them in command arguments or commit them.

```sh
python3 tools/regions.py plan spec/examples/live-end-to-end.json connections.json --out regional-plan.json
# Review every runner, allocated rate, scenario and seed.
python3 tools/regions.py apply regional-plan.json --receipts regional-dry-receipts.json --confirm
# Only after the owner approves live regional traffic:
python3 tools/regions.py apply regional-plan.json --receipts regional-live-receipts.json --confirm --live
```

Weights split the total rate, while largest-remainder allocation preserves exact bounded counts. Seeds differ by region. Each destination creates an authenticated managed stream or bounded job. A locked, private, atomically written receipt file remembers spec/job IDs; retrying reuses server idempotency keys. Partial regional deployment remains explicit in that file; retry finishes missing regions. Open each server's Runs screen for health, pause, stop and verification. The read-only allocation API is `POST /api/regions/plan`, also exposed as MCP `plan_regions`.

## Executable bundles and source maps

`web_symbols.py` generates executable minified functions, original JavaScript sources and Source Map v3 mappings for every configured release/page bundle. Exception frames refer to those exact functions/positions. `debugId` is the original JavaScript MD5 rendered as a UUID; the SDK's bundle ID omits hyphens. The injection snippet records its runtime stack as a key in `_EmbraceFileBundleIDs`. Exception/log `emb.js_file_bundle_ids` uses the same stack-keyed JSON object. Third-party Script errors retain no invented stack.

This follows `packages/web-cli/src/{processSourceFiles,uploadToApi}.ts` and `web-sdk/src/managers/EmbraceLogManager/EmbraceLogManager.ts`. Upload sends multipart fields `id`, `app`, `token`, and `file` containing gzipped JSON `{bundle,sourcemap}` to the fixed `https://dsym-store.emb-api.com/v2/store/sourcemap` endpoint.

Enable `web.symbolication` on the scenario, save it, then review the exact plan in Connections. Save a symbol token in Settings and explicitly confirm upload. Live runs and live edits require acknowledgements for every asset ID. Changed releases require new maps. Acknowledgement establishes upload, not eventual readable frames; verify exceptions after platform processing.

CLI workflow:

```sh
python3 tools/symbols.py symbolicated-spec.json --out out/release-assets
# Review manifest.json and generated bundle/map files before upload.
python3 tools/symbols.py symbolicated-spec.json --out out/release-assets --upload --confirm --token-file /private/path/symbol.token
python3 generate.py run symbolicated-spec.json --symbol-receipt out/release-assets/receipts.json
```

No source-map upload, geographic deployment or live iOS/metrics acceptance was performed during implementation. Those are explicit owner actions.
