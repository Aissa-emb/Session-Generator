# V3 selective adoption

Reference: embrace-io/stock-data at edf1e31f86c0ed75f1b8bac2445f2d938273cc19. Starting implementation: ffa07d8, population model 1.0. This is an extension of ScenarioSpec 2.0, not a wire-format or delivery migration.

## Capability audit before implementation

| Proposal | Existing state | Decision |
| --- | --- | --- |
| Weighted visitors / intents / network / hardware | Implemented and measured on all three platforms | Expose existing canonical weights through the focused editor; preserve conditional identities |
| Eight statistical families and common metadata | Only lognormal helper and fixed per-operation parameters | Add a stdlib sampler, semantic validation, fast numerical preview and actual execution hooks |
| Cohort scale and tail effects | Exact fault quotas; no general numerical cohort override | Add deterministic operation-scoped numerical overrides; keep fault allocation separate |
| Journeys / abandonment / repeated visits | Intent-driven actions with business prerequisites | Tune the existing intent model; retain semantic paths rather than add an unconstrained graph editor |
| Release and causal recovery | Existing rollout rules and affected/control/recovered stories | Add piecewise adoption at visit boundaries; retain supported incident stories and native lifecycle semantics |
| Sticky experiments | Feature flags exist; no general exposed-versus-assigned model | Add deterministic assignment and endpoint-triggered exposure with separate evidence |
| ATC token experiment / post-purchase add-on / renderer failure | No matching authenticated retail operations or verified render termination contract | Do not pass off status rewrites or missing telemetry as these stories; retain current verified faults |
| Histograms and evidence | Serialized population/fault reports; no numerical editor | Extend existing preview/report, explicitly distinguish raw draws from emitted durations |
| Kafka / processed rows / ClickHouse / new product | Incompatible with SDK ingestion architecture | Omit |

## UI control consolidation

| Current control | Decision | Canonical location |
| --- | --- | --- |
| Standard / Regression / Causal buttons plus regression and story selectors | Replace | One Scenario picker |
| User flow | Keep, move first | Business area beside Scenario |
| Signal selection and its affected percentages | Move; selected scenario rate shown once | Scenario controls / Fine-tune → Faults for combinations |
| Visitor mix disclosure, model, return/cold/interrupt shares | Merge | Fine-tune → Population |
| New relative intent / network weights | Integrate with existing population fields | Fine-tune → Journey / Population |
| New latency and old slow-page p75 | Different quantities, one focused editor each | Fine-tune → Performance; slow-page fault severity remains scenario-specific |
| Causal cohort / cache / network / seed / service release / incident schedule | Merge | Relevant story controls and Fine-tune → Scenario |
| Sending schedule, destination, run controls | Keep | Existing locations |
| Population tables / diagnostics / numerical results | Keep optional | Preview details |

No claim that synthetic locale changes destination geolocation. HTTP acknowledgement remains distinct from dashboard storage. Presets are illustrative assumptions, not market measurements. No live telemetry is sent as part of development verification.

## Adopted semantics

`modeling.version = 1.0` adds optional operation cost distributions. Omission preserves V2 defaults. Four executable quantities are reading pauses, per-character typing, API round-trip transport, and the gateway's own processing work. These are components, not promises of total request/session duration. Existing sequential/parallel/deadline layout remains authoritative. Distribution controls do not overwrite page-load metrics or grow INP from asynchronous waiting.

Eight families share one stdlib sampler; lognormal offers median/p95 controls. Truncation has a 256-attempt cap and fails on negligible probability; clipping is explicit. Tail effects use a fixed 4,096-draw threshold from the configured distribution, with values at/below the knee unchanged. No future stream ranking is required. The same transform runs in the numerical editor and generation. Bounds apply after an effect and are reported.

Bindings select a quantity plus optional platform, business area, device tier, network, intent, version, returning state, actual screen or endpoint, and exposed experiment variants. Higher matching priority wins; equal priorities for the same quantity are rejected. Effect share is a probability over visits reaching matching work, distinct from the existing exact fault quotas. Reading/typing remain natural journey actions; no target duration compresses a network deadline.

Experiment assignment is deterministic by root seed, device, web origin, experiment ID/version/salt. Enrollment and variant selection are independent of definition order. Assignment is separate from exposure at actual API endpoints. Performance audiences may require intersections of exposed variants. Assignment and exposure are reported separately; this does not claim an unsupported SDK experiment event or native crash type.

The optional editor saves/reopens a versioned workflow configuration, including every advanced performance parameter. The API's existing ScenarioSpec store round-trips canonical modeling configuration. Exported configurations contain app/tenant identifiers but never API tokens.

Release curves interpolate version shares between ordered time points. Web selects a deployed build at the visit boundary; native versions retain their catalog version/build/SDK tuple, and returning devices upgrade without downgrading unless rollback is explicitly enabled. Hash assignment is independent of performance and experiment draws. Preview reports actual payload version counts; requested curves are population targets, not an exact quota for retained native installs. Fine-tune → Release edits the same saved curve.

The reference's free-form transition matrix, new retail auth/add-on contracts, and processed termination records were not imported. The current semantic intent paths, native lifecycle rules, causal recovery stories, and existing rollback mechanisms remain authoritative. These would require additional business contracts rather than cosmetic overrides. No new infrastructure, application, processed-data sink, or egress spoofing is introduced.

## Verification (2026-11-02)

All validation below used local output or isolated test receivers. No customer telemetry was sent.

- 129 engine tests; 40 API/job/receiver tests; 21 browser acceptance tests.
- Eight new distribution/release tests cover all families, bounds, tied tails, independent RNG streams, invalid audiences, sticky experiments, real payload versions, fresh run identities and all platform adapters. Durable resume also reconstructs an identical model report from written payloads and the journal.
- Four 120-session Android/web/iOS/incident dry runs and the 600-session baseline pass payload and realism checks.
- The 360-visit performance example produces 1,592 linked API/backend traces and 23,638 backend spans across 44 services, with no relationship or payload validation errors.
- Production build, generated Python/Zod contracts, responsive checks at 320–1920px and 200% text, detached-server ownership checks, Bandit and redacted source secret scanning pass.

A fixed-clock audit uses 600 visits per platform, seed 8219, with a 50% visit selection probability. The configured gateway cost has median 120 ms / p95 500 ms; its upper 20% receives a tail effect. All 1,800 serialized visits pass SDK/realism validation. The observed selection is 312/600 on each platform (52%); the operation-specific draw stream remains independent of platform selection.

| Platform | Modeled gateway median before / after | Emitted request p95 control / affected |
| --- | --- | --- |
| Android | 117.3 / 117.3 ms | 1,128.5 / 1,860.2 ms |
| iOS | 117.2 / 117.2 ms | 1,340.9 / 2,156.3 ms |
| Web | 117.1 / 117.1 ms | 1,006.2 / 1,779.9 ms |

These are measured illustrative results, not promised live population percentiles. Request totals include unchanged transport and child-service work. A separate 4,000-device adoption check verifies the halfway share; native promotion and explicit rollback use the original build/SDK tuples. Live workers retain the model index across version edits and write the matching model evidence into their session journal.

Reproduce without network delivery:

```sh
python3 -m unittest tests.test_distributions tests.test_resume -v
python3 tools/modeling_audit.py out/modeling-audit.json
python3 generate.py run spec/examples/performance-tail.json --dry-run out/performance --quiet
python3 tools/check_output.py out/performance
python3 generate.py lint out/performance
python3 generate.py report out/performance spec/examples/performance-tail.json
```

The dry run writes `reports/signals.json` beside `reports/emitted.json`; advanced Preview can download the same evidence structure. Draw diagnostics are capped at 512 per visit and any omissions are counted. Recent values are local generation evidence; neither this report nor HTTP acknowledgement establishes remote retention. The existing iOS placeholder must be replaced before live use.

## Deliberate boundaries

The general numerical editor supports the four named duration components only. Native startup phases, browser vital scores, timeout probabilities, and activity counts retain their existing platform/causal controls. Cohorts support the dimensions exposed by the canonical `modeling` schema; arbitrary attribute predicates or time-window overlays are not accepted. Existing scheduled causal stories remain the supported way to demonstrate incident onset and recovery. This avoids implying that every reference overlay can safely be applied to every telemetry field.
