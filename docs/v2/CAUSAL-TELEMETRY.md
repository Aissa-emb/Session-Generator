# Causal telemetry, version 1

## Audit (2026-10-31)

The current application emits synthetic SDK payloads, not running banking services or native applications. Browser exception fixtures are captured from executed JavaScript in Chromium, Firefox and WebKit. The runtime transports remain Android 9.3.1 envelope 0.1.0, Apple 6.21.0 envelope 1.0, Web 2.31.0 OTLP JSON, and backend gzip OTLP protobuf. SDK goldens and local SDK sources define these adapters. Backend instrumentation scopes previously imitated several SDKs; the causal mode identifies its own simulator instrumentation honestly.

`SpecRun` owns scheduling, bounded journals, pause/stop integration and trace age limits. `Generator`/`Planner` drive legacy journeys; `calls.simulate` chooses an endpoint outcome and independently draws transport timing. `backend.layout` supports parallel children and asynchronous work but has no explicit cross-operation dependency graph. `BrowserSession`, `AndroidSession` and `IOSSession` serialize platform-specific sessions. The new causal mode is additive to ScenarioSpec, preserving existing saved scenarios and transports.

Concrete findings:

- `_flush_logs` in the web adapter inserts fictional 40 ms upload spans. Remove this option's output, retaining the field only for old-spec compatibility.
- Web network failures and opaque responses use numeric HTTP status 0. Omit an unobserved response status.
- Existing backend graphs do not use the requested canonical service catalog. Introduce a registry and distinct graph builders, rather than renaming old generic trees.
- Independent client/backend random choices cannot guarantee the specified pool timeout, response-loss, decoding or async state transitions. Build these as a shared graph before serialization.
- The Apple generic exception adapter turns arbitrary handled failures into NSError and fatal failures into an Objective-C checkout mapper. The new contract fixtures need a caught Swift DecodingError and an explicit unsafe Swift fatal path.
- Android generic exception mapping invents a screen ViewModel frame. Contract fixtures need an exact TransactionDetailMapper source and stable build.
- Browser vitals are capability-gated, including Chromium soft-navigation support; INP already requires a click. Preserve these gates and keep asynchronous server wait separate from interaction processing.
- Native manual screen spans and activity spans can both be valid. Causal journeys will create only actual modeled transitions; fatal scenarios end their process without subsequent actions.

The screenshot durations are not themselves defects: client elapsed time can include transport, parallel work does not add serially, and successful backend work can precede lost response delivery. These boundaries need explicit evidence.

## Correlation and compatibility

The sending client context parents the ingress SERVER span. Network Span Forwarding sends the frontend span; the generator must not duplicate it. `emb.dashboard_session` is supplied by XCOR RUM forwarding, never synthesized by this generator. The verification report must extract it from a retrieved trace and distinguish local validation, API acknowledgement and dashboard navigation. Three prerequisites are destination enabled, app enabled and matching domain; client CORS propagation and destination retention also matter. Existing tenant sampling covered old service names, so the new catalog requires explicit verification.

Custom `demo.*` provenance is required by the owner's new brief and supersedes the older instruction to conceal synthetic data. Only documented keys are allowed; secrets and PII checks remain in force. Native/web session-property adapters prefix these keys with the SDK's `emb.properties.` convention. Backend fields are explicitly copied; arbitrary session properties are not presumed forwarded. No custom reserved `emb.*` fields are introduced.

Sources: [NSF and metadata](https://embrace.io/docs/data-forwarding/network-spans-forwarding/), [web propagation](https://embrace.io/docs/web/advanced-features/network-spans-forwarding/), [exporter exclusions](https://embrace.io/docs/web/advanced-features/opentelemetry-export/), [session backlink](https://embrace.io/blog/faster-easier-network-spans-forwarding-in-embrace/), [HTTP semantics](https://opentelemetry.io/docs/specs/semconv/http/http-spans/). Runtime/device execution and destination verification are separate gates; fixture success does not establish either.

## Implemented architecture

`xcorgen/causal/registry.py` is the versioned service, route, RPC and scenario catalog. `scenarios.py` builds a relative-time execution graph before any adapter runs. Nodes carry ownership, parent context, dependencies, links, deadlines and outcomes. `business.py` executes the synthetic transfer state machine with locked idempotency, balanced postings and authorized status reads. `adapter.py` translates that graph through the existing Android, Apple and web serializers; it does not make HTTP calls to a real bank or supplier. Only the configured telemetry destinations receive data.

The graph validator checks cycles, containment, dependency order, protocol pairs, service ownership, database kinds, cache behavior, HTTP observations, pool timeouts, business effects and termination. A second validator inspects serialized IDs, status, duration, diagnostics and asynchronous links. SDK contract checks and official protobuf round trips run separately. Unsupported native link fields are omitted: Apple and web can encode the custom business-span links, Android's pinned envelope cannot. The manifest retains their relationship, and the actual network CLIENT→SERVER context is supported on all three.

Logical RNG streams derive from scenario version, root seed, session index and purpose. Ordinary runs get a fresh emission namespace, separating session/trace/span/operation IDs from the reproducible story. Stable synthetic device identities are scoped by seed and platform (and web origin). `causal.replay_namespace` reproduces IDs only in local dry runs; the CLI and server reject it for live delivery. A null seed uses the documented causal default 7331. `reference: true` uses the reference fixture timing; false varies reading intervals and eligible latency while preserving deadlines and dependency order.

Backend resources use `service.namespace=vantra`, canonical logical process names, `deployment.environment.name=synthetic`, and the simulator's actual instrumentation name/version. The semantic-convention schema is pinned to 1.37.0. Region and service release are explicit resource/cohort fields; changing region metadata does not change XCOR RUM's IP-derived location. Service instance IDs describe stable simulated processes. No real regional infrastructure is provisioned.

Only the `PROVENANCE` allowlist in the registry is added as `demo.*` telemetry. Root-cause descriptions live in the manifest, not an invented automatic-diagnosis attribute. Session properties use the pinned SDK APIs' `emb.properties.*` serialization. Backend attributes are copied explicitly; custom fields are not assumed to survive NSF. Correlated backend diagnostics contain actual trace/span IDs. Existing RED exports distinguish HTTP from RPC operations.

## Scenario contracts

Every row has Android, iOS and web adapters, and affected/control/recovered fixtures. Healthy is itself a control; its three labels intentionally do not manufacture a failure.

| ID | Affected execution | Control and recovery |
| --- | --- | --- |
| `healthy` | Parallel profile/account branches, 310 ms reference overview, cold-cache SQL then history | Warm cache omits SQL; both branches precede composition |
| `signin-outage` | 3000 ms session-pool acquire timeout; HTTP 503; one owned diagnostic; no SQL or protected fetch afterward | Authenticate, persist and issue a session |
| `slow-history` | 2100 ms reference history query (1800–2800 ms when varied), successful HTTP 200, client waits | Fast query; no forced poor INP from asynchronous HTTP |
| `contract-regression` | HTTP 200 contains explicit null `merchant.displayName`; client decoding/view work fails | Nullable-aware client renders the same response with a fallback |
| `transfer-response-loss` | Intent accepted with 202 but client never observes response; 5000 ms client deadline; status reconciliation begins after timeout using pre-created operation reference | Response observed and the same logical operation completes |
| `worker-backlog` | Durable intent/outbox precedes separate relay/producer/consumer trace; polls stay Pending until ledger commit and completion update | Short queue delay, same one-intent/one-ledger-effect invariant |
| `frontend-regression` | Transfer view construction fails before submit; backend overview remains healthy. Optional web widget variant blocks an actual interaction for 300–600 ms | View works and transfer can proceed |
| `connectivity` | Pre-send offline has no ingress SERVER, or post-send response loss has successful backend work; neither invents HTTP status 0 | Request and response observed normally |
| `travel-provider` | Short cached autocomplete; rich search with parallel supplier CLIENTs, 1500 ms provider deadline, partial results or a 503 when all results are required | Healthy suppliers, quoting and ranking; no fabricated external SERVER internals |

The contract regression is a handled captured JavaScript exception on web, a handled `JsonDecodingException` on Android, and a caught Swift `DecodingError.valueNotFound` log on iOS. Explicit native fatal mode models unsafe null access: Kotlin NPE or Swift optional unwrap/SIGTRAP, with matching mapper/build. The process ends and emits no later actions. No relaunch is claimed inside that fixture. Browser exceptions are captured from executing the compiled fixtures in Chromium, Firefox and WebKit; optional source-map upload still requires a Symbol Upload credential. The UI does not expose a stack setup section.

Transfer response loss and worker backlog are different graphs. The idempotency key and operation reference exist before submission; retries use the same operation. A thread-safe business model proves one durable intent and one balanced logical ledger transaction under repeated and concurrent deliveries. Message processing is a separate linked trace. It is not parented underneath an HTTP response that already ended. Completion publication follows the ledger commit, and the status endpoint cannot report Completed before the orchestrator consumes that completion.

Optional web analytics occurs only after the consent event; support traffic only after opening support. These `.example` dependencies are modeled external CLIENTs, never contacted or given invented server internals. Their traffic has no first-party traceparent/NSF context.

## Controls and deterministic commands

In **Customize → Causal demo**, select the story and cohort. Use **Scenario parameters** for region, release, fixture timing, incident schedule/share, optional web traffic and the mixed-traffic weights. Platform selection, preview, send-once, finite continuous sending, rate, pause/resume/stop and delivery counters retain the approved workflow.

Scheduled exposure is evaluated at each session's execution time: before the window is control; inside it the seeded cohort-share predicate selects affected sessions; after it the selected cohort is recovered. The configured region/release describe this simulated cohort. Mixed mode defaults to 70% healthy, 15% selected hero, 10% slow and 5% recovered. These are seeded probabilistic weights, not an exact quota for a tiny sample. Travel mixed traffic remains within the travel profile.

From the repository root:

```sh
# All 81 platform × scenario × cohort combinations, no network delivery.
.venv/bin/python tools/causal_demo.py matrix /tmp/vantra-causal-matrix --seed 7331

# One story on all three platforms; ios00 is allowed only for this dry run.
.venv/bin/python generate.py run spec/examples/causal-platform-matrix.json --dry-run /tmp/vantra-platforms --quiet
.venv/bin/python tools/check_output.py /tmp/vantra-platforms

# Individual deterministic web fixture (11 ready-to-run files are in spec/examples).
.venv/bin/python generate.py run spec/examples/causal-slow-history.json --seed 7331 --dry-run /tmp/vantra-slow --quiet

# Live web mix, 10 sessions/minute for one hour. Set the token in your shell securely.
# Existing configured app: kfs9f; tenant: demo-sandbox. Review these before sending.
.venv/bin/python generate.py run spec/examples/causal-mixed-live.json --quiet

# Attach actual saved TRACE_IDS responses to a specific run's evidence.
.venv/bin/python tools/causal_demo.py evidence /path/to/manifest.jsonl --traces /path/to/returned-traces.json --output /tmp/vantra-evidence.json
```

Live examples resolve `CHRONOSPHERE_API_TOKEN` from the environment. They contain no credential. Replace `ios00` with a real iOS application before live iOS use. The UI uses its existing server credential store. Never retry ambiguous live ingestion merely to regenerate evidence; query the recorded IDs. Exact replay requires the same configuration, seed, session index and emission namespace; use a frozen clock for byte-level timestamp comparisons. An ordinary rerun intentionally changes IDs.

Each matrix directory contains its scenario, emitted SDK/OTLP payloads, durable manifest and evidence JSON. The top-level `matrix.json` lists all 81 outcomes and verification stages. Expected pre-send absence is represented explicitly. Delivery receipts prove acknowledgement, not storage. The verification endpoint queries every expected trace ID, retries bounded transient read failures, falls back to individual IDs when a batch returns 404, preserves missing/error denominators and extracts actual `emb.dashboard_session` values. A span link alone never counts as an observed span.

## Presenter walkthrough

1. Generate the same story as **affected**, **control** and **recovered** with the same root seed and platform. Keep the emitted manifest and its exact request/trace IDs. In XCOR RUM, select the app, time range and `demo.scenario_id` / `demo.variant` session properties when those filters are available; otherwise locate the known session ID.
2. **Frontend → backend:** open the affected session, find the named user action and its network request, then use that request's trace link. Compare its actual trace ID to the manifest. The first `vantra-edge` SERVER must have the forwarded CLIENT's exact span ID as parent. Follow the causal dependency: pool acquisition, history SQL, successful detail response, supplier timeout, or transfer processing. Compare the corresponding control trace.
3. **Backend → frontend:** query `service.namespace=vantra`, the canonical service and the scenario/cohort fields. Open a known retained trace, select its forwarded app CLIENT and follow the returned `emb.dashboard_session` link. Verify its app/session-part ID against the manifest and confirm the visible action/outcome. Never construct a replacement session URL.
4. For frontend contract/view failures, demonstrate that HTTP/backend work succeeds while client decoding/rendering fails. For pre-send offline, show the transport failure and expected absence of backend work. Absence caused by sampling is a different result and fails verification.
5. For transfers, open the linked worker trace using the message span link/operation ID. Show intent, outbox publication, queue delay, one ledger effect, completion event and status reconciliation in temporal order. Do not claim a worker trace has a direct RUM backlink unless one is actually returned.

## Evidence gates and remaining integration requirements

| Capability | Android | iOS | Web |
| --- | --- | --- | --- |
| Nine stories × three cohorts | Implemented; fixture checked | Implemented; fixture checked | Implemented; fixture checked |
| SDK envelope / OTLP contract | 9.3.1 / 0.1.0 | 6.21.0 / 1.0 | Web 2.31.0 |
| Native/device or full application runtime | Not executed | Not executed | Full app not executed; exception fixtures executed in three browser engines |
| Live ingest probe | 27 sessions acknowledged | Blocked by placeholder app ID | 27 sessions acknowledged |
| Retention and complete NSF joins | Query actual run evidence; partial | Not tested live | Query actual run evidence; partial |
| UI navigation in both directions | Not verified | Not verified | Actual backlinks observed; UI navigation not verified |
| Original-source exception resolution | Native frames modeled | Native frames modeled | Maps generated; destination upload not verified without token |

The 2026-10-31 bounded live probe used Android `5nrc2`, web `kfs9f` and `demo-sandbox`. Retrieved new-service traces showed `behavior_dataset_slug=unassigned`, `tail_sampling_rate=0.5`; some Android traces lacked their forwarded CLIENT. Tenant retention and forwarding therefore remain concrete acceptance blockers, not generator success claims. A scoped policy must include the canonical services plus the actual forwarded app service names; preserve unrelated tenant assignments. Existing `vantra-end-to-end` configuration only names older service identities. Neither old sampled-out data nor missing pre-configuration forwarding can be repaired by changing a local manifest.

The native Chrome dashboard session was inaccessible without macOS Accessibility/Screen Recording permission; the separate in-app browser required dashboard sign-in. No dashboard navigation or screenshot is claimed. No iOS app, new credential, retention rule or infrastructure was created. The generated report records exact retrieved links, missing parents and query errors so a presenter can finish these gates without guessing.
